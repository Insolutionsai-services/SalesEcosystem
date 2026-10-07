"""The agent roster from the plan document, with what each agent has done in the last 30 days.
This is the live answer to "which part of the sales team is doing what"."""
from datetime import timedelta

from sqlalchemy import func, select

from ..core import agents
from ..core.models import (
    Activity,
    Campaign,
    Consent,
    Contact,
    Item,
    Quote,
    Task,
    Tenant,
)
from ..core.utils import now
from ..marketing import strategy
from ..ml import store

CHAIN = ["data_crm", "strategy", "creative", "verification", "approval", "distribution", "response", "analytics"]


def _count(s, model, *where) -> int:
    return s.scalar(select(func.count()).select_from(model).where(*where)) or 0


def roster(s, tenant: Tenant) -> list[dict]:
    t, since = tenant.id, now() - timedelta(days=30)
    acts = s.scalars(select(Activity).where(Activity.tenant_id == t, Activity.at >= since)).all()
    campaigns = s.scalars(select(Campaign).where(Campaign.tenant_id == t, Campaign.created_at >= since)).all()

    def acts_of(type_, **data):
        return [a for a in acts if a.type == type_ and all(a.data.get(k) == v for k, v in data.items())]

    replies = acts_of("msg_out", kind="reply")
    brain = store.load(t)
    team = [
        {"key": "data_crm", "name": "Data / CRM Agent", "layer": "Foundation", "status": "active",
         "job": "Single source of truth: customers, consent, catalog/fact sheet; logs every conversation automatically.",
         "code": "services/contacts, catalog, activity · ml/ training", "stats": {
             "contacts": _count(s, Contact, Contact.tenant_id == t), "catalog items": _count(s, Item, Item.tenant_id == t),
             "consent records": _count(s, Consent, Consent.tenant_id == t), "events logged (30d)": len(acts),
             "models trained": brain.trained_at.isoformat(timespec="minutes") if brain else "never"}},
        {"key": "strategy", "name": "Strategy Agent", "layer": "Thinking", "status": "active",
         "job": "Picks segment + offer + channel + timing from data and past campaign results.",
         "code": "marketing/strategy", "stats": {
             "plays recommended now": len(strategy.recommend(s, tenant)),
             "plays learned from results": len({c.play for c in campaigns if c.results})}},
        {"key": "creative", "name": "Creative Agent", "layer": "Doing", "status": "active",
         "job": "Writes A/B copy per segment and an image/video brief (image generation: later phase).",
         "code": "marketing/creative", "stats": {
             "campaigns drafted (30d)": len(campaigns), "A/B variants": sum(len(c.variants or []) for c in campaigns)}},
        {"key": "verification", "name": "Verification / Compliance Agent", "layer": "Doing", "status": "active",
         "job": "Checks every price/claim against the catalog and rules; consent, opt-out, quiet hours, caps.",
         "code": "marketing/verification · services/compliance", "stats": {
             "campaigns failing checks (30d)": sum(1 for c in campaigns if c.checks and not c.checks.get("ok")),
             "sends blocked by rules (30d)": len(acts_of("blocked"))}},
        {"key": "approval", "name": "Human approval checkpoint", "layer": "Control", "status": "active",
         "job": "Managers approve campaigns and quotes above limits; hand-offs to salespeople.",
         "code": "workflows/approvals · Queue tab", "stats": {
             "waiting for approval": _count(s, Task, Task.tenant_id == t, Task.kind == "approval", Task.status == "open"),
             "open hand-offs": _count(s, Task, Task.tenant_id == t, Task.kind == "handoff", Task.status == "open")}},
        {"key": "distribution", "name": "Distribution Agent", "layer": "Doing", "status": "active",
         "job": "WhatsApp/email broadcast (A/B + holdout) at the planned time; social/ads via webhook.",
         "code": "marketing/distribution · services/channels", "stats": {
             "campaign messages sent (30d)": len(acts_of("msg_out", via="campaign")),
             "scheduled campaigns": _count(s, Campaign, Campaign.tenant_id == t, Campaign.status == "scheduled"),
             "social posts (30d)": sum(1 for c in campaigns if c.channel == "social" and c.status == "sent")}},
        {"key": "response", "name": "Response Agent", "layer": "Doing", "status": "active",
         "job": "Replies in seconds, qualifies, quotes from the catalog, follows up persistently, hands off.",
         "code": "workflows/inbound, followups · ai/fastpath, agent", "stats": {
             "replies (30d)": len(replies), "answered by local models": sum(a.data.get("via") == "fastpath" for a in replies),
             "quotes (30d)": _count(s, Quote, Quote.tenant_id == t, Quote.created_at >= since),
             "follow-ups sent (30d)": len(acts_of("msg_out", kind="followup"))}},
        {"key": "voice", "name": "Voice Agent", "layer": "Doing (later phase)", "status": "planned",
         "job": "Answers calls for engaged leads (IndicConformer speech-to-text + Indic Parler-TTS).",
         "code": "not built yet - deferred per plan", "stats": {}},
        {"key": "analytics", "name": "Analytics Agent", "layer": "Learning", "status": "active",
         "job": "Measures replies, sales and lift vs holdout per campaign and variant; feeds Strategy.",
         "code": "marketing/attribution · analytics/metrics, scoring", "stats": {
             "campaigns measured": sum(1 for c in campaigns if c.results),
             "AI calls (30d)": len(acts_of("llm"))}},
        {"key": "orchestrator", "name": "Orchestrator Agent", "layer": "Control", "status": "active",
         "job": "Runs the chain in order, schedules sends, retries later on failure, escalates to humans.",
         "code": "marketing/orchestrator · workflows/scheduler", "stats": {
             "campaigns in the pipeline": sum(1 for c in campaigns if c.status in strategy.ACTIVE_STATUSES)}},
    ]
    for agent in team:  # live state from the company's settings: always / on / off / planned
        agent["status"] = agents.state(tenant, agent["key"])
    return team


