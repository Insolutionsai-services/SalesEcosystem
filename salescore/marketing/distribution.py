"""Distribution agent: delivers an approved campaign - WhatsApp/email broadcast through the compliance gate
(A/B split + holdout), and social/ad posts to a webhook (Postiz, Zapier, Make, n8n, ...)."""
import logging
from datetime import datetime

import httpx
from sqlalchemy import select

from ..core.config import PUBLIC_BASE_URL
from ..core.models import Campaign, Tenant
from ..core.playbook import setting
from ..core.utils import first_name, now
from ..services import channels
from . import audience

log = logging.getLogger("salescore.distribution")
CHANNELS = ("messaging", "social")


def _broadcast(s, tenant: Tenant, campaign: Campaign) -> dict:
    variants = campaign.variants or [{"label": "A", "text": campaign.draft or ""}]
    people = audience.segment_contacts(s, tenant, campaign.segment)
    holdout, targets = audience.split_holdout(people, campaign.segment.get("holdout_pct", setting(
        tenant, "marketing.holdout_pct")), campaign.id)
    sent = 0
    for c in targets:
        v = audience.variant_for(c, variants)
        sent += channels.send(s, tenant, c, v["text"].replace("{name}", first_name(c.name)), "marketing",
                              via="campaign", image=campaign.image, campaign_id=campaign.id, variant=v["label"])
    return {"audience": len(people), "sent": sent, "blocked": len(targets) - sent, "holdout_ids": [c.id for c in holdout]}


def _publish(tenant: Tenant, campaign: Campaign) -> dict:
    url = setting(tenant, "distribution.webhook_url")
    if not url:
        raise ValueError("social posts need playbook.distribution.webhook_url (Postiz, Zapier, Make, n8n...)")
    variants = campaign.variants or [{"label": "A", "text": campaign.draft or ""}]
    payload = {"tenant": tenant.name, "campaign_id": campaign.id, "name": campaign.name, "channel": "social",
               "text": variants[0]["text"].replace("{name}", first_name(None)), "variants": variants,
               "image_brief": campaign.image_brief,
               "image_url": f"{PUBLIC_BASE_URL.rstrip('/')}/media/{campaign.image}" if campaign.image else None}
    r = httpx.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return {"posted": True, "webhook_status": r.status_code}


def dispatch(s, tenant: Tenant, campaign: Campaign) -> dict:
    try:
        stats = _publish(tenant, campaign) if campaign.channel == "social" else _broadcast(s, tenant, campaign)
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
