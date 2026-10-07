"""The event log: every message, quote, block and stage change. Analytics and ML read from here."""
from sqlalchemy import select

from ..core.models import Activity


def log_activity(s, tenant_id: int, contact_id: int | None, type_: str, body: str | None = None,
                 channel: str | None = None, **data) -> None:
    s.add(Activity(tenant_id=tenant_id, contact_id=contact_id, type=type_, body=body, channel=channel, data=data))


def has_activity(s, contact_id: int, type_: str) -> bool:
    return s.scalars(select(Activity.id).where(Activity.contact_id == contact_id, Activity.type == type_)).first() is not None


def seen_external_id(s, tenant_id: int, ext_id: str) -> bool:
    """True if a provider message id was already processed (webhook redelivery)."""
    return s.scalars(select(Activity.id).where(Activity.tenant_id == tenant_id, Activity.type == "msg_in",
                                               Activity.data["ext_id"].as_string() == ext_id)).first() is not None


def timeline(s, contact_id: int, limit: int = 200, types: list[str] | None = None) -> list[Activity]:
    """The latest `limit` events for one contact (optionally only some types), oldest first."""
    q = select(Activity).where(Activity.contact_id == contact_id)
    if types:
        q = q.where(Activity.type.in_(types))
    rows = s.scalars(q.order_by(Activity.at.desc(), Activity.id.desc()).limit(limit)).all()
    return list(reversed(rows))


def recent_messages(s, contact_id: int, limit: int = 20) -> list[Activity]:
    return timeline(s, contact_id, limit, types=["msg_in", "msg_out"])
