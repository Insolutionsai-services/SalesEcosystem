"""Prices built from what each unit really costs us, never picked by hand.

  Starter and Growth: fixed monthly prices (PRESETS) with fixed amounts included.
  Custom:              platform fee + Σ (units chosen × PRICE per unit) + extra logins.
  COST is what the providers charge us (facts, below); PRICE is our decision; margin = PRICE - COST, shown by cost_sheet().
  Custom is priced a little above the ready plans for the same amounts, so Starter and Growth stay the better deal.

Provider costs (INR, before GST), checked Nov 2026. Re-check these when a provider changes its rates:
  Gemini 3.1 Flash-Lite   $0.25 / 1M input, $1.50 / 1M output tokens (the model ai/llm.py picks first; third-party listings)
                          our own data: an AI job averages 2,229 input + 1,363 output tokens (297 jobs in the local prod copy)
                          -> $0.0026 = ₹0.23 per AI message at ₹88 / $
  WhatsApp (Meta, India)  marketing ₹0.8631, utility ₹0.115 per delivered message; replies inside the 24-hour window are free.
                          We use the Cloud API directly, so there is no reseller (BSP) fee on top.
  Exotel                  outbound call ≈ ₹1.20 a minute. NOT published: planning range ₹0.80-1.50; get your rate in writing
  Sarvam                  speech-to-text ₹0.49 / min; Bulbul v3 voice ₹3 per 1,000 characters. The agent speaks ~half of each
                          minute at ~900 characters a minute -> ~450 characters = ₹1.35 a minute. Call brain on Sarvam: ~₹0.03 a minute
  Email                   ~₹0.01 per email (bulk SMTP)

Fixed costs we pay whatever customers use, shared across the paying companies:
  server ≈ ₹2,800 (Google Cloud e2-medium in Mumbai + disk and backups; measured to carry ~25 customers)
  Exotel account ≈ ₹1,999, only once a customer has phone calls (third-party figure, confirm with Exotel)
The platform fee covers that share, support and the always-on agents (Data/CRM, Verification, Approval) with 3 logins."""
from ..core import config
from ..core.agents import ALWAYS_ON
from ..core.utils import now
from .access import AGENT_NAMES

CURRENCY = "INR"
USD_INR = 88.0
YEAR_MONTHS = 12                 # yearly = 12 months, no free month: it made Starter lose money without GST (see cost_sheet()['yearly'])
COST_GST = 1.0 if config.GST_REGISTERED else 1.18   # not GST-registered: the 18% our providers charge can't be claimed back
SERVER_MONTHLY = 2800           # e2-medium, Mumbai, with disk and backups
EXOTEL_MONTHLY = 1999            # only when at least one customer has calls
PAYING_COMPANIES = 1             # how many paying companies share the fixed costs; update as you grow
HOSTING = SERVER_MONTHLY / PAYING_COMPANIES                  # each company's share of the server
PLATFORM_FEE = 3499              # Custom plan base, per month: hosting, support, always-on agents, 3 logins
INCLUDED_LOGINS = 3
EXTRA_LOGIN = 299                # per extra login per month (costs us ~nothing)
PRICE = {  # what a customer pays per unit on the Custom plan and for add-on packs (our decision; must stay above COST)
    "ai": 1.00, "wa_followup": 0.25, "wa_campaign": 1.50, "emails": 0.01, "call_minutes": 5.00,
}
AT_COST = {"emails"}             # passed on without profit

GEMINI_IN, GEMINI_OUT = 0.25, 1.50          # USD per 1M tokens (Gemini 3.1 Flash-Lite)
TOKENS_IN, TOKENS_OUT = 2229, 1363          # measured average per AI job
COST = {  # INR per unit, split by provider so the page can say what each price pays for
    "ai": {"Gemini": (TOKENS_IN * GEMINI_IN + TOKENS_OUT * GEMINI_OUT) / 1e6 * USD_INR},
    "wa_followup": {"WhatsApp (Meta)": 0.115},
    "wa_campaign": {"WhatsApp (Meta)": 0.8631},
    "emails": {"Email": 0.01},
    "call_minutes": {"Exotel": 1.20, "Sarvam speech-to-text": 0.49, "Sarvam voice": 1.35, "Sarvam call brain": 0.03},
}


