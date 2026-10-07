"""Local development: seed a demo company with two sign-ins, start the API, open the sign-in page."""
import json
import os
import random
import threading
import webbrowser
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select

from .ai import llm
from .analytics.metrics import analyse
from .analytics.training import train_tenant
from .core.models import (
    Activity,
    Contact,
    Item,
    LoginToken,
    Quote,
    Session,
    Tenant,
    User,
    init_db,
)
from .core.playbook import setting
from .core.utils import now
from .services.catalog import import_catalog
from .services.contacts import import_contacts
from .services.quotes import _line as quote_line
from .services.quotes import import_orders
from .services.users import check_password, hash_password

PACKAGE_DIR = Path(__file__).resolve().parent
DEMO_DIR = PACKAGE_DIR / "demo"
DEMO_TENANT = "Demo Paints & Co"
# (email, name, role, password). The sales lead runs everything (role "admin" internally).
# Short password on purpose for local demos; real accounts need 8+ characters.
DEMO_USERS = [("sales@demo.in", "Ravi (Sales lead)", "admin", "sales")]
OLD_DEMO_EMAILS = ("owner@demopaints.in", "sales@demopaints.in", "admin@demo.in")


def _demo_users(s, tenant: Tenant) -> None:
    """Puts the demo sign-ins in place (also on older demo databases) without the real-account password rule."""
    for old in s.scalars(select(User).where(User.email.in_(OLD_DEMO_EMAILS))):
        s.execute(delete(LoginToken).where(LoginToken.user_id == old.id))
        s.delete(old)
    for email, name, role, password in DEMO_USERS:
        user = s.scalars(select(User).where(User.email == email)).first()
        if user is None:
            s.add(User(tenant_id=tenant.id, email=email, name=name, role=role, password_hash=hash_password(password)))
        else:
            user.role = role
            if not check_password(password, user.password_hash):
                user.password_hash = hash_password(password)
    s.commit()


DEMO_LEADS = ["Arun", "Bhavya", "Charan", "Deepa", "Elango", "Farah", "Gokul", "Harini", "Imran", "Janani", "Karthik",
              "Lavanya", "Mohan", "Nisha", "Om Prakash", "Pooja", "Rahul", "Sangeetha", "Tamil", "Uma", "Vijay", "Yamuna",
              "Zaheer", "Anitha", "Bala", "Divya", "Ganesh", "Kavya"]
DEMO_ASKS = ["price of white emulsion 20L?", "need waterproofing for terrace", "do you deliver to Velachery?",
             "quote for 10 tins exterior paint", "which putty for new walls?", "is the primer in stock?"]


