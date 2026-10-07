"""Audiences: the standard customer criteria every company can use, the holdout group and A/B assignment.

Criteria (all optional; a customer must match every criterion given; several values inside one criterion = any of them):
  segments                  RFM: champions, loyal, new, at_risk, hibernating, regular, never_bought
  stages / exclude_stages   pipeline stages from the playbook
  min_score                 lead score 0..1 (0.6 = 60% chance to buy)
  attrs                     customer details from imports, e.g. {"city": ["Chennai", "Madurai"]}
  sources                   where the lead came from, e.g. ["website_form", "meta_lead_ad"]
  bought_skus               bought any of these products
  bought_within_days        last purchase within N days
  not_bought_for_days       no purchase for at least N days (and has bought before)
  min_spend                 lifetime spend at least this much
"""
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from ..core.models import Contact, Item, Quote, Tenant
from ..core.playbook import setting
from ..core.utils import now
from ..services import compliance

SEGMENTS = ["champions", "loyal", "new", "regular", "at_risk", "hibernating", "never_bought"]
MAX_DETAIL_VALUES = 40  # a detail with more distinct values (e.g. address) isn't a useful filter


@dataclass
class Purchases:
    last: datetime | None = None
    spend: float = 0.0
    skus: set = field(default_factory=set)


def purchase_index(s, tenant_id: int) -> dict[int, Purchases]:
    """Each customer's buying history from won orders, built once per audience check."""
    index: dict[int, Purchases] = {}
    for q in s.scalars(select(Quote).where(Quote.tenant_id == tenant_id, Quote.status == "won")):
        p = index.setdefault(q.contact_id, Purchases())
        p.last = max(p.last, q.created_at) if p.last else q.created_at
        p.spend += q.total
        p.skus.update(ln["sku"] for ln in q.lines)
    return index


def _values(v) -> list[str]:
    items = v if isinstance(v, list) else [v]
    return [str(x).strip().lower() for x in items if str(x).strip()]


def matches(contact: Contact, seg: dict, buys: Purchases | None = None, at: datetime | None = None) -> bool:
    buys, at = buys or Purchases(), at or now()
    if seg.get("segments"):
        own = contact.segment or ("never_bought" if not buys.last else "regular")
        if own not in seg["segments"]:
            return False
    if seg.get("stages") and contact.stage not in seg["stages"]:
        return False
    if contact.stage in seg.get("exclude_stages", []):
        return False
    if (contact.score or 0) < seg.get("min_score", 0):
        return False
    for key, wanted in (seg.get("attrs") or {}).items():
        if wanted and str(contact.attrs.get(key, "")).strip().lower() not in _values(wanted):
            return False
    if seg.get("sources") and (contact.source or "").lower() not in _values(seg["sources"]):
        return False
    if seg.get("bought_skus") and not buys.skus & set(seg["bought_skus"]):
        return False
    if seg.get("bought_within_days") and not (buys.last and buys.last >= at - timedelta(days=seg["bought_within_days"])):
        return False
    if seg.get("not_bought_for_days") and not (buys.last and buys.last < at - timedelta(days=seg["not_bought_for_days"])):
        return False
    if seg.get("min_spend") and buys.spend < seg["min_spend"]:
        return False
    return True


def segment_contacts(s, tenant: Tenant, seg: dict) -> list[Contact]:
    # ponytail: filters in Python; push into SQL when a tenant passes ~100k contacts
    index, at = purchase_index(s, tenant.id), now()
    candidates = s.scalars(select(Contact).where(Contact.tenant_id == tenant.id, Contact.opted_out.is_(False)))
    return [c for c in candidates if matches(c, seg, index.get(c.id), at)]


def reachable(s, contacts: list[Contact]) -> list[Contact]:
    """Contacts a marketing message may legally go to (recorded consent)."""
    return [c for c in contacts if compliance.has_consent(s, c)]


def preview(s, tenant: Tenant, seg: dict) -> dict:
    """Live count for the audience builder."""
    people = segment_contacts(s, tenant, seg)
    ok = reachable(s, people)
    holdout = int(len(ok) * seg.get("holdout_pct", setting(tenant, "marketing.holdout_pct")) / 100)
    return {"matched": len(people), "reachable": len(ok), "no_consent": len(people) - len(ok),
            "will_receive": len(ok) - holdout, "holdout": holdout,
            "sample": [c.name or c.phone or c.email for c in ok[:6]]}


def options(s, tenant: Tenant) -> dict:
    """The choices the audience builder offers, taken from this company's own data, with counts."""
    contacts = s.scalars(select(Contact).where(Contact.tenant_id == tenant.id, Contact.opted_out.is_(False))).all()
    index = purchase_index(s, tenant.id)
    segs = Counter(c.segment or ("never_bought" if c.id not in index else "regular") for c in contacts)
    stages = Counter(c.stage for c in contacts)
    details: dict[str, Counter] = {}
    for c in contacts:
        for k, v in c.attrs.items():
            if str(v).strip():
                details.setdefault(k, Counter())[str(v).strip()] += 1
    bought = Counter(sku for p in index.values() for sku in p.skus)
    names = {i.sku: i.name for i in s.scalars(select(Item).where(Item.tenant_id == tenant.id))}
    return {
        "segments": [{"value": k, "count": segs.get(k, 0)} for k in SEGMENTS],
        "stages": [{"value": k, "count": stages.get(k, 0)} for k in (setting(tenant, "stages") or sorted(stages))],
        "details": {k: [{"value": v, "count": n} for v, n in cnt.most_common()]
                    for k, cnt in sorted(details.items()) if len(cnt) <= MAX_DETAIL_VALUES},
        "sources": [{"value": k, "count": n} for k, n in Counter(c.source for c in contacts if c.source).most_common()],
        "products": [{"value": sku, "label": names.get(sku, sku), "count": n} for sku, n in bought.most_common()],
        "total": len(contacts),
    }


def split_holdout(audience: list[Contact], pct: float, seed: int) -> tuple[list[Contact], list[Contact]]:
    """(holdout, targets). Deterministic per campaign so the control group can be re-derived."""
    shuffled = sorted(audience, key=lambda c: c.id)
    random.Random(seed).shuffle(shuffled)
    n = int(len(shuffled) * pct / 100)
    return shuffled[:n], shuffled[n:]


def variant_for(contact: Contact, variants: list[dict]) -> dict:
    """Stable A/B assignment: the same contact always gets the same variant."""
    return variants[contact.id % len(variants)]
