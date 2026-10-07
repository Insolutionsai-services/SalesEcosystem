"""The sales dashboard for one period: enquiries -> quotes -> orders -> payments, how they moved over time,
and what each agent did. Each window's rows are read once and counted in Python."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, select

from ..core.models import Activity, Campaign, Contact, Quote, Task, Tenant
from ..core.utils import now
from ..services.quotes import PAID_SLACK
from .metrics import ai_usage

WON_AT = func.coalesce(Quote.closed_at, Quote.created_at)  # orders imported before closed_at existed


def _between(col, start: datetime, end: datetime | None):
    """end=None: the window runs up to now, with no upper bound to race the clock."""
    return (col >= start) if end is None else (col >= start) & (col < end)


@dataclass
class Window:
    enquiries: list[tuple[int, datetime]]   # (contact, when) - wrote in or arrived as a lead
    quotes: list[tuple[datetime, float]]    # (created, total) - sent, not waiting for approval
    orders: list[tuple[Quote, datetime]]    # (order, won at)
    paid: list[tuple[float, float]]         # (paid amount, total) - latest payment in the window


def _window(s, tid: int, start: datetime, end: datetime | None) -> Window:
    wrote = s.execute(select(Activity.contact_id, Activity.at).where(
        Activity.tenant_id == tid, Activity.type == "msg_in", _between(Activity.at, start, end)))
    new = s.execute(select(Contact.id, Contact.created_at).where(Contact.tenant_id == tid, _between(Contact.created_at, start, end)))
    quotes = s.execute(select(Quote.created_at, Quote.total).where(
        Quote.tenant_id == tid, Quote.status != "pending_approval", _between(Quote.created_at, start, end)))
    orders = s.execute(select(Quote, WON_AT).where(Quote.tenant_id == tid, Quote.status == "won", _between(WON_AT, start, end)))
    # ponytail: a payment counts in the period of its latest instalment; keep a payments table if part-payments must split by date
    paid = s.execute(select(Quote.paid_amount, Quote.total).where(
        Quote.tenant_id == tid, Quote.paid_amount > 0, _between(Quote.paid_at, start, end)))
    return Window([(c, at) for c, at in [*wrote, *new] if c is not None], list(quotes), list(orders), list(paid))


def _kpis(w: Window) -> dict:
    order_value = sum(q.total for q, _ in w.orders)
    return {
        "enquiries": len({c for c, _ in w.enquiries}),
        "quotes": len(w.quotes), "quoted_value": round(sum(t for _, t in w.quotes), 2),
        "orders": len(w.orders), "order_value": round(order_value, 2),
        "paid": round(sum(p for p, _ in w.paid), 2), "paid_orders": sum(p >= t - PAID_SLACK for p, t in w.paid),
        "avg_order": round(order_value / len(w.orders), 2) if w.orders else None,
        "order_rate": round(len(w.orders) / len(w.quotes), 3) if w.quotes else None,
    }


def _trend(w: Window, start: datetime, at_now: datetime, days: int) -> dict:
    step = 1 if days <= 14 else 7 if days <= 120 else 30  # sparse daily counts read as noise; a month reads best by week
    n = -(-days // step)
    idx = lambda at: min(n - 1, max(0, n - 1 - (at_now - at).days // step))  # noqa: E731  -- whole buckets end today
    people = [set() for _ in range(n)]  # enquiries are people, as on the tile
    for c, at in w.enquiries:
        people[idx(at)].add(c)
    quotes, orders = [0] * n, [0] * n
    for at, _ in w.quotes:
        quotes[idx(at)] += 1
    for _, at in w.orders:
        orders[idx(at)] += 1
    labels = [max(start, at_now - timedelta(days=(n - i) * step)).date().isoformat() for i in range(n)]
    return {"bucket_days": step, "labels": labels, "enquiries": [len(p) for p in people], "quotes": quotes, "orders": orders}


def _agents(s, tid: int, start: datetime) -> dict:
    usage = ai_usage(s, tid, start)
    task_rows = s.execute(select(Task.kind, Task.data).where(Task.tenant_id == tid, Task.created_at >= start)).all()
    tasks = Counter(kind for kind, _ in task_rows)
    made = s.scalar(select(func.count()).select_from(Quote).where(Quote.tenant_id == tid, Quote.created_at >= start))
    blocked = s.scalar(select(func.count()).select_from(Activity).where(
        Activity.tenant_id == tid, Activity.type == "blocked", Activity.at >= start))
    res = [r or {} for r in s.scalars(select(Campaign.results).where(Campaign.tenant_id == tid, Campaign.sent_at >= start))]
    return {
        "replies": usage["replies"], "replies_instant": usage["replies_local"], "handoffs": tasks["handoff"],
        "quotes_made": made, "quotes_needed_approval": sum(k == "approval" and "quote_id" in (d or {}) for k, d in task_rows),
        "blocked": blocked, "followups": tasks["followup"],
        "campaigns": len(res), "campaign_reached": sum(r.get("sent", 0) for r in res),
        "campaign_replies": sum(r.get("replied", 0) for r in res), "campaign_orders": sum(r.get("won", 0) for r in res),
        "campaign_revenue": round(sum(r.get("revenue", 0) for r in res), 2),
        "llm_calls": usage["llm_calls"], "llm_cost_usd": usage["llm_cost_usd"],
    }


def report(s, tenant: Tenant, days: int | None = 30) -> dict:
    tid, at_now = tenant.id, now()
    if days is None:  # all time
        first = min(filter(None, [s.scalar(select(func.min(WON_AT)).where(Quote.tenant_id == tid)),
                                  s.scalar(select(func.min(Contact.created_at)).where(Contact.tenant_id == tid))]), default=at_now)
        start = datetime.combine(first.date(), datetime.min.time())
        span = max(1, (at_now - start).days + 1)
    else:
        span, start = days, at_now - timedelta(days=days)
    w = _window(s, tid, start, None)
    due = func.coalesce(Quote.paid_amount, 0)
    outstanding, unpaid = s.execute(select(func.sum(Quote.total - due), func.sum(case((due < Quote.total - PAID_SLACK, 1), else_=0)))
                                    .where(Quote.tenant_id == tid, Quote.status == "won")).one()
    products, sources = defaultdict(lambda: {"value": 0.0, "qty": 0.0}), defaultdict(lambda: {"enquiries": 0, "orders": 0})
    for q, _ in w.orders:
        for ln in q.lines:
            products[ln["name"]]["value"] += ln["amount"]
            products[ln["name"]]["qty"] += ln["qty"]
    people = {c for c, _ in w.enquiries}
    source = dict(s.execute(select(Contact.id, Contact.source).where(Contact.id.in_(people | {q.contact_id for q, _ in w.orders}))).all())
    for c in people:
        sources[source.get(c) or "unknown"]["enquiries"] += 1
    for q, _ in w.orders:
        sources[source.get(q.contact_id) or "unknown"]["orders"] += 1
    return {
        "days": days, "start": start.isoformat(), "end": at_now.isoformat(),
        "current": _kpis(w),
        "previous": _kpis(_window(s, tid, start - timedelta(days=span), start)) if days else None,
        "outstanding": round(outstanding or 0, 2), "unpaid_orders": unpaid or 0,
        "trend": _trend(w, start, at_now, span),
        "agents": _agents(s, tid, start),
        "products": sorted(({"name": k, **{f: round(v, 2) for f, v in d.items()}} for k, d in products.items()),
                           key=lambda p: -p["value"])[:6],
        "sources": sorted(({"source": k, **d} for k, d in sources.items()), key=lambda r: -(r["enquiries"] + r["orders"]))[:6],
    }