def _demo_recent(s, tenant: Tenant) -> None:
    """Six weeks of enquiries, quotes, orders and payments, so the dashboard has a story on day one (demo only)."""
    for won in s.scalars(select(Quote).where(Quote.tenant_id == tenant.id, Quote.status == "won", Quote.paid_amount.is_(None))):
        won.closed_at, won.paid_amount, won.paid_at = won.created_at, won.total, won.created_at  # older demo databases
    if s.scalar(select(Contact.id).where(Contact.tenant_id == tenant.id, Contact.phone == "919000050000")):
        s.commit()
        return  # already added
    rnd, items, at_now = random.Random(7), list(s.scalars(select(Item).where(Item.tenant_id == tenant.id, Item.price >= 500))), now()
    tax_pct = setting(tenant, "pricing.tax_pct")
    sources = ["website_form", "meta_lead_ad", "indiamart", "referral", "website_form", "meta_lead_ad"]
    for i, name in enumerate(DEMO_LEADS):
        came = at_now - timedelta(days=rnd.uniform(0, 44), hours=rnd.uniform(0, 8))
        c = Contact(tenant_id=tenant.id, name=name, phone=f"91900005{i:04d}", source=rnd.choice(sources), created_at=came,
                    last_inbound_at=came, attrs={"city": rnd.choice(["Chennai", "Chennai", "Madurai", "Coimbatore"])})
        s.add(c)
        s.flush()
        s.add(Activity(tenant_id=tenant.id, contact_id=c.id, type="msg_in", channel="whatsapp", body=rnd.choice(DEMO_ASKS), at=came))
        s.add(Activity(tenant_id=tenant.id, contact_id=c.id, type="msg_out", channel="whatsapp", body="(demo reply)", at=came + timedelta(seconds=4),
                       data={"via": rnd.choice(["fastpath", "fastpath", "llm"]), "kind": "reply"}))
        if rnd.random() > .62 or not items:
            continue
        item, qty = rnd.choice(items), rnd.choice([2, 4, 6, 10, 15])
        quoted = min(at_now, came + timedelta(hours=rnd.uniform(1, 30)))
        line = quote_line(item.sku, item.name, qty, item.price)
        tax = round(line["amount"] * tax_pct / 100, 2)
        total = line["amount"] + tax
        q = Quote(tenant_id=tenant.id, contact_id=c.id, status="sent", subtotal=line["amount"], tax=tax, total=total,
                  created_at=quoted, valid_until=quoted + timedelta(days=7), lines=[line])
        roll = rnd.random()
        if roll < .5:
            q.status, q.closed_at = "won", min(at_now, quoted + timedelta(days=rnd.uniform(.5, 4)))
            pay = rnd.random()
            if pay < .85:
                q.paid_amount = total if pay < .68 else round(total / 2, 2)
                q.paid_at = min(at_now, q.closed_at + timedelta(days=rnd.uniform(0, 5)))
            c.stage = "won"
        elif roll < .68:
            q.status, q.closed_at, c.stage = "lost", min(at_now, quoted + timedelta(days=3)), "lost"
        else:
            c.stage = "quoted"
        s.add(q)
    s.commit()


def _demo(name: str) -> str:
    return (DEMO_DIR / name).read_text(encoding="utf-8")


def seed_demo() -> Tenant:
    """Creates the demo tenant with catalog, leads and order history once; returns it on later runs."""
    init_db()
    with Session() as s:
        tenant = s.scalars(select(Tenant).where(Tenant.name == DEMO_TENANT)).first()
        if tenant is None:
            tenant = Tenant(name=DEMO_TENANT, playbook=json.loads(_demo("playbook.json")))
            s.add(tenant)
            s.flush()
            import_catalog(s, tenant, _demo("catalog.csv"))
            import_contacts(s, tenant, _demo("contacts.csv"))
            import_orders(s, tenant, _demo("orders.csv"))
            s.commit()
            train_tenant(s, tenant)
            analyse(s, tenant)  # RFM segments + lead scores, so Strategy has audiences from the start
            s.commit()
        _demo_users(s, tenant)
        _demo_recent(s, tenant)
        return tenant


def run(host: str, port: int, open_browser: bool, reload: bool = False) -> None:
    import uvicorn

    os.environ.setdefault("ADMIN_KEY", "dev-admin")  # also inherited by the reloader's worker process
    tenant = seed_demo()
    url = f"http://{host}:{port}/"
    cfg = llm.settings()
    ai = f"{cfg.provider} / {cfg.model} (lite: {cfg.model_lite})"
    if cfg.key_env and not cfg.api_key:
        ai += f" - {cfg.key_env} NOT SET in .env; local models still answer routine messages"
    logins = "\n".join(f"            {email} / {password}  ({role})" for email, _, role, password in DEMO_USERS)
    print(f"\n  Sales Core running\n  UI        {url}\n  Sign in\n{logins}\n"
          f"  API docs  http://{host}:{port}/docs   (API key {tenant.api_key})\n"
          f"  Admin key {os.environ['ADMIN_KEY']}\n  AI        {ai}\n", flush=True)
    if open_browser:
        threading.Timer(2.0, webbrowser.open, [url]).start()
    # reload is opt-in: on some Windows shells the reloader cannot stop its worker and keeps serving old code
    uvicorn.run("salescore.api.app:app", host=host, port=port, reload=reload,
                reload_dirs=[str(PACKAGE_DIR)] if reload else None)
