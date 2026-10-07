"""Inbound: a customer messages us on any channel -> AI reply (or a human, after hand-off)."""
from ..ai import agent, fastpath, llm
from ..analytics.metrics import score_contact
from ..core import agents
from ..core.models import Contact, Tenant
from ..core.playbook import setting
from ..core.utils import now
from ..services import channels, compliance, contacts
from ..services.activity import log_activity, seen_external_id
from ..services.tasks import close_open_tasks, has_open_task
from .followups import schedule_followup


def handle_inbound(s, tenant: Tenant, text: str, phone=None, email=None, name=None, channel=None,
                   ext_id: str | None = None) -> str | None:
    """Returns the reply sent, or None when nothing was sent (duplicate delivery, or a human owns the chat)."""
    if ext_id and seen_external_id(s, tenant.id, ext_id):
        return None
    contact = contacts.find_or_create_contact(s, tenant, phone, email, name, source=channel or "inbound")
    contact.last_inbound_at = now()
    log_activity(s, tenant.id, contact.id, "msg_in", body=text, channel=channel, ext_id=ext_id)
    close_open_tasks(s, contact.id, "followup", status="cancelled")  # they replied: the "no reply" cadence stops

    if compliance.is_opt_out(text):
        contacts.set_opt_out(s, tenant, contact, True, source=f"customer said: {text}")
        reply = setting(tenant, "opt_out_reply")
        channels.send_unchecked(s, tenant, contact, reply, "reply")  # confirmation must bypass the gate
        return reply
    if compliance.is_opt_in(text):
        contacts.set_opt_out(s, tenant, contact, False, source=f"customer said: {text}")
    if contact.bot_paused:
        return None  # a human owns this conversation; it shows in their queue via the activity log
    if not agents.enabled(tenant, "response"):  # replies switched off for this company: a person answers
        if not has_open_task(s, contact.id, "handoff"):
            contacts.handoff(s, tenant, contact, "Response agent is off: please reply", pause_bot=False)
        return None

    reply, via = answer(s, tenant, contact, text)
    if channels.send(s, tenant, contact, reply, "reply", via) and not contact.bot_paused:
        schedule_followup(s, tenant, contact, step=0)
    score_contact(s, tenant, contact)  # real-time P(win) with the trained model
    return reply


def answer(s, tenant: Tenant, contact: Contact, text: str) -> tuple[str, str]:
    """Trained local models first (free, instant); the LLM only when they are not confident."""
    if fast := fastpath.answer(s, tenant, contact, text):
        if fast.handoff:
            contacts.handoff(s, tenant, contact, "customer asked for a person")
        return fast.text, "fastpath"
    try:
        reply = agent.run(s, tenant, "reply", text, contact)
    except llm.LLMUnavailable as e:  # provider down / no key: a human picks it up, the AI continues once it's back
        contacts.handoff(s, tenant, contact, f"AI unavailable: {e}", pause_bot=False)
        return setting(tenant, "handoff_reply"), "fallback"
    if not reply:  # model gave nothing usable: never leave the customer hanging
        contacts.handoff(s, tenant, contact, "assistant could not answer")
        return setting(tenant, "handoff_reply"), "llm"
    return reply, "llm"


def human_reply(s, tenant: Tenant, contact: Contact, text: str, resume_bot: bool) -> bool:
    """A salesperson answers from the hand-off queue; optionally gives the conversation back to the AI."""
    sent = channels.send(s, tenant, contact, text, "reply", via="human")
    if resume_bot:
        contact.bot_paused = False
        close_open_tasks(s, contact.id, "handoff")
    return sent
