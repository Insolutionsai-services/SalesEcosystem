"""Owner console: every customer company, the revenue and profit they bring, plan requests, prices, and the server's health.
Only the platform owner sees this (config.OWNER_EMAILS, or the X-Admin-Key). Costs are estimates: this month's real usage
(from the activity log) × the cost rates in services/pricing.py, plus the server shared by the companies that pay."""
import ctypes
import os
import platform
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from ..core import config
from ..core.models import Activity, Base, Campaign, Contact, Deployment, Tenant, User, engine
from ..core.utils import now
from . import plan, pricing, users as user_svc

STARTED = now()
PACKAGE = Path(__file__).resolve().parent.parent
PLANS = ("trial", "starter", "growth", "custom")
USAGE_KEYS = ("ai", "wa_followup", "wa_campaign", "emails", "call_minutes")


def month_start(d: datetime | None = None) -> datetime:
    return (d or now()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ---------- money ----------

def plan_key(t: Tenant) -> str:
    """trial | starter | growth | custom ("unlimited" companies were arranged by hand: custom)."""
    return t.plan if t.plan in PLANS else "custom"


def monthly_price(t: Tenant) -> int | None:
    """What the company pays a month. None = custom company whose price was never recorded."""
    if t.plan == "trial":
        return 0
    if t.plan_price is not None:
        return t.plan_price
    if t.plan in ("starter", "growth"):
        return pricing.PRESETS[t.plan]["price"]
    return None


def status(t: Tenant) -> tuple[str, str]:
    """(key, label) for lists and filters."""
    if t.plan == "trial":
        left = plan.days_left(t)
        return ("trial_ended", "Trial ended") if not left else ("trial", f"On trial · {left} day{'s' if left != 1 else ''} left")
    return ("paying", "Paying") if monthly_price(t) else ("unpriced", "Price not set")


def _usage_this_month(s, since: datetime) -> dict[int, dict[str, int]]:
    """Units each company used since `since`, the same units the prices are in."""
    out: dict[int, dict[str, int]] = {}
    via, kind = Activity.data["via"].as_string(), Activity.data["kind"].as_string()
    rows = s.execute(select(Activity.tenant_id, Activity.channel, via, kind, func.count())
                     .where(Activity.type == "msg_out", Activity.at >= since).group_by(Activity.tenant_id, Activity.channel, via, kind)).all()
    for tid, channel, v, k, n in rows:
        u = out.setdefault(tid, dict.fromkeys(USAGE_KEYS, 0))
        if k == "test":
            continue
        if channel == "email":
            u["emails"] += n
        elif channel == "whatsapp":
            if v == "campaign":
                u["wa_campaign"] += n
            elif k != "reply":                       # replies inside the 24-hour window are free
                u["wa_followup"] += n
    for tid, n in s.execute(select(Activity.tenant_id, func.count()).where(Activity.type == "llm", Activity.at >= since)
                            .group_by(Activity.tenant_id)).all():
        out.setdefault(tid, dict.fromkeys(USAGE_KEYS, 0))["ai"] = n
    return out


def _row(s, t: Tenant, usage: dict, paying: int, logins: dict, contacts: dict, campaigns: dict) -> dict:
    u = usage.get(t.id, dict.fromkeys(USAGE_KEYS, 0))
    u = {**u, "call_minutes": plan.used(s, t, "call_minutes", month_start())}
    price = monthly_price(t)
    usage_cost = sum(u[k] * pricing.unit_cost(k) for k in USAGE_KEYS)
    share = pricing.SERVER_MONTHLY * pricing.COST_GST / paying if price else 0   # the server is shared by the companies that pay
    cost = round(usage_cost + share)
    key, label = status(t)
    preset = pricing.PRESETS.get(t.plan, {})
    included = (t.plan_details or {}).get("usage") or preset.get("usage") or {}
    return {"id": t.id, "name": t.name, "plan": plan_key(t), "plan_label": pricing.PRESETS[plan_key(t)]["label"] if plan_key(t) != "trial" else "Trial",
            "status": key, "status_label": label, "since": (t.created_at or t.plan_started_at).date().isoformat(),
            "logins": logins.get(t.id, (0, None))[0], "last_active": logins.get(t.id, (0, None))[1],
            "contacts": contacts.get(t.id, 0), "campaigns": campaigns.get(t.id, 0), "usage": u,
            "included": {k: included.get(k) for k in USAGE_KEYS if included.get(k) is not None},
            "revenue": price or 0, "price_set": price is not None, "cost": cost, "usage_cost": round(usage_cost), "server_share": round(share),
            "profit": (price or 0) - cost}


def customers(s) -> list[dict]:
    tenants = s.scalars(select(Tenant).order_by(Tenant.id)).all()
    since = month_start()
    usage = _usage_this_month(s, since)
    paying = max(1, sum(1 for t in tenants if monthly_price(t)))
    logins = {tid: (n, last.isoformat() if last else None)
              for tid, n, last in s.execute(select(User.tenant_id, func.count(), func.max(User.last_login_at)).group_by(User.tenant_id)).all()}
    contacts = dict(s.execute(select(Contact.tenant_id, func.count()).group_by(Contact.tenant_id)).all())
    campaigns = dict(s.execute(select(Campaign.tenant_id, func.count()).where(Campaign.created_at >= since).group_by(Campaign.tenant_id)).all())
    return [_row(s, t, usage, paying, logins, contacts, campaigns) for t in tenants]


def overview(s) -> dict:
    rows = customers(s)
    this_month, last_month = month_start(), month_start(month_start() - timedelta(days=1))
    created = [datetime.fromisoformat(r["since"]) for r in rows]
    months = []
    m = month_start(now())
    for _ in range(12):
        months.append(m)
        m = month_start(m - timedelta(days=1))
    months.reverse()
    signups = [{"month": m.strftime("%Y-%m"), "label": m.strftime("%b %y"),
                "count": sum(1 for c in created if c.year == m.year and c.month == m.month)} for m in months]
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    by_plan = {k: {"customers": 0, "revenue": 0, "cost": 0} for k in ("starter", "growth", "custom")}
    for r in rows:
        if r["plan"] in by_plan and r["status"] in ("paying", "unpriced"):
            b = by_plan[r["plan"]]
            b["customers"] += 1; b["revenue"] += r["revenue"]; b["cost"] += r["cost"]
    revenue = sum(r["revenue"] for r in rows)
    calls = any(r["usage"]["call_minutes"] for r in rows)
    fixed = round((pricing.SERVER_MONTHLY + (pricing.EXOTEL_MONTHLY if calls else 0)) * pricing.COST_GST)
    usage_cost = sum(r["usage_cost"] for r in rows)        # trials use the server and the AI too: count them in the real total
    cost = usage_cost + fixed
    attention = []
    pending = [r for r in requests(s) if r["status"] == "pending"]
    if pending:
        attention.append({"kind": "requests", "text": f"{len(pending)} plan request{'s' if len(pending) > 1 else ''} waiting for you", "tone": "human"})
    for r in rows:
        if r["status"] == "unpriced":
            attention.append({"kind": "customer", "id": r["id"], "text": f"{r['name']}: no monthly price recorded", "tone": "warn"})
        elif r["revenue"] and r["profit"] < 0:
            attention.append({"kind": "customer", "id": r["id"], "text": f"{r['name']} is costing more than they pay", "tone": "bad"})
        elif r["status"] == "trial" and plan.days_left(s.get(Tenant, r["id"])) <= 3:
            attention.append({"kind": "customer", "id": r["id"], "text": f"{r['name']}: trial ends soon", "tone": "warn"})
    return {"customers": {"total": len(rows), "new_this_month": sum(1 for c in created if c >= this_month),
                          "new_last_month": sum(1 for c in created if last_month <= c < this_month), "by_status": by_status},
            "money": {"revenue": revenue, "cost": cost, "usage_cost": usage_cost, "fixed_cost": fixed, "profit": revenue - cost,
                      "margin_pct": round((revenue - cost) / revenue * 100, 1) if revenue else None, "month": this_month.strftime("%B %Y")},
            "signups": signups, "by_plan": by_plan, "attention": attention[:8], "gst_registered": config.GST_REGISTERED}


def customer(s, tid: int) -> dict:
    t = s.get(Tenant, tid)
    if t is None:
        raise KeyError("customer not found")
    row = next(r for r in customers(s) if r["id"] == tid)
    people = [{"name": u.name, "email": u.email, "role": "Sales lead" if u.role == "admin" else "Sales rep",
               "last_login": u.last_login_at.isoformat() if u.last_login_at else None}
              for u in s.scalars(select(User).where(User.tenant_id == tid).order_by(User.id))]
    return {**row, "people": people, "details": t.plan_details, "requests": [r for r in requests(s) if r["tenant_id"] == tid][:5]}


def set_plan(s, tid: int, key: str, price: int | None) -> None:
    t = s.get(Tenant, tid)
    if t is None:
        raise KeyError("customer not found")
    if key not in PLANS:
        raise ValueError("plan must be trial, starter, growth or custom")
    if key == "custom" and (price is None or price <= 0):
        raise ValueError("enter the monthly price agreed for this custom plan")
    if price is not None and price < 0:
        raise ValueError("price can't be negative")
    plan.set_plan(t, key)
    t.plan_price = None if key == "trial" else (price if price is not None else pricing.PRESETS[key].get("price"))
    if key in ("starter", "growth"):
        p = pricing.PRESETS[key]
        t.plan_details = {"agents": p["agents"], "usage": p["usage"], "campaigns": p.get("campaigns")}
    elif key == "trial":
        t.plan_details = None
    t.plan_started_at = now()


# ---------- every login, across all companies ----------

def all_users(s) -> list[dict]:
    names = dict(s.execute(select(Tenant.id, Tenant.name)).all())
    return [{"id": u.id, "name": u.name, "email": u.email, "role": "Sales lead" if u.role == "admin" else "Sales rep", "tenant_id": u.tenant_id,
             "company": names.get(u.tenant_id, "?"), "created": u.created_at.isoformat() if u.created_at else None,
             "last_login": u.last_login_at.isoformat() if u.last_login_at else None, "disabled": bool(u.disabled),
             "owner": u.email in config.OWNER_EMAILS}
            for u in s.scalars(select(User).order_by(User.tenant_id, User.id))]


def _user(s, uid: int) -> User:
    u = s.get(User, uid)
    if u is None:
        raise KeyError("login not found")
    return u


def set_user(s, uid: int, password: str | None = None, disabled: bool | None = None) -> None:
    u = _user(s, uid)
    if disabled is not None:
        if disabled and u.email in config.OWNER_EMAILS:
            raise ValueError("an owner login can't be switched off here")
        u.disabled = disabled
        if disabled:
            user_svc._revoke(s, u.id)                 # signed out everywhere, right now
    if password:
        user_svc.update_user(s, u, password=password)  # hashed; other sessions end


def remove_user(s, uid: int) -> None:
    u = _user(s, uid)
    if u.email in config.OWNER_EMAILS:
        raise ValueError("an owner login can't be removed here")
    if u.role == "admin" and user_svc._admins(s, u.tenant_id) == 1:
        raise ValueError("this is the company's only sales lead; give someone else that role first")
    user_svc._revoke(s, u.id)
    s.delete(u)


# ---------- plan requests ----------

def requests(s) -> list[dict]:
    names = dict(s.execute(select(Tenant.id, Tenant.name)).all())
    out = []
    for a in s.scalars(select(Activity).where(Activity.type == "plan_request").order_by(Activity.id.desc()).limit(200)):
        d = a.data or {}
        out.append({"id": a.id, "tenant_id": a.tenant_id, "customer": names.get(a.tenant_id, "?"), "plan": d.get("plan"), "label": d.get("label"),
                    "cycle": d.get("cycle"), "monthly": d.get("monthly"), "once": d.get("once"), "lines": d.get("lines", []),
                    "by": d.get("requested_by"), "at": d.get("requested_at") or a.at.isoformat(), "status": d.get("status", "pending")})
    return out


def decide(s, request_id: int, approve: bool) -> dict:
    a = s.get(Activity, request_id)
    if a is None or a.type != "plan_request":
        raise KeyError("request not found")
    d = dict(a.data or {})
    if d.get("status", "pending") != "pending":
        raise ValueError("this request was already decided")
    if approve:
        set_plan(s, a.tenant_id, d["plan"], int(d.get("monthly") or 0) or None)
        t = s.get(Tenant, a.tenant_id)
        t.plan_details = {"agents": d.get("agents"), "usage": d.get("usage"), "campaigns": d.get("campaigns"), "cycle": d.get("cycle")}
    a.data = {**d, "status": "approved" if approve else "dismissed", "decided_at": now().isoformat(timespec="minutes")}
    return {"status": a.data["status"]}


# ---------- server health ----------

def version() -> str:
    if v := os.getenv("APP_VERSION"):
        return v
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PACKAGE.parent, capture_output=True, text=True, timeout=3).stdout.strip()
        if sha:
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    newest = max(p.stat().st_mtime for p in PACKAGE.rglob("*.py"))
    return "build " + datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")


