"""Strategy agent: decides WHO to target, with WHAT offer, on which CHANNEL and WHEN - from the company's data and
the measured results of past campaigns (the Analytics -> Strategy feedback loop). Pure data, no LLM, $0."""
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from ..core import agents
from ..core.models import Activity, Campaign, Item, Quote, Tenant
from ..core.playbook import setting
from ..core.utils import hour_in_window, now, to_local, to_utc_naive
from . import audience

PRIOR_REPLY_RATE, PRIOR_CONVERSION_RATE, PRIOR_WEIGHT = 0.08, 0.02, 20  # beliefs before a play has history
DEFAULT_SEND_HOUR = 11
ACTIVE_STATUSES = ("pending_approval", "needs_fix", "scheduled")


@dataclass
class Play:
    key: str
    name: str
    goal: str
    offer: str
    segment: dict
    audience: int = 0
    reachable: int = 0
    reply_rate: float = PRIOR_REPLY_RATE
    conversion_rate: float = PRIOR_CONVERSION_RATE
    expected_replies: float = 0.0
    expected_revenue: float = 0.0
    send_at: datetime | None = None
    channel: str = "messaging"
    why: str = ""
    skus: list[str] = field(default_factory=list)
    learned_from: int = 0  # past campaigns of this play used for the estimate

    def to_dict(self) -> dict:
        return asdict(self)


def _won_quotes(s, tenant_id: int) -> list[Quote]:
    return s.scalars(select(Quote).where(Quote.tenant_id == tenant_id, Quote.status == "won")).all()


def overstock(s, tenant: Tenant, since_days: int = 90) -> list[Item]:
    """Items whose stock is far above recent sales - worth promoting."""
    since, ratio = now() - timedelta(days=since_days), setting(tenant, "marketing.overstock_ratio")
    sold = Counter()
    for q in _won_quotes(s, tenant.id):
        if q.created_at >= since:
            for ln in q.lines:
                sold[ln["sku"]] += ln["qty"]
    items = s.scalars(select(Item).where(Item.tenant_id == tenant.id, Item.stock > 0)).all()
    return sorted((i for i in items if i.stock >= ratio * max(sold[i.sku], 1)), key=lambda i: -i.stock * i.price)


def best_send_time(s, tenant: Tenant, at: datetime) -> datetime:
    """Next occurrence of the local hour customers message most, outside quiet hours."""
    tz, (q_start, q_end) = setting(tenant, "timezone"), setting(tenant, "compliance.quiet_hours")
    hours = Counter(to_local(a.at, tz).hour for a in s.scalars(select(Activity).where(
        Activity.tenant_id == tenant.id, Activity.type == "msg_in", Activity.at >= at - timedelta(days=90))))
    ranked = [h for h, _ in hours.most_common()] + [DEFAULT_SEND_HOUR, 10, 12, 16, 18]
    hour = next(h for h in ranked if not hour_in_window(h, q_start, q_end))
    local = to_local(at, tz)
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return to_utc_naive(target)


def learned_rates(s, tenant_id: int, play: str) -> tuple[float, float, int]:
    """Reply and conversion rates from this play's measured campaigns, shrunk toward the priors."""
    done = [c.results for c in s.scalars(select(Campaign).where(Campaign.tenant_id == tenant_id, Campaign.play == play))
            if c.results and c.results.get("sent")]
    sent = sum(r["sent"] for r in done)
    replied = sum(r["replied"] for r in done)
    won = sum(r["won"] for r in done)
    return ((replied + PRIOR_REPLY_RATE * PRIOR_WEIGHT) / (sent + PRIOR_WEIGHT),
            (won + PRIOR_CONVERSION_RATE * PRIOR_WEIGHT) / (sent + PRIOR_WEIGHT), len(done))


def _candidate_plays(s, tenant: Tenant) -> list[Play]:
    disc = setting(tenant, "pricing.auto_approve_discount_pct")
    closed = [st for st in (setting(tenant, "stages") or []) if st in ("won", "lost")]
    plays = [
        Play("reward_champions", "Reward best customers", "Keep top customers buying and referring",
             "early access to new stock and priority delivery", {"segments": ["champions", "loyal"]},
             why="Top customers by recency, frequency and spend (RFM) respond best and refer others."),
        Play("win_back", "Win back lapsing customers", "Bring back customers who stopped buying",
             f"a {disc:g}% comeback discount on what they usually buy" if disc else "a personal check-in and free consultation",
             {"segments": ["at_risk", "hibernating"]},
             why="Customers who used to buy often but have gone quiet: cheaper to win back than to find new ones."),
        Play("hot_leads", "Close hot leads", "Convert leads the scoring model rates likely to buy",
             "help finalising their quote and a short price hold", {"min_score": 0.6, "exclude_stages": closed},
             why="The lead-scoring model gives these open leads a 60%+ chance to buy."),
        Play("new_leads", "Welcome new leads", "Start conversations with leads who haven't engaged",
             "a helpful intro and one useful tip", {"stages": ["new"]},
             why="New leads go cold fast; a first touch within days lifts conversion."),
    ]
    for item in overstock(s, tenant)[:1]:
        plays.append(Play("clear_overstock", f"Clear {item.name}", f"Sell down excess stock of {item.name}",
                          f"{item.name}" + (f" at {disc:g}% off while stock lasts" if disc else " - ready stock, fast delivery"),
                          {}, skus=[item.sku],
                          why=f"{item.stock:g} in stock vs slow recent sales: cash tied up in inventory."))
    return plays


def recommend(s, tenant: Tenant, limit: int = 5) -> list[Play]:
    """Ranked plays for this company right now (expected revenue first)."""
    at = now()
    cooldown = at - timedelta(days=setting(tenant, "marketing.cooldown_days"))
    busy = {c.play for c in s.scalars(select(Campaign).where(Campaign.tenant_id == tenant.id)) if c.play and (
        c.status in ACTIVE_STATUSES or (c.sent_at and c.sent_at >= cooldown))}
    won = _won_quotes(s, tenant.id)
    avg_order = sum(q.total for q in won) / len(won) if won else 0.0
    send_at = best_send_time(s, tenant, at)

    if not agents.enabled(tenant, "strategy"):
        return []
    allowed = set(setting(tenant, "marketing.plays"))
    ranked = []
    for play in _candidate_plays(s, tenant):
        if play.key not in allowed:
            continue
        if play.key in busy:
            continue
        people = audience.segment_contacts(s, tenant, play.segment)
        play.audience, play.reachable = len(people), len(audience.reachable(s, people))
        if not play.reachable:
            continue
        play.reply_rate, play.conversion_rate, play.learned_from = learned_rates(s, tenant.id, play.key)
        play.expected_replies = round(play.reachable * play.reply_rate, 1)
        play.expected_revenue = round(play.reachable * play.conversion_rate * avg_order, 2)
        play.send_at = send_at
        ranked.append(play)
    return sorted(ranked, key=lambda p: (-p.expected_revenue, -p.expected_replies))[:limit]