def _short(text: str | None, n: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _event(at, agent: str, text: str, tone: str = "info", contact_id=None, campaign_id=None) -> dict:
    return {"at": at, "agent": agent, "text": text, "tone": tone, "contact_id": contact_id, "campaign_id": campaign_id}


def _activity_event(a: Activity, who: str) -> dict | None:
    d, c = a.data or {}, a.contact_id
    if a.type == "msg_in":
        return _event(a.at, "Customer", f"{who}: “{_short(a.body)}”", "info", c)
    if a.type == "msg_out":
        via, kind = d.get("via"), d.get("kind")
        if via == "campaign":
            return _event(a.at, "Distribution", f"Delivered campaign variant {d.get('variant', 'A')} to {who}", "info", c,
                          d.get("campaign_id"))
        if via == "human":
            return _event(a.at, "Salesperson", f"Replied to {who}: “{_short(a.body, 60)}”", "info", c)
        if kind == "followup":
            return _event(a.at, "Response", f"Followed up with {who}", "info", c)
        if kind == "marketing":
            return _event(a.at, "Response", f"Sent {who} a proactive message", "info", c)
        how = " instantly with a local model" if via == "fastpath" else ""
        return _event(a.at, "Response", f"Answered {who}{how}: “{_short(a.body, 70)}”", "good", c)
    if a.type == "blocked":
        return _event(a.at, "Verification", f"Stopped a {d.get('kind')} message to {who}: {d.get('reason')}", "warn", c)
    if a.type == "failed":
        return _event(a.at, "Distribution", f"Couldn't deliver to {who}: {_short(d.get('error'), 60)}", "bad", c)
    if a.type == "quote" and d.get("status"):
        total = f" · {d['total']:,.0f}" if d.get("total") else ""
        tone = {"won": "good", "lost": "bad", "pending_approval": "warn"}.get(d["status"], "info")
        return _event(a.at, "Response", f"Quote #{d.get('quote_id')} {d['status'].replace('_', ' ')} for {who}{total}", tone, c)
    if a.type == "stage":
        return _event(a.at, "Data / CRM", f"Moved {who} {a.body}", "info", c)
    if a.type == "insights":
        return _event(a.at, "Analytics", "Wrote this week's insights for the manager", "good")
    return None


def feed(s, tenant: Tenant, limit: int = 40) -> list[dict]:
    """What the agents did recently, newest first, in plain words."""
    acts = s.scalars(select(Activity).where(Activity.tenant_id == tenant.id, Activity.type != "llm")
                     .order_by(Activity.at.desc(), Activity.id.desc()).limit(limit)).all()
    ids = {a.contact_id for a in acts if a.contact_id}
    names = {c.id: c.name or c.phone or c.email for c in s.scalars(select(Contact).where(Contact.id.in_(ids)))} if ids else {}
    events = [e for a in acts if (e := _activity_event(a, names.get(a.contact_id, "a customer")))]
    for c in s.scalars(select(Campaign).where(Campaign.tenant_id == tenant.id).order_by(Campaign.id.desc()).limit(10)):
        events.append(_event(c.created_at, "Creative", f"Drafted “{c.name}” ({len(c.variants or [])} versions)",
                             "info", campaign_id=c.id))
        if c.checks and not c.checks.get("ok"):
            events.append(_event(c.created_at, "Verification", f"Flagged {len(c.checks['errors'])} issue(s) in “{c.name}”",
                                 "warn", campaign_id=c.id))
        if c.sent_at:
            events.append(_event(c.sent_at, "Distribution", f"Launched “{c.name}” to {(c.stats or {}).get('sent', 0)} customers",
                                 "good", campaign_id=c.id))
    return sorted(events, key=lambda e: e["at"], reverse=True)[:limit]
