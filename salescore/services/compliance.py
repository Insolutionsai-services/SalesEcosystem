"""Compliance gate. Plain code, never an LLM: every outbound message passes here or is not sent."""
from datetime import datetime, timedelta

from sqlalchemy import func, select

from ..core.models import Activity, Consent, Contact, Tenant
from ..core.playbook import setting
from ..core.utils import hour_in_window, to_local

OPT_OUT_WORDS = {"stop", "unsubscribe", "opt out", "optout", "stop all", "cancel"}
OPT_IN_WORDS = {"start", "subscribe"}
SERVICE_WINDOW = timedelta(hours=24)  # WhatsApp customer-service window
TEMPORARY_BLOCKS = {"quiet hours", "frequency cap"}  # retry later; everything else is final


def _keyword(text: str) -> str:
    return text.strip().lower().strip(".!")


def is_opt_out(text: str) -> bool:
    return _keyword(text) in OPT_OUT_WORDS


def is_opt_in(text: str) -> bool:
    return _keyword(text) in OPT_IN_WORDS


def has_consent(s, contact: Contact) -> bool:
    latest = s.scalars(select(Consent).where(Consent.contact_id == contact.id)
                       .order_by(Consent.at.desc(), Consent.id.desc()).limit(1)).first()
    return bool(latest and latest.granted)


def in_service_window(contact: Contact, at: datetime) -> bool:
    return bool(contact.last_inbound_at and at - contact.last_inbound_at <= SERVICE_WINDOW)


def proactive_sent_since(s, contact_id: int, since: datetime) -> int:
    return s.scalar(select(func.count()).select_from(Activity).where(
        Activity.contact_id == contact_id, Activity.type == "msg_out",
        Activity.data["kind"].as_string() != "reply", Activity.at >= since))


def check(s, tenant: Tenant, contact: Contact, kind: str, at: datetime) -> str | None:
    """kind: reply | followup | marketing. Returns a block reason, or None if the send is allowed."""
    if contact.opted_out:
        return "opted out"
    if kind == "reply" and in_service_window(contact, at):
        return None  # answering the customer's own message is always allowed

    start, end = setting(tenant, "compliance.quiet_hours")
    if hour_in_window(to_local(at, setting(tenant, "timezone")).hour, start, end):
        return "quiet hours"

    consent = has_consent(s, contact)
    if kind == "marketing" and not consent:
        return "no marketing consent"
    if kind in ("followup", "reply") and not (consent or contact.last_inbound_at):
        return "no consent and never enquired"

    if proactive_sent_since(s, contact.id, at - timedelta(days=7)) >= setting(tenant, "compliance.max_proactive_per_week"):
        return "frequency cap"
    return None