PRICE["emails"] = round(sum(COST["emails"].values()) * COST_GST, 4)   # at cost, as decided


def unit_cost(key: str) -> float:
    return sum(COST[key].values()) * COST_GST


def unit_price(key: str) -> float:
    return PRICE[key]


USAGE = {  # slider rows; `needs` = the agent that does this work
    "ai": {"label": "AI messages", "unit": "message", "help": "Replies, follow-ups and drafts the AI writes",
           "steps": [500, 1000, 2000, 5000, 10000, 25000, 50000], "group": "AI"},
    "wa_followup": {"label": "WhatsApp follow-ups & reminders", "unit": "message", "needs": "response", "group": "WhatsApp",
                    "help": "Follow-ups, payment reminders and quotes sent after 24 hours. Replies to customers are free",
                    "steps": [0, 500, 1000, 3000, 5000, 10000, 25000]},
    "wa_campaign": {"label": "WhatsApp campaign messages", "unit": "message", "needs": "distribution", "group": "WhatsApp",
                    "help": "Offers and announcements sent to your customers", "steps": [0, 1000, 3000, 5000, 10000, 25000, 50000]},
    "call_minutes": {"label": "Call minutes", "unit": "minute", "needs": "voice", "group": "Calls",
                     "help": "Time the Voice agent spends on the phone", "steps": [0, 100, 250, 500, 1000, 2500, 5000]},
    "emails": {"label": "Emails", "unit": "email", "help": "Emails sent to customers", "steps": [0, 500, 1000, 3000, 5000, 10000, 25000],
               "group": "Email"},
    "members": {"label": "Team logins", "unit": "login", "help": "People who can sign in",
                "steps": [3, 5, 10, 15, 25, 50], "group": "Platform"},
}
AGENTS = ["response", "analytics", "strategy", "creative", "distribution", "orchestrator", "voice"]  # switchable on the page
AGENT_HELP = {
    "response": "Replies to customers in seconds, qualifies and quotes", "analytics": "Sales, replies and campaign results",
    "strategy": "Picks who to target, with which offer, and when", "creative": "Writes campaign messages and posters",
    "distribution": "Sends campaigns on WhatsApp, email and social", "orchestrator": "Runs everything on schedule, every day",
    "voice": "Phones your leads and books demos",
    "data_crm": "Customers and every conversation", "verification": "Consent, prices and quiet hours", "approval": "Your OK on risky steps",
}
DEFAULT_ON = {"wa_followup": 1000, "wa_campaign": 3000, "call_minutes": 250}  # amount a row jumps to when its agent is switched on

PRESETS = {
    # campaigns: how many a month; wa_campaign: the messages they may send (about 200 customers per campaign)
    "starter": {"label": "Starter", "tagline": "Answer every customer and send your own campaigns", "price": 5000,
                "highlight": "One fixed price, no surprises",
                "agents": ["response", "analytics", "distribution", "orchestrator"], "campaigns": 5,
                "usage": {"ai": 1000, "wa_followup": 1000, "wa_campaign": 1000, "call_minutes": 0, "emails": 500, "members": 3}},
    "growth": {"label": "Growth", "tagline": "Replies plus AI-planned campaigns that sell", "price": 15000, "popular": True,
               "highlight": "AI plans and writes your campaigns",
               "agents": ["response", "analytics", "strategy", "creative", "distribution", "orchestrator"], "campaigns": 15,
               "usage": {"ai": 5000, "wa_followup": 3000, "wa_campaign": 3000, "call_minutes": 0, "emails": 3000, "members": 10}},
    "custom": {"label": "Custom", "tagline": "Your agents, your amounts", "highlight": "Only pay for what you use"},
}

