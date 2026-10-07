"""Which agents run for a company, and the settings each one exposes in the UI.
Settings live in the playbook; this module only describes them (labels, types, limits) and validates edits."""
import copy

from .playbook import setting

ALWAYS_ON = {  # switching these off would break the data, the law, or the human checkpoint
    "data_crm": "Every other agent reads and writes customer data here.",
    "verification": "Required by law: consent, opt-out, quiet hours and price checks.",
    "approval": "Campaigns and large discounts always need a person's OK.",
}
PLANNED = {"voice"}
PLAYS = [("reward_champions", "Reward best customers"), ("win_back", "Win back lapsing customers"),
         ("hot_leads", "Close hot leads"), ("new_leads", "Welcome new leads"), ("clear_overstock", "Clear overstock")]


def _f(path, label, type_, help_="", **extra) -> dict:
    return {"path": path, "label": label, "type": type_, "help": help_, **extra}


FIELDS = {
    "data_crm": [
        _f("currency", "Currency", "text", "Used on quotes and reports, e.g. INR"),
        _f("timezone", "Time zone", "text", "For quiet hours and send times, e.g. Asia/Kolkata"),
        _f("country_code", "Phone country code", "text", "Added to 10-digit numbers, e.g. 91"),
        _f("stages", "Pipeline stages", "list", "In order, separated by commas"),
        _f("auto_first_touch", "Message newly imported leads automatically", "bool"),
    ],
    "strategy": [
        _f("marketing.plays", "Campaign types it may suggest", "multi", options=[{"value": k, "label": v} for k, v in PLAYS]),
        _f("marketing.cooldown_days", "Days before repeating a campaign type", "int", min=1, max=90),
        _f("marketing.overstock_ratio", "Overstock when stock is this many times 90-day sales", "number", min=1, max=50),
    ],
    "creative": [
        _f("tone", "Tone of voice", "text", "e.g. warm, helpful, concise"),
        _f("languages", "Languages", "list", "Codes or names, e.g. en, ta"),
        _f("creative.variants", "Versions per campaign", "select", "Two versions lets Analytics compare them",
           options=[{"value": 1, "label": "1 version"}, {"value": 2, "label": "2 versions (A/B test)"}]),
        _f("brand.primary", "Poster main colour", "color"),
        _f("brand.accent", "Poster highlight colour", "color"),
        _f("brand.contact", "Contact line on posters", "text", "e.g. +91 98400 11001 · demopaints.in"),
    ],
    "verification": [
        _f("pricing.max_discount_pct", "Maximum discount %", "number", "Copy or quotes above this are blocked", min=0, max=90),
        _f("compliance.max_proactive_per_week", "Proactive messages per customer per week", "int", min=0, max=21),
        _f("compliance.quiet_hours", "Quiet hours", "hours", "No proactive messages between these hours"),
        _f("compliance.banned_phrases", "Banned words and claims", "list"),
        _f("compliance.required_phrases", "Text every campaign must include", "list", "e.g. your RERA number"),
    ],
    "approval": [
        _f("pricing.auto_approve_discount_pct", "Quotes go out automatically up to this discount %", "number", min=0, max=90),
        _f("pricing.auto_approve_max_total", "Quotes go out automatically up to this total amount", "number",
           "Bigger quotes wait for you in Needs you. 0 = no amount limit", min=0),
        _f("pricing.tax_pct", "Tax % on quotes", "number", min=0, max=50),
        _f("pricing.validity_days", "Quotes are valid for (days)", "int", min=1, max=90),
    ],
    "distribution": [
        _f("channel", "Send through", "select", "Automatic uses WhatsApp once connected, then email; until then messages only show in the app",
           options=[{"value": "auto", "label": "Automatic (WhatsApp, then email)"}, {"value": "whatsapp", "label": "WhatsApp only"},
                    {"value": "email", "label": "Email only"}, {"value": "console", "label": "Console (testing)"}]),
        _f("marketing.holdout_pct", "Holdout group %", "number", "Customers who get nothing, to measure real lift", min=0, max=50),
        _f("whatsapp_template.name", "WhatsApp template name", "text", "Meta-approved template for sends after 24 hours"),
        _f("whatsapp_template.language", "WhatsApp template language", "text", "e.g. en"),
        _f("whatsapp_template.image", "Template has an image header", "bool", "Turn on if the approved template shows a picture on top; posters go there"),
        _f("distribution.webhook_url", "Social / ads webhook URL", "text", "Postiz, Zapier, Make or n8n"),
    ],
    "response": [
        _f("fastpath.enabled", "Answer routine questions instantly with the local models", "bool", "Free and instant"),
        _f("followup_days", "Follow up after (days)", "intlist", "e.g. 1, 3, 7"),
        _f("qualify", "Questions to ask customers", "list"),
        _f("handoff_rules", "When to hand over to a person", "list"),
        _f("handoff_reply", "Message when handing to a person", "textarea"),
    ],
    "analytics": [
        _f("marketing.attribution_days", "Count replies and sales for (days) after a campaign", "int", min=1, max=60),
    ],
    "orchestrator": [
        _f("marketing.auto_draft", "Draft the next best campaign every day", "bool", "Drafts still wait for your approval"),
    ],
    "voice": [],
}

