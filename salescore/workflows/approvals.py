"""Human approval checkpoint: quotes over the auto limits and every campaign wait here for a manager."""
from ..core.models import Campaign, Contact, Quote, Task, Tenant
from ..core.playbook import setting
from ..marketing import orchestrator
from ..services import channels, contacts, quotes


def _decide_quote(s, tenant: Tenant, task: Task, approve: bool, text: str | None, send_now: bool) -> dict:
    quote, contact = s.get(Quote, task.data["quote_id"]), s.get(Contact, task.contact_id)
    if not approve:
        quote.status = "rejected"
        contacts.handoff(s, tenant, contact, f"quote {quote.id} rejected by manager; call the customer", pause_bot=False)
    else:
        quote.status = "sent"
        channels.send(s, tenant, contact, text or quotes.format_quote(quote, setting(tenant, "currency")), "reply")
    return {"quote": quote.id, "status": quote.status}


def _decide_campaign(s, tenant: Tenant, task: Task, approve: bool, text: str | None, send_now: bool) -> dict:
    campaign = s.get(Campaign, task.data["campaign_id"])
    return orchestrator.approve(s, tenant, campaign, text, send_now) if approve else orchestrator.reject(campaign)


def decide(s, tenant: Tenant, task: Task, approve: bool, text: str | None = None, send_now: bool = False,
           by: str | None = None) -> dict:
    """text optionally replaces the drafted message; send_now skips the Strategy agent's planned send time."""
    if task.tenant_id != tenant.id or task.kind != "approval" or task.status != "open":
        raise ValueError("not an open approval for this tenant")
    task.status, task.data = "done", {**task.data, "approved": approve, **({"by": by} if by else {})}
    handler = _decide_quote if "quote_id" in task.data else _decide_campaign
    return handler(s, tenant, task, approve, text, send_now)