ADDONS = {  # one-time packs at the same unit price, used after the plan's monthly amount runs out
    "call_minutes": {"label": "Call minutes", "needs": "voice", "packs": [100, 500, 1000]},
    "ai": {"label": "AI messages", "packs": [1000, 5000, 10000]},
    "wa_campaign": {"label": "WhatsApp messages", "packs": [1000, 5000, 20000]},
}


def _lines(usage: dict) -> list[dict]:
    lines = [{"key": "platform", "group": "Platform", "label": "Platform, CRM, compliance and approvals", "amount": PLATFORM_FEE}]
    extra = max(0, usage["members"] - INCLUDED_LOGINS)
    if extra:
        lines.append({"key": "members", "group": "Platform", "label": f"{extra} extra logins", "amount": extra * EXTRA_LOGIN})
    for k, u in USAGE.items():
        if k == "members" or not usage.get(k):
            continue
        lines.append({"key": k, "group": u["group"], "label": f"{u['label']}: {usage[k]:,}", "amount": round(usage[k] * unit_price(k))})
    return lines


def _clean(agents, usage: dict) -> tuple[set[str], dict]:
    agents = set(agents or [])
    if agents - set(AGENTS):
        raise ValueError("unknown agent")
    if not agents:
        raise ValueError("switch on at least one agent")
    out = {}
    for k, u in USAGE.items():
        v = int(usage.get(k, u["steps"][0]))
        if v not in u["steps"]:
            raise ValueError(f"choose one of the amounts for {u['label']}")
        out[k] = 0 if u.get("needs") and u["needs"] not in agents else v   # no campaign messages without Distribution, and so on
    return agents, out


def custom_total(agents, usage: dict) -> int:
    """What the same agents and amounts would cost on the Custom plan."""
    return sum(x["amount"] for x in _lines(_clean(agents, usage)[1]))


def our_cost(usage: dict, companies: int | None = None) -> float:
    """What a month with these amounts costs us: the server share plus every provider's fees (plus Exotel when there are calls)."""
    n = companies or PAYING_COMPANIES
    return (SERVER_MONTHLY / n + (EXOTEL_MONTHLY / n if usage.get("call_minutes") else 0)) * COST_GST + sum(usage.get(k, 0) * unit_cost(k) for k in COST)


def catalog() -> dict:
    """Everything the page shows and adds up; unit prices come from the cost sheet above."""
    return {"currency": CURRENCY, "year_months": YEAR_MONTHS, "tax_note": "Prices exclude 18% GST." if config.GST_REGISTERED else "Final price. No GST is added.",
            "platform_fee": PLATFORM_FEE, "included_logins": INCLUDED_LOGINS, "extra_login": EXTRA_LOGIN,
            "presets": [{"key": k, **p, "price": p.get("price"),
                         "custom_price": custom_total(p["agents"], p["usage"]) if "usage" in p else None} for k, p in PRESETS.items()],
            "always": [{"key": k, "label": AGENT_NAMES[k], "help": AGENT_HELP[k]} for k in ALWAYS_ON],
            "agents": [{"key": k, "label": AGENT_NAMES[k], "help": AGENT_HELP[k]} for k in AGENTS],
            "usage": {k: {**u, "price": unit_price(k) if k in COST else EXTRA_LOGIN} for k, u in USAGE.items()},
            "default_on": DEFAULT_ON, "addons": [{"key": k, **a} for k, a in ADDONS.items()]}


