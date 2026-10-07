"""Orchestrator: runs the marketing chain in the order of the plan and decides what needs a human.

  Data/CRM -> Strategy -> Creative -> Verification -> Human approval -> Distribution -> (Response) -> Analytics
      ^                                                                                                 |
      +---------------------------- results feed the next Strategy cycle -------------------------------+
"""
from dataclasses import asdict

from sqlalchemy import select

from ..core import agents
from ..core.models import Campaign, Tenant
from ..core.playbook import setting
from ..core.utils import now
from ..services.tasks import add_task
from . import (
    attribution,
    audience,
    creative,
    distribution,
    poster,
    strategy,
    verification,
)


def _plan_dict(play: strategy.Play) -> dict:
    d = asdict(play)
    d["send_at"] = play.send_at.isoformat(timespec="minutes") if play.send_at else None
    return d


WRITERS = ("ai", "own")                    # who writes the message
VISUALS = ("none", "ai_poster", "upload")  # what image goes with it ("upload" = attached after creation)


def _poster_words(campaign: Campaign) -> dict | None:
    return (campaign.plan or {}).get("poster") if campaign.image else None


def checks_for(s, tenant: Tenant, campaign: Campaign) -> dict:
    """Verification of everything a customer will read: message versions and poster words."""
    return verification.verify_all(s, tenant, campaign.variants or [], _poster_words(campaign))


def launch(s, tenant: Tenant, play: strategy.Play, writer: str = "ai", message: str | None = None,
           visual: str = "none") -> Campaign:
    """Strategy output -> Creative -> Verification -> approval task. Nothing reaches a customer before approval."""
    if writer not in WRITERS or visual not in VISUALS:
        raise ValueError("unknown message or image option")
    if writer == "own" and not (message or "").strip():
        raise ValueError("write the message, or choose 'Write with AI'")
    people = audience.segment_contacts(s, tenant, play.segment)
    if writer == "own":
        draft = creative.manual(message)
    elif agents.enabled(tenant, "creative"):
        draft = creative.write(s, tenant, play.goal, play.offer, f"{play.name}: {len(people)} contacts. {play.why}", play.skus)
    else:  # Creative off: the goal text is sent as written (still verified and approved)
        draft = creative.manual(play.offer)
    plan = _plan_dict(play)
    image = None
    if visual == "ai_poster":
        plan["poster"] = creative.poster_words(draft, play.name, draft.variants[0]["text"] if draft.variants else play.offer)
        image = poster.render(tenant, **plan["poster"])
    campaign = Campaign(tenant_id=tenant.id, name=play.name, goal=play.goal, segment=play.segment, play=play.key,
                        channel=play.channel, plan=plan, variants=draft.variants, image_brief=draft.image_brief,
                        image=image, draft=draft.variants[0]["text"] if draft.variants else None, send_at=play.send_at,
                        stats={"audience": len(people), "writer": writer, "visual": visual})
    campaign.checks = checks_for(s, tenant, campaign)
    campaign.status = "pending_approval" if campaign.checks["ok"] else "needs_fix"
    s.add(campaign)
    s.flush()
    add_task(s, tenant.id, None, "approval", campaign_id=campaign.id,
             reason=f"campaign '{play.name}' to {play.reachable} reachable contacts"
                    + ("" if campaign.checks["ok"] else f" - {len(campaign.checks['errors'])} verification error(s) to fix"))
    return campaign


def launch_play(s, tenant: Tenant, key: str, visual: str = "none") -> Campaign:
    if not agents.enabled(tenant, "strategy"):
        raise ValueError("the Strategy agent is off; write your own campaign instead")
    play = next((p for p in strategy.recommend(s, tenant, limit=20) if p.key == key), None)
    if play is None:
        raise ValueError(f"play {key!r} is not available right now (no reachable audience, or on cooldown)")
    return launch(s, tenant, play, visual=visual)


def launch_custom(s, tenant: Tenant, name: str, goal: str, segment: dict, channel: str = "messaging",
                  writer: str = "ai", message: str | None = None, visual: str = "none") -> Campaign:
    """A manager's own idea still goes through Creative (if chosen) -> Verification -> Approval."""
    if channel not in distribution.CHANNELS:
        raise ValueError(f"channel must be one of {distribution.CHANNELS}")
    if not name.strip():
        raise ValueError("give the campaign a name")
    people = audience.segment_contacts(s, tenant, segment)
    play = strategy.Play("custom", name.strip(), goal, offer=goal or message or name, segment=segment, channel=channel,
                         audience=len(people), reachable=len(audience.reachable(s, people)),
                         send_at=strategy.best_send_time(s, tenant, now()), why="Created by a manager.")
    return launch(s, tenant, play, writer, message, visual)


