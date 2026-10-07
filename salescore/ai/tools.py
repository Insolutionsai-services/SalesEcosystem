"""Tools the agent may call. Each is a thin adapter over a deterministic service function."""
from datetime import timedelta

from ..core.models import Contact, Tenant
from ..core.playbook import setting
from ..core.utils import now, to_json
from ..services import catalog, contacts, quotes, tasks


def function_tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    """OpenAI-compatible function tool (understood by Gemini, Ollama, vLLM, Groq, OpenRouter...)."""
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required or []}}}


SCHEMAS = [
    function_tool("search_catalog", "Search this business's catalog (products, units, services, plans) by free text. "
        "Returns sku, name, price, currency, stock and attributes.", {"query": {"type": "string"}}, ["query"]),
    function_tool("create_quote", "Create a priced quote from catalog SKUs. The system prices it from the catalog; returns the "
        "quote text and status ('sent' = share it now, 'pending_approval' = tell the customer the team will confirm).",
        {"lines": {"type": "array", "items": {"type": "object", "properties": {
            "sku": {"type": "string"}, "qty": {"type": "number"}}, "required": ["sku", "qty"]}},
         "discount_pct": {"type": "number", "description": "Only if the customer negotiates; within playbook limits."},
         "note": {"type": "string"}}, ["lines"]),
    function_tool("update_contact", "Save what you learned (budget, need, location, timeline, company...) and/or move the "
        "pipeline stage.", {"stage": {"type": "string"},
                            "facts": {"type": "object", "additionalProperties": {"type": "string"}}}),
    function_tool("create_task", "Schedule a next step: followup, meeting, demo, site_visit, callback.",
        {"kind": {"type": "string"}, "due_in_hours": {"type": "number"}, "note": {"type": "string"}},
        ["kind", "due_in_hours"]),
    function_tool("handoff_to_human", "Pass this conversation to a human salesperson; the bot stops replying.",
        {"reason": {"type": "string"}}, ["reason"]),
]
CONTACT_FREE = {"search_catalog"}


def schemas_for(job: str, has_contact: bool) -> list[dict]:
    if job == "insights":
        return []
    return SCHEMAS if has_contact else [t for t in SCHEMAS if t["function"]["name"] in CONTACT_FREE]


def _search_catalog(s, tenant, contact, args):
    return to_json(catalog.search_catalog(s, tenant.id, args["query"]))


def _create_quote(s, tenant, contact, args):
    q = quotes.create_quote(s, tenant, contact, args["lines"], float(args.get("discount_pct") or 0), args.get("note", ""))
    return to_json({"status": q.status, "quote": quotes.format_quote(q, setting(tenant, "currency"))})


def _update_contact(s, tenant, contact, args):
    contacts.update_contact(s, tenant, contact, args.get("stage"), args.get("facts"))
    return "saved"


def _create_task(s, tenant, contact, args):
    due = now() + timedelta(hours=max(0.0, float(args["due_in_hours"])))
    task = tasks.add_task(s, tenant.id, contact.id, args["kind"], due, note=args.get("note", ""))
    return f"task {task.id} scheduled"


def _handoff(s, tenant, contact, args):
    contacts.handoff(s, tenant, contact, args["reason"])
    return "handed off; tell the customer a team member will reply shortly"


HANDLERS = {"search_catalog": _search_catalog, "create_quote": _create_quote, "update_contact": _update_contact,
            "create_task": _create_task, "handoff_to_human": _handoff}


def run_tool(s, tenant: Tenant, contact: Contact | None, name: str, args: dict) -> str:
    """Raises ValueError/KeyError/TypeError on bad input; the agent loop returns those to the model."""
    handler = HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"unknown tool {name}")
    if contact is None and name not in CONTACT_FREE:
        raise ValueError(f"{name} needs a customer")
    return handler(s, tenant, contact, args)