SWITCHABLE = ["strategy", "creative", "distribution", "response", "analytics", "orchestrator"]
PRESETS = {
    "full": {"label": "Full AI sales team", "on": SWITCHABLE},
    "replies": {"label": "Customer replies only", "on": ["response", "analytics", "orchestrator"]},
    "campaigns": {"label": "Campaigns only", "on": ["strategy", "creative", "distribution", "analytics", "orchestrator"]},
    "broadcast": {"label": "Send my own WhatsApp broadcasts", "on": ["distribution", "analytics", "orchestrator"]},
}


def enabled(tenant, key: str) -> bool:
    if key in ALWAYS_ON:
        return True
    if key in PLANNED:
        return False
    return bool(setting(tenant, f"agents.{key}.enabled"))


def state(tenant, key: str) -> str:
    return "always" if key in ALWAYS_ON else "planned" if key in PLANNED else "on" if enabled(tenant, key) else "off"


def fields_with_values(tenant, key: str) -> list[dict]:
    return [{**f, "value": setting(tenant, f["path"])} for f in FIELDS.get(key, [])]


def _coerce(field: dict, value):
    t, label = field["type"], field["label"]
    try:
        if t == "bool":
            return bool(value)
        if t in ("int", "number"):
            v = int(value) if t == "int" else float(value)
            if not field.get("min", v) <= v <= field.get("max", v):
                raise ValueError
            return v
        if t in ("text", "textarea"):
            return str(value or "").strip() or None
        if t == "color":
            v = str(value).strip()
            if len(v) != 7 or v[0] != "#":
                raise ValueError
            int(v[1:], 16)
            return v.lower()
        if t == "list":
            items = value if isinstance(value, list) else str(value or "").split(",")
            return [str(x).strip() for x in items if str(x).strip()]
        if t == "intlist":
            items = value if isinstance(value, list) else str(value or "").split(",")
            out = [int(x) for x in items if str(x).strip()]
            if any(d < 0 for d in out):
                raise ValueError
            return out
        if t == "hours":
            start, end = (int(x) for x in value)
            if not (0 <= start <= 23 and 0 <= end <= 23):
                raise ValueError
            return [start, end]
        allowed = [o["value"] for o in field["options"]]
        if t == "select":
            if value not in allowed:
                raise ValueError
            return value
        if t == "multi":
            if not isinstance(value, list) or any(v not in allowed for v in value):
                raise ValueError
            return value
    except (TypeError, ValueError):
        pass
    limits = f" ({field['min']}-{field['max']})" if "min" in field else ""
    raise ValueError(f"'{label}' has an invalid value{limits}")


def _set(playbook: dict, path: str, value) -> None:
    keys, node = path.split("."), playbook
    for k in keys[:-1]:
        if not isinstance(node.get(k), dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value


def update(playbook: dict, key: str, values: dict | None = None, on: bool | None = None) -> dict:
    """Returns a new playbook with the agent's settings and/or on/off switch changed. Raises ValueError on bad input."""
    if key not in FIELDS:
        raise ValueError(f"unknown agent '{key}'")
    pb = copy.deepcopy(playbook)
    if on is not None:
        if key not in SWITCHABLE:
            raise ValueError("this agent can't be switched off" if key in ALWAYS_ON else "this agent isn't available yet")
        _set(pb, f"agents.{key}.enabled", bool(on))
    fields = {f["path"]: f for f in FIELDS[key]}
    for path, value in (values or {}).items():
        if path not in fields:
            raise ValueError(f"'{path}' isn't a setting of this agent")
        _set(pb, path, _coerce(fields[path], value))
    return pb


def apply_preset(playbook: dict, preset: str) -> dict:
    if preset not in PRESETS:
        raise ValueError(f"unknown preset '{preset}'")
    pb = copy.deepcopy(playbook)
    for key in SWITCHABLE:
        _set(pb, f"agents.{key}.enabled", key in PRESETS[preset]["on"])
    return pb