def redesign_poster(s, tenant: Tenant, campaign: Campaign, headline: str | None = None, subline: str | None = None,
                    cta: str | None = None) -> Campaign:
    """(Re)draws the AI poster, optionally with the manager's own words; re-checks everything."""
    _editable(campaign)
    words = {**creative.poster_words(creative.Draft([]), campaign.name, campaign.draft or campaign.goal),
             **((campaign.plan or {}).get("poster") or {})}
    words.update({k: v.strip() for k, v in {"headline": headline, "subline": subline, "cta": cta}.items() if v and v.strip()})
    campaign.plan = {**(campaign.plan or {}), "poster": words}
    campaign.image = poster.render(tenant, **words)
    campaign.stats = {**(campaign.stats or {}), "visual": "ai_poster"}
    _recheck(s, tenant, campaign)
    return campaign


def attach_upload(s, tenant: Tenant, campaign: Campaign, data: bytes) -> Campaign:
    """The company's own poster replaces any generated one."""
    _editable(campaign)
    campaign.image = poster.save_upload(data)
    campaign.plan = {k: v for k, v in (campaign.plan or {}).items() if k != "poster"}  # nothing to check in a photo
    campaign.stats = {**(campaign.stats or {}), "visual": "upload"}
    _recheck(s, tenant, campaign)
    return campaign


def remove_image(s, tenant: Tenant, campaign: Campaign) -> Campaign:
    _editable(campaign)
    campaign.image = None
    campaign.stats = {**(campaign.stats or {}), "visual": "none"}
    _recheck(s, tenant, campaign)
    return campaign


def _editable(campaign: Campaign) -> None:
    if campaign.status not in ("pending_approval", "needs_fix"):
        raise ValueError("this campaign was already approved; images can only change before approval")


def _recheck(s, tenant: Tenant, campaign: Campaign) -> None:
    campaign.checks = checks_for(s, tenant, campaign)
    campaign.status = "pending_approval" if campaign.checks["ok"] else "needs_fix"


def approve(s, tenant: Tenant, campaign: Campaign, text: str | None = None, send_now: bool = False) -> dict:
    """Manager approved (optionally with rewritten copy): re-verify, then send at the planned time or now."""
    if not agents.enabled(tenant, "distribution"):
        raise ValueError("the Distribution agent is off: turn it on in Agents to send campaigns")
    if send_now:
        campaign.send_at = None
    if text:
        campaign.variants = [{"label": "A", "angle": "edited by manager", "text": text}]
        campaign.draft = text
    campaign.checks = checks_for(s, tenant, campaign)
    if not campaign.checks["ok"]:
        campaign.status = "needs_fix"
        raise ValueError("verification failed: " + "; ".join(campaign.checks["errors"]))
    if campaign.send_at and campaign.send_at > now():
        campaign.status = "scheduled"
        return {"campaign": campaign.id, "status": "scheduled", "send_at": campaign.send_at}
    return distribution.dispatch(s, tenant, campaign)


def reject(campaign: Campaign) -> dict:
    campaign.status = "rejected"
    return {"campaign": campaign.id, "status": "rejected"}


def run_cycle(s, tenant: Tenant, daily: bool = False) -> dict:
    """Every tick: send what's due. Daily: measure results, then (if enabled) draft the next best play."""
    out = {"dispatched": distribution.dispatch_due(s, tenant, now()) if agents.enabled(tenant, "distribution") else []}
    if daily:
        out["measured"] = attribution.refresh(s, tenant) if agents.enabled(tenant, "analytics") else 0
        waiting = s.scalars(select(Campaign.id).where(Campaign.tenant_id == tenant.id,
                                                      Campaign.status.in_(strategy.ACTIVE_STATUSES))).first()
        if setting(tenant, "marketing.auto_draft") and not waiting and (plays := strategy.recommend(s, tenant, 1)):
            out["drafted"] = launch(s, tenant, plays[0]).id
    return out


def list_campaigns(s, tenant_id: int, limit: int = 100) -> list[Campaign]:
    return s.scalars(select(Campaign).where(Campaign.tenant_id == tenant_id)
                     .order_by(Campaign.created_at.desc(), Campaign.id.desc()).limit(limit)).all()
