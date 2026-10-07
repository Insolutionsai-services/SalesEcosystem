"""Manager copilot ("Ask your team"): the sales manager directs the agent team in plain language.
It answers from real data through tools and can draft campaigns; approving and sending stay with the human."""
import json

from sqlalchemy import select

from ..ai import agent, llm
from ..ai.tools import function_tool
from ..analytics.metrics import analyse
from ..analytics.team import roster
from ..core.config import AGENT_MAX_TURNS
from ..core.models import Contact, Tenant
from ..core.playbook import setting
from ..core.utils import to_json
from ..marketing import orchestrator, strategy
from ..services import catalog, tasks

HISTORY = 10  # previous turns kept for context

SYSTEM = """You are the chief of staff of {company}'s AI sales team. You speak with the sales manager.
The team: Data/CRM, Strategy, Creative, Verification, Distribution, Response, Analytics and the Orchestrator agents
(Voice comes later). Use your tools for every number, name or status; never invent data. Currency: {currency}.
You may draft campaigns: drafts go to the manager's Approvals list. You cannot approve, send, change prices or
contact customers; when something needs the manager, say exactly what and where (Approvals, Conversations, Marketing).
Answer briefly: a direct answer first, then at most 5 short bullet points. Plain text, no tables.
Refer to customers by name, never by internal id numbers; show lead scores as percentages."""

TOOLS = [
    function_tool("get_overview", "Business snapshot: pipeline, win rate, revenue, response speed, AI usage, hot leads.", {}),
    function_tool("whats_waiting", "Approvals and hand-offs waiting for the manager.", {}),
    function_tool("strategy_recommendations", "The Strategy agent's ranked campaign plays with audience and expected results.", {}),
    function_tool("draft_campaign", "Run Creative + Verification for one recommended play; the draft goes to Approvals.",
                  {"play": {"type": "string", "description": "play key from strategy_recommendations"}}, ["play"]),
    function_tool("draft_custom_campaign", "Draft a campaign from the manager's own idea; goes to Approvals.",
                  {"name": {"type": "string"}, "goal": {"type": "string"},
                   "segments": {"type": "array", "items": {"type": "string"},
                                "description": "optional RFM segments: champions, loyal, new, at_risk, hibernating, regular"}},
                  ["name", "goal"]),
    function_tool("find_customers", "Look up customers by name/phone text, segment or stage.",
                  {"text": {"type": "string"}, "segment": {"type": "string"}, "stage": {"type": "string"}}),
    function_tool("search_catalog", "Search products/services with live price and stock.", {"query": {"type": "string"}}, ["query"]),
    function_tool("team_status", "What each agent has done in the last 30 days.", {}),
]


def _overview(s, t, args):
    m = analyse(s, t)
    return {k: m[k] for k in ("contacts_by_stage", "segments", "win_rate", "revenue_won_30d", "open_pipeline",
                              "forecast_weighted_pipeline", "median_first_response_min", "hot_leads", "ai_30d")}


def _waiting(s, t, args):
    names = {c.id: c.name or c.phone for c in s.scalars(select(Contact).where(Contact.tenant_id == t.id))}
    return [{"kind": x.kind, "customer": names.get(x.contact_id), "reason": x.data.get("reason") or x.data.get("note")}
            for kind in ("approval", "handoff") for x in tasks.list_tasks(s, t.id, "open", kind)]


def _recommendations(s, t, args):
    return [{k: p[k] for k in ("key", "name", "offer", "why", "reachable", "expected_replies", "expected_revenue", "send_at")}
            for p in (x.to_dict() for x in strategy.recommend(s, t))]


def _campaign_summary(c) -> dict:
    return {"campaign_id": c.id, "name": c.name, "status": c.status, "verification": c.checks,
            "variants": [v["text"] for v in c.variants or []]}


def _draft(s, t, args):
    return _campaign_summary(orchestrator.launch_play(s, t, args["play"]))


def _draft_custom(s, t, args):
    segment = {"segments": args["segments"]} if args.get("segments") else {}
    return _campaign_summary(orchestrator.launch_custom(s, t, args["name"], args["goal"], segment))


def _customers(s, t, args):
    text = (args.get("text") or "").lower()
    rows = [c for c in s.scalars(select(Contact).where(Contact.tenant_id == t.id))
            if (not text or text in f"{c.name} {c.phone} {c.email}".lower())
            and (not args.get("segment") or c.segment == args["segment"])
            and (not args.get("stage") or c.stage == args["stage"])]
    return [{"id": c.id, "name": c.name, "stage": c.stage, "segment": c.segment, "score": c.score,
             "facts": c.attrs} for c in rows[:15]] + ([{"more": len(rows) - 15}] if len(rows) > 15 else [])


def _catalog(s, t, args):
    return catalog.search_catalog(s, t.id, args["query"])


def _team(s, t, args):
    return [{"agent": a["name"], "status": a["status"], "stats": a["stats"]} for a in roster(s, t)]


HANDLERS = {"get_overview": _overview, "whats_waiting": _waiting, "strategy_recommendations": _recommendations,
            "draft_campaign": _draft, "draft_custom_campaign": _draft_custom, "find_customers": _customers,
            "search_catalog": _catalog, "team_status": _team}


def _run_tool(s, tenant, call: dict, actions: list) -> dict:
    fn = call["function"]
    try:
        result = HANDLERS[fn["name"]](s, tenant, json.loads(fn.get("arguments") or "{}"))
        if isinstance(result, dict) and "campaign_id" in result:
            actions.append({"type": "campaign", "id": result["campaign_id"], "name": result["name"]})
        content = to_json(result)
    except (KeyError, ValueError, TypeError) as e:
        content = f"Error: {e}"
    return {"role": "tool", "tool_call_id": call["id"], "name": fn["name"], "content": content}


def ask(s, tenant: Tenant, message: str, history: list[dict] | None = None) -> dict:
    """Returns {"reply": str, "actions": [{"type": "campaign", ...}]} for the UI to link."""
    messages = [{"role": "system", "content": SYSTEM.format(company=tenant.name, currency=setting(tenant, "currency"))}]
    messages += [{"role": m["role"], "content": str(m["content"])[:2000]} for m in (history or [])[-HISTORY:]
                 if m.get("role") in ("user", "assistant") and m.get("content")]
    messages.append({"role": "user", "content": message})
    actions: list[dict] = []
    for _ in range(AGENT_MAX_TURNS):
        reply = llm.chat(messages, TOOLS)
        agent.record_usage(s, tenant, None, "copilot", reply)
        messages.append(reply.message)
        if not reply.tool_calls:
            return {"reply": (reply.text or "").strip() or "I couldn't find an answer to that.", "actions": actions}
        messages += [_run_tool(s, tenant, call, actions) for call in reply.tool_calls]
    return {"reply": "That needed more steps than I'm allowed. Try a narrower question.", "actions": actions}