def quote(sel: dict) -> dict:
    """Price {plan, cycle, agents, usage: {row: amount}, addons: {row: {pack: i, qty: n}}}. Raises ValueError on anything unknown."""
    plan, cycle = sel.get("plan", "custom"), sel.get("cycle", "month")
    if plan not in PRESETS:
        raise ValueError("choose Starter, Growth or Custom")
    if cycle not in ("month", "year"):
        raise ValueError("billing must be monthly or yearly")
    if plan != "custom":   # a ready plan is exactly its preset
        sel = {**sel, "agents": PRESETS[plan]["agents"], "usage": PRESETS[plan]["usage"]}
    agents, usage = _clean(sel.get("agents"), sel.get("usage") or {})
    if plan == "custom":
        lines = [{**x, "kind": "month"} for x in _lines(usage)]
    else:   # one fixed price covers everything the ready plan includes
        lines = [{"key": "plan", "group": "Plan", "label": f"{PRESETS[plan]['label']} plan", "amount": PRESETS[plan]["price"], "kind": "month"}]
    for k, pick in (sel.get("addons") or {}).items():
        a = ADDONS.get(k)
        if a is None:
            raise ValueError("unknown add-on")
        qty, i = int(pick.get("qty", 0)), int(pick.get("pack", 0))
        if qty <= 0:
            continue
        if not 0 <= i < len(a["packs"]) or qty > 20:
            raise ValueError(f"unknown pack for {a['label']}")
        if a.get("needs") and a["needs"] not in agents:
            raise ValueError(f"{a['label']} need the {AGENT_NAMES[a['needs']]} agent")
        units = a["packs"][i] * qty
        lines.append({"key": f"addon_{k}", "group": USAGE[k]["group"], "label": f"{a['label']}: {qty} × {a['packs'][i]:,}",
                      "amount": round(units * unit_price(k)), "kind": "once"})
    monthly = sum(x["amount"] for x in lines if x["kind"] == "month")
    once = sum(x["amount"] for x in lines if x["kind"] == "once")
    recurring = monthly * (YEAR_MONTHS if cycle == "year" else 1)
    return {"plan": plan, "label": PRESETS[plan]["label"], "cycle": cycle, "campaigns": PRESETS[plan].get("campaigns"), "agents": sorted(agents | set(ALWAYS_ON)), "usage": usage,
            "lines": lines, "monthly": monthly, "once": once, "recurring": recurring,
            "saving": monthly * (12 - YEAR_MONTHS) if cycle == "year" else 0, "due_now": recurring + once, "currency": CURRENCY}


def _margin(price: float, cost: float) -> dict:
    return {"price": round(price, 2), "cost": round(cost, 2), "profit": round(price - cost, 2), "margin_pct": round((price - cost) / price * 100, 1)}


def cost_sheet(companies: int | None = None) -> dict:
    """For the platform owner only: per unit and per ready plan (fully used), what it costs us, what the customer pays and what we keep."""
    n = companies or PAYING_COMPANIES
    units = {k: {**_margin(PRICE[k], unit_cost(k)), "by_provider": {p: round(c, 3) for p, c in v.items()}} for k, v in COST.items()}
    plans = {k: {**_margin(p["price"], our_cost(p["usage"], n)), "if_fully_used": True}
             for k, p in PRESETS.items() if "price" in p and p["price"]}
    yearly = {k: {**_margin(p["price"] * YEAR_MONTHS, our_cost(p["usage"], n) * 12), "paid_months": YEAR_MONTHS, "cost_months": 12}
              for k, p in PRESETS.items() if p.get("price")}
    return {"units": units, "plans": plans, "yearly": yearly, "platform_fee": _margin(PLATFORM_FEE, SERVER_MONTHLY / n), "usd_inr": USD_INR,
            "server_monthly": SERVER_MONTHLY, "exotel_monthly": EXOTEL_MONTHLY, "companies": n, "server_share_per_company": round(SERVER_MONTHLY / n)}


def current(tenant) -> dict:
    if tenant.plan in ("starter", "growth"):
        return {"key": tenant.plan, "label": PRESETS[tenant.plan]["label"]}
    if tenant.plan == "custom":
        return {"key": "custom", "label": "Custom"}
    if tenant.plan == "trial":
        return {"key": "trial", "label": "Free trial"}
    return {"key": "custom", "label": "Custom", "arranged": True}   # set up by the GYC team (formerly "unlimited")


def stamp(q: dict, by: str) -> dict:
    return {**q, "requested_by": by, "requested_at": now().isoformat(timespec="minutes")}
