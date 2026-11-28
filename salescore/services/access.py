"""What each sales rep may open and change. The sales lead (role admin) always has everything and decides this per person.
`User.access` holds {"pages": [...], "agents": {key: hidden|view|edit}}; empty means the standard sales-rep access below.
The server enforces it (see check_request); the UI only mirrors it so people don't see screens they can't use."""
from ..core.agents import ALWAYS_ON, FIELDS

PAGES = [  # (key, label, what it gives); "home" is always on: it is where everyone lands
    ("home", "Command centre", "Today's numbers and what the agents are doing"),
    ("approvals", "Needs you", "Approve quotes and campaigns, take over chats"),
    ("conversations", "Conversations", "Read and answer customer chats"),
    ("marketing", "Marketing", "Build, approve and send campaigns"),
    ("analytics", "Analytics", "Sales, replies and campaign results"),
    ("blueprint", "Blueprint", "Plot layout and availability"),
    ("clients", "Clients", "Brokers and their leads"),
    ("suggestions", "AI suggestions", "What the AI suggests doing next"),
    ("quotes", "Quotes & orders", "Quotes, orders and payments"),
    ("agents", "Agents", "See the AI team and its rules"),
    ("catalog", "Catalog", "Products, prices and stock"),
    ("settings", "Settings", "Channels, AI key and the playbook (view only)"),
    ("copilot", "Ask your team", "The assistant that answers and acts on instructions"),
]
PAGE_KEYS = [k for k, _, _ in PAGES]
ESTATE_ONLY = {"blueprint", "clients", "suggestions"}
AGENT_NAMES = {"data_crm": "Data / CRM", "strategy": "Strategy", "creative": "Creative", "verification": "Verification",
               "approval": "Human approval", "distribution": "Distribution", "response": "Response", "voice": "Voice",
               "analytics": "Analytics", "orchestrator": "Orchestrator"}
LEVELS = ("hidden", "view", "edit")

DEFAULT = {"pages": PAGE_KEYS, "agents": {k: "view" for k in FIELDS}}  # what every rep had before per-person access existed
FULL = {"pages": PAGE_KEYS, "agents": {k: "edit" for k in FIELDS}}
TEMPLATES = {
    "rep": {"label": "Sales rep", "help": "Every screen, agents view only", "access": DEFAULT},
    "replies": {"label": "Reply desk", "help": "Chats, approvals and quotes",
                "access": {"pages": ["home", "approvals", "conversations", "quotes", "copilot"],
                           "agents": {**{k: "hidden" for k in FIELDS}, "response": "view"}}},
    "marketing": {"label": "Marketing", "help": "Runs campaigns and their agents",
                  "access": {"pages": ["home", "marketing", "analytics", "agents", "catalog", "copilot"],
                             "agents": {**{k: "view" for k in FIELDS}, "strategy": "edit", "creative": "edit", "distribution": "edit"}}},
    "viewer": {"label": "View only", "help": "Dashboard and analytics, changes nothing",
               "access": {"pages": ["home", "analytics"], "agents": {k: "hidden" for k in FIELDS}}},
}

# API paths that belong to a screen. "all" = reading is blocked too; "write" = only changes are (the Command centre reads them for its summary).
_ROUTES = [
    ("approvals", "write", "/v1/approvals"),
    ("conversations", "all", "/v1/contacts"), ("conversations", "write", "/v1/inbound"), ("conversations", "write", "/v1/import/contacts"),
    ("marketing", "write", "/v1/campaigns"), ("marketing", "write", "/v1/marketing"), ("marketing", "all", "/v1/audience"),
    ("marketing", "write", "/v1/outreach"),
    ("analytics", "all", "/v1/analytics"),
    ("quotes", "write", "/v1/quotes"),
    ("catalog", "write", "/v1/catalog"), ("catalog", "write", "/v1/import/catalog"),
    ("blueprint", "all", "/v1/realestate/plots"), ("clients", "all", "/v1/realestate/clients"),
    ("suggestions", "all", "/v1/realestate/suggestions"),
    ("settings", "all", "/v1/channels"), ("settings", "all", "/v1/ai"), ("settings", "all", "/v1/playbook"),
    ("agents", "all", "/v1/agents"), ("agents", "all", "/v1/voice"), ("agents", "write", "/v1/train"),
    ("copilot", "all", "/v1/copilot"),
]


def is_lead(user) -> bool:
    return user is not None and user.role == "admin"


def resolve(user) -> dict:
    """The person's effective access: the lead gets everything, a rep gets their saved access filled in with the defaults."""
    if is_lead(user):
        return FULL
    saved = (user.access if user is not None else None) or {}
    pages = [p for p in PAGE_KEYS if p == "home" or p in saved.get("pages", DEFAULT["pages"])]
    agents = {k: saved.get("agents", {}).get(k, DEFAULT["agents"][k]) for k in FIELDS}
    if "agents" not in pages:
        agents = {k: "hidden" for k in agents}  # no Agents screen: nothing to look at there
    return {"pages": pages, "agents": agents}


def validate(access: dict | None) -> dict | None:
    """Clean a sales lead's edit; raises ValueError on anything unknown. None = back to the standard sales-rep access."""
    if access is None:
        return None
    if not isinstance(access, dict):
        raise ValueError("access must be an object")
    pages = access.get("pages", DEFAULT["pages"])
    if not isinstance(pages, list) or any(p not in PAGE_KEYS for p in pages):
        raise ValueError("unknown screen in access")
    agents = access.get("agents", {})
    if not isinstance(agents, dict) or any(k not in FIELDS or v not in LEVELS for k, v in agents.items()):
        raise ValueError("agent access must be hidden, view or edit")
    return {"pages": [p for p in PAGE_KEYS if p == "home" or p in pages], "agents": {k: agents.get(k, "view") for k in FIELDS}}


def can_page(user, page: str) -> bool:
    return page in resolve(user)["pages"]


def agent_level(user, key: str) -> str:
    return resolve(user)["agents"].get(key, "hidden")


def blocked_page(user, method: str, path: str) -> str | None:
    """The screen this request needs and the person lacks, or None. Only signed-in reps are limited; API keys and the lead pass."""
    if user is None or is_lead(user):
        return None
    write = method not in ("GET", "HEAD", "OPTIONS")
    for page, scope, prefix in _ROUTES:
        if (path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?")) and (scope == "all" or write):
            return None if can_page(user, page) else page
    return None


def options(industry: str | None) -> dict:
    """Everything the sales lead's access editor shows: screens for this company, agents, and one-click templates."""
    pages = [{"key": k, "label": label, "help": help_, "fixed": k == "home"} for k, label, help_ in PAGES
             if k not in ESTATE_ONLY or industry == "realestate"]
    agents = [{"key": k, "label": AGENT_NAMES[k], "always_on": k in ALWAYS_ON} for k in FIELDS]
    templates = [{"key": k, "label": t["label"], "help": t["help"], "access": t["access"]} for k, t in TEMPLATES.items()]
    return {"pages": pages, "agents": agents, "levels": list(LEVELS), "templates": templates}
