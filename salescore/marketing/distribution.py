"""Distribution agent: delivers an approved campaign - WhatsApp/email broadcast through the compliance gate
(A/B split + holdout), and social/ad posts to a webhook (Postiz, Zapier, Make, n8n, ...)."""
import logging
from datetime import datetime

import httpx
from sqlalchemy import select

from ..core.config import PUBLIC_BASE_URL
from ..core.models import Campaign, Contact, Tenant
from ..core.playbook import setting
from ..core.utils import first_name, now
from ..services import channels, plan
from ..services.activity import log_activity
from . import audience

log = logging.getLogger("salescore.distribution")
CHANNELS = ("messaging", "social")
DESTINATIONS = {"whatsapp": "WhatsApp", "email": "Email", "instagram": "Instagram", "meta_ads": "Meta Ads", "google_ads": "Google Ads"}
MESSAGING = ("whatsapp", "email")   # one message to each customer; the rest are one post through the company's webhook


def available(tenant: Tenant) -> list[dict]:
    """Every place a campaign can go and whether this company has connected it."""
    free = plan.is_trial(tenant)
    ok = {**channels.ready(tenant), **{k: bool(setting(tenant, "distribution.webhook_url")) and not free for k in DESTINATIONS if k not in MESSAGING}}
    out = [{"key": k, "label": label, "ready": ok[k]} for k, label in DESTINATIONS.items()]
    for d in out if free else []:
        d.update({"label": f"Test to my email ({tenant.owner_email})"} if d["key"] == "email" else {"note": "Contact us"})
    return out


def choose(tenant: Tenant, campaign: Campaign, picked: list[str]) -> None:
    """Remembers where the manager said this campaign goes. Refused now, not at send time, if any of it can't be reached."""
    if not picked:
        raise ValueError("pick where this campaign should go")
    if unknown := set(picked) - set(DESTINATIONS):
        raise ValueError(f"unknown destination: {', '.join(sorted(unknown))}")
    ready = {d["key"]: d["ready"] for d in available(tenant)}
    if off := [DESTINATIONS[k] for k in picked if not ready[k]]:
        raise ValueError(f"connect {', '.join(off)} first")
    keys = [k for k in DESTINATIONS if k in picked]
    campaign.channel = "messaging" if set(keys) & set(MESSAGING) else "social"
    campaign.stats = {**(campaign.stats or {}), "destinations": keys, "destination_names": [DESTINATIONS[k] for k in keys]}


def _route(tenant: Tenant, contact, only: list[str]) -> str | None:
    """The first chosen channel that reaches this customer; None sends nothing. Sample addresses stay in the app, as everywhere."""
    if ch := next((k for k in only if channels.pick(tenant, contact, k) == k), None):
        return ch
    return "console" if "email" in only and contact.email and channels.placeholder(contact.email) else None


def _broadcast(s, tenant: Tenant, campaign: Campaign, only: list[str] | None = None) -> dict:
    variants = campaign.variants or [{"label": "A", "text": campaign.draft or ""}]
    people = audience.segment_contacts(s, tenant, campaign.segment)
    holdout, targets = audience.split_holdout(people, campaign.segment.get("holdout_pct", setting(
        tenant, "marketing.holdout_pct")), campaign.id)
    sent = 0
    for c in targets:
        channel = _route(tenant, c, only) if only else None
        if only and not channel:
            continue
        v = audience.variant_for(c, variants)
        sent += channels.send(s, tenant, c, v["text"].replace("{name}", first_name(c.name)), "marketing", channel=channel,
                              via="campaign", image=campaign.image, campaign_id=campaign.id, variant=v["label"])
    return {"audience": len(people), "sent": sent, "blocked": len(targets) - sent, "holdout_ids": [c.id for c in holdout]}


def _publish(tenant: Tenant, campaign: Campaign, only: list[str] | None = None) -> dict:
    url = setting(tenant, "distribution.webhook_url")
    if not url:
        raise ValueError("social posts need playbook.distribution.webhook_url (Postiz, Zapier, Make, n8n...)")
    variants = campaign.variants or [{"label": "A", "text": campaign.draft or ""}]
    payload = {"tenant": tenant.name, "campaign_id": campaign.id, "name": campaign.name, "channel": "social",
               "destinations": only or [], "text": variants[0]["text"].replace("{name}", first_name(None)), "variants": variants,
               "image_brief": campaign.image_brief,
               "image_url": f"{PUBLIC_BASE_URL.rstrip('/')}/media/{campaign.image}" if campaign.image else None}
    r = httpx.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return {"posted": True, "webhook_status": r.status_code, "posted_to": [DESTINATIONS[k] for k in only or []]}


def _test_to_owner(s, tenant: Tenant, campaign: Campaign) -> dict:
    """A free company's campaign: one copy to its own proved address, so it can see the result. No customer receives anything."""
    me = Contact(tenant_id=tenant.id, name="there", email=tenant.owner_email)
    variants = campaign.variants or [{"text": campaign.draft or ""}]
    text = "Test copy. Only you received this.\n\n" + variants[0]["text"].replace("{name}", first_name(None))
    try:
        if channels.pick(tenant, me, "email") != "email":
            raise ValueError("email isn't available for test copies yet")
        if reason := channels.email_cap(s, tenant, me, "email"):
            raise ValueError(reason)
        channels.deliver(tenant, me, text, campaign.image, "email")
    except (ValueError, *channels.DELIVERY_ERRORS) as e:
        campaign.status, campaign.stats = "failed", {**(campaign.stats or {}), "error": str(e)}
        return {"campaign": campaign.id, "status": "failed", "error": str(e)}
    log_activity(s, tenant.id, None, "msg_out", "test", "email", kind="test")
    campaign.status, campaign.sent_at = "test_sent", now()
    campaign.stats = {**(campaign.stats or {}), "test_to": tenant.owner_email}
    return {"campaign": campaign.id, "status": "test_sent", "test_to": tenant.owner_email}


def dispatch(s, tenant: Tenant, campaign: Campaign) -> dict:
    if plan.is_trial(tenant):
        return _test_to_owner(s, tenant, campaign)
    try:
        picked = (campaign.stats or {}).get("destinations")
        if picked is None:   # no person chose: campaigns approved before destinations existed, and the API
            stats = _publish(tenant, campaign) if campaign.channel == "social" else _broadcast(s, tenant, campaign)
        else:   # the post first: if it fails nothing has reached customers, so the campaign can safely be sent again
            posts, messages = [k for k in picked if k not in MESSAGING], [k for k in picked if k in MESSAGING]
            stats = {**(_publish(tenant, campaign, posts) if posts else {}), **(_broadcast(s, tenant, campaign, messages) if messages else {})}
    except (httpx.HTTPError, ValueError) as e:
        campaign.status, campaign.stats = "failed", {**(campaign.stats or {}), "error": str(e)}
        log.warning("campaign %s failed: %s", campaign.id, e)
        return {"campaign": campaign.id, "status": "failed", "error": str(e)}
    campaign.status, campaign.sent_at = "sent", now()
    campaign.stats = {**(campaign.stats or {}), **stats}
    return {"campaign": campaign.id, "status": "sent", **stats}


def dispatch_due(s, tenant: Tenant, at: datetime) -> list[dict]:
    """Scheduled campaigns whose send time has come."""
    due = s.scalars(select(Campaign).where(Campaign.tenant_id == tenant.id, Campaign.status == "scheduled",
                                           Campaign.send_at <= at)).all()
    return [dispatch(s, tenant, c) for c in due]
