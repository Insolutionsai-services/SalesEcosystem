"""One agent runtime for every sales job: prompt (job + playbook) -> LLM -> tools -> ... -> final text.
Provider-neutral (see llm.py). Every call is logged with tokens and cost."""
import json

from ..core.config import AGENT_MAX_TURNS
from ..core.models import Contact, Tenant
from ..core.utils import to_json
from ..services.activity import log_activity
from ..services.contacts import contact_profile
from . import llm
from .prompts import SKIP, system_prompt
from .tools import run_tool, schemas_for

TOOL_INPUT_ERRORS = (ValueError, KeyError, TypeError, json.JSONDecodeError)
LITE_JOBS = {"followup", "first_touch", "reorder", "insights"}  # background jobs run on the cheaper model
PROFILE_HISTORY = 12  # recent messages sent as context; fewer tokens per call


def is_skip(text: str | None) -> bool:
    return not text or text.strip().upper() == SKIP


def record_usage(s, tenant: Tenant, contact: Contact | None, job: str, reply: llm.Reply) -> None:
    log_activity(s, tenant.id, contact.id if contact else None, "llm", job=job, model=reply.model,
                 tokens_in=reply.usage.get("prompt_tokens", 0), tokens_out=reply.usage.get("completion_tokens", 0),
                 cost_usd=reply.cost_usd, seconds=reply.seconds)


def _tool_message(s, tenant, contact, call: dict) -> dict:
    fn = call["function"]
    try:
        content = run_tool(s, tenant, contact, fn["name"], json.loads(fn.get("arguments") or "{}"))
    except TOOL_INPUT_ERRORS as e:  # bad tool input -> let the model correct itself
        content = f"Error: {e}"
    return {"role": "tool", "tool_call_id": call["id"], "name": fn["name"], "content": content}


def run(s, tenant: Tenant, job: str, text: str, contact: Contact | None = None, chat=None) -> str | None:
    """Runs one agent job to completion. Returns the final text, or None if the model gave nothing usable."""
    chat = chat or llm.chat
    tools = schemas_for(job, contact is not None) or None
    profile = to_json(contact_profile(s, contact, PROFILE_HISTORY)) if contact else "n/a"
    messages = [{"role": "system", "content": system_prompt(job, to_json(tenant.playbook))},  # stable prefix: cacheable
                {"role": "user", "content": f"CUSTOMER PROFILE:\n{profile}\n\nINPUT:\n{text}"}]

    for _ in range(AGENT_MAX_TURNS):
        reply = chat(messages, tools, lite=job in LITE_JOBS)
        record_usage(s, tenant, contact, job, reply)
        messages.append(reply.message)
        if not reply.tool_calls:
            return (reply.text or "").strip() or None
        messages += [_tool_message(s, tenant, contact, call) for call in reply.tool_calls]
    raise RuntimeError(f"agent job {job!r} exceeded {AGENT_MAX_TURNS} turns")