def record_deployment(s) -> None:
    """Called once at start-up: a new row only when the code version changed since the last start."""
    v = version()
    last = s.scalars(select(Deployment).order_by(Deployment.id.desc()).limit(1)).first()
    if last is None or last.version != v:
        s.add(Deployment(version=v)); s.commit()


def _memory() -> dict:
    """App process memory, its container limit (Linux cgroup), and the whole machine's memory, in MB."""
    out = {"app_mb": None, "app_limit_mb": None, "machine_total_mb": None, "machine_used_mb": None}
    try:
        if Path("/proc/self/status").exists():
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    out["app_mb"] = int(line.split()[1]) // 1024
            info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
            total, avail = int(info["MemTotal"].split()[0]) // 1024, int(info["MemAvailable"].split()[0]) // 1024
            out.update(machine_total_mb=total, machine_used_mb=total - avail)
            for f in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
                if Path(f).exists() and (raw := Path(f).read_text().strip()).isdigit() and int(raw) < 1 << 50:
                    out["app_limit_mb"] = int(raw) // 2**20
                    break
        elif platform.system() == "Windows":
            class PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [(n, ctypes.c_size_t) for n in
                                                                                   ("peak", "ws", "a", "b", "c", "d", "pf", "ppf")]
            class MS(ctypes.Structure):
                _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong)] + \
                           [(n, ctypes.c_ulonglong) for n in ("tp", "ap", "tv", "av", "ae")]
            k32, ps = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            ps.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
            c = PMC(); c.cb = ctypes.sizeof(PMC)
            if ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                out["app_mb"] = c.ws // 2**20
            m = MS(); m.len = ctypes.sizeof(MS)
            if k32.GlobalMemoryStatusEx(ctypes.byref(m)):
                out.update(machine_total_mb=m.total // 2**20, machine_used_mb=(m.total - m.avail) // 2**20)
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    return out


def _database(s) -> dict:
    size = None
    if engine.dialect.name == "postgresql":
        size = s.execute(select(func.pg_database_size(func.current_database()))).scalar()
    elif engine.dialect.name == "sqlite" and engine.url.database and Path(engine.url.database).exists():
        size = Path(engine.url.database).stat().st_size
    rows = {t.name: s.execute(select(func.count()).select_from(t)).scalar()
            for t in Base.metadata.sorted_tables if t.name in ("tenants", "users", "contacts", "items", "quotes", "activities", "campaigns", "tasks")}
    return {"engine": engine.dialect.name, "size_mb": round(size / 2**20, 1) if size else None, "rows": rows}


def system(s) -> dict:
    disk = shutil.disk_usage(Path(config.MODEL_DIR).resolve().anchor if os.name == "nt" else "/")
    models = sum(p.stat().st_size for p in Path(config.MODEL_DIR).glob("*.joblib")) if Path(config.MODEL_DIR).exists() else 0
    deployments = [{"version": d.version, "at": d.started_at.isoformat(timespec="minutes")}
                   for d in s.scalars(select(Deployment).order_by(Deployment.id.desc()).limit(15))]
    companies = s.scalar(select(func.count()).select_from(Tenant))
    return {"version": version(), "started_at": STARTED.isoformat(timespec="minutes"), "uptime_h": round((now() - STARTED).total_seconds() / 3600, 1),
            "python": platform.python_version(), "memory": _memory(), "database": _database(s),
            "disk": {"total_gb": round(disk.total / 2**30, 1), "used_gb": round(disk.used / 2**30, 1), "free_gb": round(disk.free / 2**30, 1)},
            "models_mb": round(models / 2**20, 1), "deployments": deployments,
            "capacity": {"companies": companies, "comfortable": 25, "machine": "e2-medium"}}


def pricing_view(companies: int | None) -> dict:
    sheet = pricing.cost_sheet(companies)
    return {**sheet, "catalog": pricing.catalog(), "cost_gst": pricing.COST_GST, "gst_registered": config.GST_REGISTERED,
            "rates": {k: {p: round(c, 4) for p, c in v.items()} for k, v in pricing.COST.items()},
            "fixed": [{"label": "Server (e2-medium)", "amount": round(pricing.SERVER_MONTHLY * pricing.COST_GST)},
                      {"label": "Exotel account (only once you sell calls)", "amount": round(pricing.EXOTEL_MONTHLY * pricing.COST_GST)}],
            # sent only to the owner: the customer page never names the vendors behind the app
            "ai_note": f"AI cost assumes {pricing.TOKENS_IN:,} input + {pricing.TOKENS_OUT:,} output tokens per message on Gemini 3.1 Flash-Lite "
                       f"(${pricing.GEMINI_IN} / ${pricing.GEMINI_OUT} per million), ₹{pricing.USD_INR:g} per $."}
