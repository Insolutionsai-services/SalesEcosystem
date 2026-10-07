"""Per-tenant configuration. Everything industry-specific lives in the tenant's playbook;
this module is the single place that knows the platform defaults."""

DEFAULTS = {
    "currency": "INR",
    "timezone": "Asia/Kolkata",
    "country_code": "91",
    "channel": "auto",               # auto (WhatsApp, else email, else console) | whatsapp | email | console
    "stages": None,                  # e.g. ["new", "qualified", "quoted", "won", "lost"]; None = free-form
    "followup_days": [1, 3, 7],
    "auto_first_touch": False,
    "pricing": {"max_discount_pct": 0, "auto_approve_discount_pct": 0, "auto_approve_max_total": 0, "tax_pct": 0, "validity_days": 7},
    "compliance": {"quiet_hours": [21, 9], "max_proactive_per_week": 3,
                   "banned_phrases": [], "required_phrases": []},  # checked by the Verification agent, e.g. RERA no.
    "marketing": {  # the Strategy -> ... -> Analytics chain
        "auto_draft": False,          # daily: Orchestrator drafts the top strategy play (still needs approval)
        "cooldown_days": 7,           # don't repeat the same play sooner than this
        "attribution_days": 7,        # replies/sales within N days of a send count for the campaign
        "holdout_pct": 10,            # control group that gets nothing, to measure real lift
        "overstock_ratio": 3,         # stock >= ratio x units sold in 90 days -> "clear overstock" play
        "plays": ["reward_champions", "win_back", "hot_leads", "new_leads", "clear_overstock"],  # Strategy may suggest
    },
    "distribution": {"webhook_url": None},  # social/ads: posts go to this webhook (Postiz, Zapier, Make, n8n...)
    # which agents run for this company (see core/agents.py); Data/CRM, Verification and Approval are always on
    "agents": {k: {"enabled": True} for k in ("strategy", "creative", "distribution", "response", "analytics", "orchestrator")},
    "creative": {"variants": 2},
    "brand": {"primary": "#0a7a6a", "accent": "#f2a443", "contact": ""},  # poster colours and contact line
    "tone": "", "languages": ["en"], "qualify": [], "handoff_rules": [],
    "whatsapp_template": None,       # {"name": ..., "language": "en"} for sends outside the 24h window
    "email_subject": None,
    "opt_out_footer": "Reply STOP to opt out.",
    "opt_out_reply": "You're unsubscribed. Reply START any time to hear from us again.",
    "handoff_reply": "Thanks! A team member will get back to you shortly.",
    # Local answers from the trained models (no LLM call). Raise thresholds for fewer, safer local answers.
    "fastpath": {"enabled": True, "min_confidence": 0.55, "catalog_min_score": 0.45, "catalog_min_margin": 0.05,
                 "faq_min_score": 0.6, "faq_min_margin": 0.05},
    "templates": {  # {name} {business} {item} {price} {stock} {quote} are filled in; translate per tenant
        "greeting": "Hi {name}! Welcome to {business}. How can I help you today?",
        "thanks": "You're welcome, {name}! Anything else I can help you with?",
        "price": "{item}: {price} per unit{tax}. {stock} How many do you need?",
        "in_stock": "In stock ({stock} available).",
        "out_of_stock": "Currently out of stock - I'll check the next arrival for you.",
        "quote_sent": "Here is your quote:\n{quote}\nShall I go ahead with this order?",
        "quote_pending": "Thanks! Our team is confirming price and availability for {item}. We'll get back to you shortly.",
    },
}


def setting(tenant, path: str):
    """setting(tenant, "pricing.tax_pct") -> tenant's value, else the platform default."""
    value, default = tenant.playbook, DEFAULTS
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
        default = default.get(key) if isinstance(default, dict) else None
    return default if value is None else value
