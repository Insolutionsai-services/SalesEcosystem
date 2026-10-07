"""Analytics agent (campaign side): measures what each campaign actually did - replies, quotes, sales and revenue
within the attribution window, per A/B variant and against the holdout group. Strategy reads these results
to plan the next cycle, which closes the loop."""
from datetime import timedelta

from sqlalchemy import select

from ..core.models import Activity, Campaign, Quote, Tenant
from ..core.playbook import setting
from ..core.utils import now

MIN_PER_VARIANT = 20  # below this, an A/B "winner" is noise


def _outcomes(s, tenant_id: int, contact_ids: set[int], start, end) -> dict:
    if not contact_ids:
        return {"n": 0, "replied": 0, "quoted": 0, "won": 0, "revenue": 0.0}
    replied = set(s.scalars(select(Activity.contact_id).where(
        Activity.tenant_id == tenant_id, Activity.type == "msg_in", Activity.contact_id.in_(contact_ids),
        Activity.at > start, Activity.at <= end)))
    quotes = s.scalars(select(Quote).where(Quote.tenant_id == tenant_id, Quote.contact_id.in_(contact_ids),
                                           Quote.created_at > start, Quote.created_at <= end)).all()
    won = [q for q in quotes if q.status == "won"]
    return {"n": len(contact_ids), "replied": len(replied), "quoted": len({q.contact_id for q in quotes}),
            "won": len({q.contact_id for q in won}), "revenue": round(sum(q.total for q in won), 2)}


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def measure(s, tenant: Tenant, campaign: Campaign) -> dict:
    if not campaign.sent_at or campaign.channel == "social":
        return campaign.results or {}
    days = setting(tenant, "marketing.attribution_days")
    start, end = campaign.sent_at, campaign.sent_at + timedelta(days=days)
    rows = s.scalars(select(Activity).where(Activity.tenant_id == tenant.id, Activity.type == "msg_out",
                                            Activity.data["campaign_id"].as_integer() == campaign.id)).all()
    by_variant: dict[str, set[int]] = {}
    for a in rows:
        by_variant.setdefault(a.data.get("variant", "A"), set()).add(a.contact_id)
    everyone = set().union(*by_variant.values()) if by_variant else set()
    total = _outcomes(s, tenant.id, everyone, start, end)
    holdout = _outcomes(s, tenant.id, set((campaign.stats or {}).get("holdout_ids", [])), start, end)
    variants = {label: {**(o := _outcomes(s, tenant.id, ids, start, end)), "reply_rate": _rate(o["replied"], o["n"])}
                for label, ids in sorted(by_variant.items())}
    reply_rate, holdout_rate = _rate(total["replied"], total["n"]), _rate(holdout["replied"], holdout["n"])
    eligible = {k: v for k, v in variants.items() if v["n"] >= MIN_PER_VARIANT}
    campaign.results = {
        "window_days": days, "complete": now() >= end,
        "sent": total["n"], "replied": total["replied"], "quoted": total["quoted"], "won": total["won"],
        "revenue": total["revenue"], "reply_rate": reply_rate, "conversion_rate": _rate(total["won"], total["n"]),
        "holdout": {**holdout, "reply_rate": holdout_rate},
        "lift_reply_rate": round(reply_rate - holdout_rate, 4) if reply_rate is not None and holdout_rate is not None else None,
        "variants": variants,
        "winner": max(eligible, key=lambda k: eligible[k]["reply_rate"]) if len(eligible) > 1 else None,
        "measured_at": now().isoformat(timespec="seconds"),
    }
    return campaign.results


def refresh(s, tenant: Tenant, days: int = 45) -> int:
    """Re-measures campaigns sent recently (results settle over the attribution window)."""
    recent = s.scalars(select(Campaign).where(Campaign.tenant_id == tenant.id, Campaign.status == "sent",
                                              Campaign.sent_at >= now() - timedelta(days=days))).all()
    for c in recent:
        measure(s, tenant, c)
    return len(recent)
