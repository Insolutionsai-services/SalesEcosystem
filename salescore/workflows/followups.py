"""Proactive outreach: first touch for new leads, capped follow-up cadence, reorder nudges."""
from datetime import datetime, timedelta

from ..ai import agent, llm
from ..core import agents
from ..core.models import Contact, Task, Tenant
from ..core.playbook import setting
from ..core.utils import now
from ..services import channels, compliance
from ..services.activity import has_activity
from ..services.tasks import add_task, due_tasks, has_open_task

JOB_GATE_KIND = {"followup": "followup", "first_touch": "marketing", "reorder": "marketing"}
RETRY_AFTER = timedelta(hours=1)


def schedule_followup(s, tenant: Tenant, contact: Contact, step: int) -> None:
    """Step n of playbook.followup_days; stops when the cadence runs out."""
    days = setting(tenant, "followup_days")
    if step < len(days) and not has_open_task(s, contact.id, "followup"):
        add_task(s, tenant.id, contact.id, "followup", now() + timedelta(days=days[step]), step=step)


def schedule_first_touch(s, tenant: Tenant, new_contacts: list[Contact]) -> None:
    for c in new_contacts:
        never_contacted = c.last_inbound_at is None and not has_activity(s, c.id, "msg_out")
        if never_contacted and not has_open_task(s, c.id, "first_touch"):
            add_task(s, tenant.id, c.id, "first_touch")


def _run_one(s, tenant: Tenant, task: Task, at: datetime) -> bool:
    task.status = "done"
    contact = s.get(Contact, task.contact_id)
    if contact is None or contact.opted_out or contact.bot_paused:
        return False
    gate_kind = JOB_GATE_KIND[task.kind]
    if reason := compliance.check(s, tenant, contact, gate_kind, at):  # before the LLM call: don't pay for a blocked send
        if reason in compliance.TEMPORARY_BLOCKS:
            task.status, task.due_at = "open", at + RETRY_AFTER
        return False
    try:
        text = agent.run(s, tenant, task.kind, f"Task: {task.kind}. {task.data.get('note', '')}", contact)
    except llm.LLMUnavailable:  # provider down or not configured: keep the task, try again later
        task.status, task.due_at = "open", at + RETRY_AFTER
        return False
    if agent.is_skip(text) or not channels.send(s, tenant, contact, text, gate_kind, via="llm"):
        return False
    if task.kind == "followup":
        schedule_followup(s, tenant, contact, task.data.get("step", 0) + 1)
    return True


def run_due(s, tenant: Tenant, at: datetime) -> dict:
    if not agents.enabled(tenant, "response"):  # follow-ups belong to the Response agent; they wait while it's off
        return {"tasks_run": 0, "messages_sent": 0}
    due = due_tasks(s, tenant.id, JOB_GATE_KIND, at)
    return {"tasks_run": len(due), "messages_sent": sum(_run_one(s, tenant, t, at) for t in due)}
