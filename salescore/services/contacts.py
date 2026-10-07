"""Contacts: dedupe, qualification facts, pipeline stage, consent, hand-off to humans."""
from sqlalchemy import or_, select

from ..core.models import Consent, Contact, Quote, Tenant
from ..core.playbook import setting
from ..core.utils import is_truthy, norm_email, norm_phone, parse_csv
from .activity import log_activity, recent_messages
from .tasks import add_task


def find_or_create_contact(s, tenant: Tenant, phone=None, email=None, name=None, source=None) -> Contact:
    phone, email = norm_phone(phone, setting(tenant, "country_code")), norm_email(email)
    conditions = ([Contact.phone == phone] if phone else []) + ([Contact.email == email] if email else [])
    if not conditions:
        raise ValueError("contact needs a phone or email")
    contact = s.scalars(select(Contact).where(Contact.tenant_id == tenant.id, or_(*conditions))).first()
    if contact is None:
        contact = Contact(tenant_id=tenant.id, phone=phone, email=email, name=name, source=source, attrs={})
        s.add(contact)
        s.flush()
    else:  # merge: fill gaps, never overwrite
        contact.phone, contact.email, contact.name = contact.phone or phone, contact.email or email, contact.name or name
    return contact


def update_contact(s, tenant: Tenant, contact: Contact, stage: str | None = None, attrs: dict | None = None) -> None:
    if stage and stage != contact.stage:
        stages = setting(tenant, "stages")
        if stages and stage not in stages:
            raise ValueError(f"stage must be one of {stages}")
        log_activity(s, tenant.id, contact.id, "stage", body=f"{contact.stage} -> {stage}")
        contact.stage = stage
    if attrs:
        contact.attrs = {**contact.attrs, **attrs}  # reassign so SQLAlchemy sees the change


def record_consent(s, tenant: Tenant, contact: Contact, granted: bool, source: str) -> None:
    s.add(Consent(tenant_id=tenant.id, contact_id=contact.id, granted=granted, source=source))


def set_opt_out(s, tenant: Tenant, contact: Contact, opted_out: bool, source: str) -> None:
    contact.opted_out = opted_out
    record_consent(s, tenant, contact, granted=not opted_out, source=source)


def handoff(s, tenant: Tenant, contact: Contact, reason: str, pause_bot: bool = True) -> None:
    """Put a human in charge; with pause_bot the AI stops replying until a human resumes it."""
    contact.bot_paused = contact.bot_paused or pause_bot
    add_task(s, tenant.id, contact.id, "handoff", reason=reason)


def contact_profile(s, contact: Contact, history: int = 20) -> dict:
    """What the agent sees about a customer: facts, stage, quotes/orders, recent conversation."""
    quotes = s.scalars(select(Quote).where(Quote.contact_id == contact.id)
                       .order_by(Quote.created_at.desc()).limit(10)).all()
    return {
        "name": contact.name, "stage": contact.stage, "segment": contact.segment, "score": contact.score,
        "known_facts": contact.attrs,
        "quotes_and_orders": [{"id": q.id, "status": q.status, "total": q.total, "date": f"{q.created_at:%Y-%m-%d}",
                               "items": [f"{ln['name']} × {ln['qty']:g}" for ln in q.lines]} for q in quotes],
        "conversation": [f"{'customer' if a.type == 'msg_in' else 'us'}: {a.body}"
                         for a in recent_messages(s, contact.id, history)],
    }


def import_contacts(s, tenant: Tenant, text: str) -> list[Contact]:
    """CSV with phone and/or email; optional name, source, consent, stage; other columns become facts."""
    contacts = []
    for row in parse_csv(text):
        contact = find_or_create_contact(s, tenant, row.pop("phone", None), row.pop("email", None),
                                         row.pop("name", None) or None, row.pop("source", None) or "import")
        consent = row.pop("consent", "")
        if consent:
            record_consent(s, tenant, contact, is_truthy(consent), source=f"import:{contact.source}")
        update_contact(s, tenant, contact, stage=row.pop("stage", None) or None,
                       attrs={k: v for k, v in row.items() if v})
        contacts.append(contact)
    return contacts


def list_contacts(s, tenant_id: int, limit: int = 500) -> list[Contact]:
    return s.scalars(select(Contact).where(Contact.tenant_id == tenant_id)
                     .order_by(Contact.last_inbound_at.desc().nulls_last(), Contact.id.desc()).limit(limit)).all()
