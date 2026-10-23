"""Analytics agent (campaign side): measures what each campaign actually did - replies, quotes, sales and revenue
within the attribution window, per A/B variant and against the holdout group. Strategy reads these results
to plan the next cycle, which closes the loop."""
import re
from datetime import timedelta

from sqlalchemy import select

from ..core.models import Activity, Campaign, Contact, Quote, Tenant
from ..core.playbook import setting
from ..core.utils import now
from ..ml import store

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


STOP = re.compile(r"\b(stop|unsubscribe|not interested|don'?t (message|text|call)|remove me|no thanks)\b", re.IGNORECASE)
ORDER = {"hot": 0, "warm": 1, "low": 2, "not_interested": 3}


def _interest(text: str, intent: str, opted_out: bool, quoted: bool) -> str:
    """Deterministic read of a reply: who is worth a human's time first. The LLM is not involved."""
    if opted_out or STOP.search(text or ""):
        return "not_interested"
    if quoted or intent in ("price", "stock", "human"):
        return "hot"          # asked a price / availability / for a person, or already has a quote
    if intent == "thanks":
        return "low"
    return "warm"             # greeting or a real question that needs a person ("other")


def interested(s, tenant: Tenant, by_variant: dict, start, end) -> list[dict]:
    """Everyone who replied to the campaign, best prospects first: who, what they said, how interested."""
    variant_of = {cid: label for label, ids in by_variant.items() for cid in ids}
    if not variant_of:
        return []
    replies = {}
    for a in s.scalars(select(Activity).where(
            Activity.tenant_id == tenant.id, Activity.type == "msg_in", Activity.contact_id.in_(variant_of),
            Activity.at > start, Activity.at <= end).order_by(Activity.at)):
        replies[a.contact_id] = a        # keeps each contact's latest reply
    quoted = {q.contact_id for q in s.scalars(select(Quote).where(
        Quote.tenant_id == tenant.id, Quote.contact_id.in_(replies), Quote.created_at > start))}
    brain = store.load(tenant.id)
    out = []
    for c in s.scalars(select(Contact).where(Contact.id.in_(replies))):
        text = replies[c.id].body or ""
        intent = brain.intents.predict(text)[0] if brain and text else "other"
        out.append({"contact_id": c.id, "name": c.name, "phone": c.phone, "email": c.email, "variant": variant_of[c.id],
                    "replied_at": replies[c.id].at.isoformat(timespec="seconds"), "reply": text[:200],
                    "intent": intent, "interest": _interest(text, intent, c.opted_out, c.id in quoted),
                    "stage": c.stage, "score": c.score})
    return sorted(out, key=lambda r: (ORDER[r["interest"]], -(r["score"] or 0)))


def recipients(s, tenant: Tenant, campaign: Campaign) -> list[dict]:
    """Everyone the campaign was sent to, with their interest if they replied ("no_reply" otherwise), best prospects first."""
    if not campaign.sent_at or campaign.channel == "social":
        return []
    start, end = campaign.sent_at, campaign.sent_at + timedelta(days=setting(tenant, "marketing.attribution_days"))
    by_variant: dict[str, set[int]] = {}
    for a in s.scalars(select(Activity).where(Activity.tenant_id == tenant.id, Activity.type == "msg_out",
                                              Activity.data["campaign_id"].as_integer() == campaign.id)):
        by_variant.setdefault(a.data.get("variant", "A"), set()).add(a.contact_id)
    replied = {p["contact_id"]: p for p in interested(s, tenant, by_variant, start, end)}
    variant_of = {cid: label for label, ids in by_variant.items() for cid in ids}
    out = []
    for c in s.scalars(select(Contact).where(Contact.id.in_(set().union(*by_variant.values()) if by_variant else set()))):
        r = replied.get(c.id, {})
        out.append({"contact_id": c.id, "name": c.name, "phone": c.phone, "email": c.email, "interest": r.get("interest", "no_reply"),
                    "variant": variant_of[c.id], "reply": r.get("reply"), "replied_at": r.get("replied_at"), "score": c.score})
    return sorted(out, key=lambda r: (ORDER.get(r["interest"], 4), -(r["score"] or 0)))


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
        "no_reply": total["n"] - total["replied"],
        "interested": (people := interested(s, tenant, by_variant, start, end)),
        "interest_counts": {k: sum(p["interest"] == k for p in people) for k in ORDER},
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
