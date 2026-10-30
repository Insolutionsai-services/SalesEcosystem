"""Pending payments: won orders that are not fully paid, and the reminders that chase them (straight from the database)."""
from datetime import datetime, timedelta

from sqlalchemy import func, select

from ..core.models import Activity, Contact, Quote, Tenant
from ..core.playbook import setting
from ..core.utils import to_local
from . import channels, compliance
from .quotes import PAID_SLACK


def _reminders(s, tid: int, ids: list[int]) -> dict[int, int]:
    """Reminders already tried per order. Failed attempts count too, so a broken channel is not retried every tick."""
    quote = Activity.data["quote_id"].as_integer()
    rows = s.execute(select(quote, func.count()).where(
        Activity.tenant_id == tid, Activity.type.in_(("msg_out", "failed")), Activity.data["kind"].as_string() == "reminder",
        quote.in_(ids)).group_by(quote))
    return dict(rows.all())


def pending(s, tenant: Tenant, at: datetime, limit: int | None = None) -> list[dict]:
    """Oldest first: who owes what, for how long, how many reminders went out and when the next one is planned."""
    due = Quote.total - func.coalesce(Quote.paid_amount, 0)
    since = func.coalesce(Quote.closed_at, Quote.created_at)
    rows = s.execute(select(Quote, Contact.name, since).join(Contact, Contact.id == Quote.contact_id).where(
        Quote.tenant_id == tenant.id, Quote.status == "won", due > PAID_SLACK).order_by(since).limit(limit)).all()
    sent, days = _reminders(s, tenant.id, [q.id for q, *_ in rows]), setting(tenant, "payment_reminder_days")
    return [{"quote_id": q.id, "contact_id": q.contact_id, "customer": name, "total": q.total, "paid": q.paid_amount or 0,
             "balance": round(q.total - (q.paid_amount or 0), 2), "since": won_at.isoformat(), "age_days": max(0, (at - won_at).days),
             "reminders": sent.get(q.id, 0),
             "next_reminder": (won_at + timedelta(days=days[sent.get(q.id, 0)])).isoformat() if sent.get(q.id, 0) < len(days) else None}
            for q, name, won_at in rows]


def remind_due(s, tenant: Tenant, at: datetime) -> int:
    """One message per order whose next planned reminder has come (playbook.payment_reminder_days after the order was won)."""
    if compliance.in_quiet_hours(tenant, to_local(at, setting(tenant, "timezone")).hour):
        return 0
    cur = setting(tenant, "currency")
    sent = 0
    for row in pending(s, tenant, at):
        if not row["next_reminder"] or datetime.fromisoformat(row["next_reminder"]) > at:
            continue
        contact = s.get(Contact, row["contact_id"])
        if contact.opted_out:
            continue
        text = (f"Hi {contact.name or 'there'}, a gentle reminder from {tenant.name}: {cur} {row['balance']:,.0f} is still pending "
                f"for order #{row['quote_id']} (total {cur} {row['total']:,.0f}, paid {cur} {row['paid']:,.0f}). "
                "Please pay at your convenience, or reply here if you have already paid.")
        sent += channels.send_unchecked(s, tenant, contact, text, "reminder", via="system", quote_id=row["quote_id"])
    return sent
