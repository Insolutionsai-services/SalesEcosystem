"""Periodic run per tenant: expire quotes, due outreach, the marketing chain, and (daily) training + analytics."""
from ..ai import agent, llm
from ..analytics.metrics import analyse
from ..analytics.training import train_tenant
from ..core import agents
from ..core.models import Tenant
from ..core.utils import now, to_json
from ..marketing import orchestrator
from ..services.activity import log_activity
from ..services.quotes import expire_quotes
from .followups import run_due


def tick(s, tenant: Tenant, daily: bool = False) -> dict:
    """Cron: every few minutes; daily=True once a day."""
    at = now()
    expire_quotes(s, tenant.id, at)
    if not agents.enabled(tenant, "orchestrator"):  # nothing runs on its own; people trigger everything
        return {"tasks_run": 0, "messages_sent": 0, "paused": "the Orchestrator agent is off"}
    result = run_due(s, tenant, at)
    result["marketing"] = orchestrator.run_cycle(s, tenant, daily)  # scheduled sends; daily: measure + next play
    if daily:
        result["training"] = train_tenant(s, tenant)  # relearn from yesterday's outcomes before scoring
        metrics = analyse(s, tenant)
        if not agents.enabled(tenant, "analytics"):
            return result | {"metrics": metrics, "insights": "The Analytics agent is off."}
        try:
            summary = agent.run(s, tenant, "insights", to_json(metrics))
        except llm.LLMUnavailable as e:  # metrics, scores and training above still count
            summary = f"AI insights unavailable: {e}"
        log_activity(s, tenant.id, None, "insights", body=summary, metrics=metrics)
        result |= {"metrics": metrics, "insights": summary}
    return result
