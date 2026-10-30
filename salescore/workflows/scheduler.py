"""Periodic run per tenant: expire quotes, due outreach, the marketing chain, and (daily) training + analytics."""
from ..ai import agent, llm
from ..analytics.metrics import analyse
from ..analytics.training import train_tenant
from ..core import agents
from ..core.config import DEMO_BOOKING_URL
from ..core.models import Tenant
from ..core.utils import now, to_json
from ..marketing import orchestrator
from ..services import dues, otp, plan, users
from ..services.activity import log_activity
from ..services.quotes import expire_quotes
from ..services.receipt import send_due as send_receipts
from .followups import run_due


def _trial_reminder(s, tenant: Tenant) -> None:
    """Three days before a trial ends, the company's sales leads get one email with what they built and the demo link."""
    if not (plan.is_trial(tenant) and plan.days_left(tenant) == 3 and otp.configured()):
        return
    used = {k: plan.used(s, tenant, k) for k in ("campaigns", "emails", "ai")}
    text = (f"Hi,\n\nYour GYC trial for {tenant.name} ends in 3 days. So far: {used['campaigns']} campaigns, "
            f"{used['emails']} emails sent, {used['ai']} AI messages.\n\nEverything stays visible after the trial, but sending and "
            f"editing pause. To keep going, book a short demo and we'll switch on the full version: {DEMO_BOOKING_URL}\n\nThe GYC team")
    for u in users.list_users(s, tenant.id):
        if u.role == "admin":
            try:
                otp.mail(u.email, "3 days left in your GYC trial", text)
            except ValueError:  # mail trouble must not stop the daily run
                pass


def tick(s, tenant: Tenant, daily: bool = False) -> dict:
    """Cron: every few minutes; daily=True once a day."""
    at = now()
    if daily:
        _trial_reminder(s, tenant)
    expire_quotes(s, tenant.id, at)
    receipts = send_receipts(s, tenant, at)  # a receipt is a record of a payment, not outreach: it goes out even when the Orchestrator is off
    if plan.expired(tenant):  # after the trial nothing else goes out on its own
        return {"tasks_run": 0, "messages_sent": 0, "receipts": receipts, "paused": "the trial has ended"}
    if not agents.enabled(tenant, "orchestrator"):  # nothing runs on its own; people trigger everything
        return {"tasks_run": 0, "messages_sent": 0, "receipts": receipts, "paused": "the Orchestrator agent is off"}
    result = run_due(s, tenant, at) | {"receipts": receipts}
    if agents.enabled(tenant, "response"):  # chasing payments is the Response agent's job, like follow-ups
        result["payment_reminders"] = dues.remind_due(s, tenant, at)
    result["marketing"] = orchestrator.run_cycle(s, tenant, daily)  # scheduled sends; daily: measure + next play
    if daily:
        result["training"] = train_tenant(s, tenant)  # relearn from yesterday's outcomes before scoring
        metrics = analyse(s, tenant)
        if not agents.enabled(tenant, "analytics"):
            return result | {"metrics": metrics, "insights": "The Analytics agent is off."}
        try:
            summary = agent.run(s, tenant, "insights", to_json(metrics))
        except (llm.LLMUnavailable, plan.PlanLimit) as e:  # metrics, scores and training above still count
            summary = f"AI insights unavailable: {e}"
        log_activity(s, tenant.id, None, "insights", body=summary, metrics=metrics)
        result |= {"metrics": metrics, "insights": summary}
    return result
