"""The real-estate demo company (GYC Estates): two ventures of 25 plots, clients (brokers) and the leads they brought, sales and
bookings, all from the CSV files in demo/estates. Signs in as re@demo.in / estate. Added straight to the database, no OTP."""
import csv
import io
import json
from datetime import datetime, timedelta

from sqlalchemy import select

from .analytics.metrics import analyse
from .analytics.training import train_tenant
from .core.models import Activity, Client, Contact, Item, Session, Tenant, User
from .core.utils import norm_phone, now, parse_csv
from .dev import DEMO_DIR
from .services.catalog import import_catalog
from .services.contacts import import_contacts
from .services.quotes import create_quote, import_orders
from .services.users import check_password, hash_password

ESTATES_DIR = DEMO_DIR / "estates"
ESTATES_TENANT = "GYC Estates"
ESTATES_USER = ("re@demo.in", "Sales lead (Estates)", "admin", "estate")
DEMAND_CHATS = {  # lead index -> what they wrote: the AI suggestions read budget, size, facing and venture from these
    6: "looking for east facing plot under 40 lakhs in lakeview heights", 7: "need a 200 yard east facing plot, budget 38 lakhs",
    15: "any east facing plots below 40L? want to visit this weekend", 17: "interested in 240 sq yd plot in sunrise meadows around 35 lakhs",
    18: "budget 40L, east facing only", 21: "looking for plot near 30 lakhs, north facing, 150 yard",
    22: "west facing plot in sunrise meadows under 35L", 23: "need east facing 200 yd plot under 40L", 4: "looking for 267 sq yd plot in lakeview heights, budget 60 lakhs"}
ASKS = [("is plot 14 in sunrise meadows available?", "Plot 14 in Sunrise Meadows is available. Shall I arrange a site visit?"),
        ("rate of east facing plot in lakeview heights", "East facing plots in Lakeview Heights start from the price on the layout. Which size do you prefer?"),
        ("what is the booking amount", "Pay a token amount of 1 lakh to block a plot for 7 days, then 30% within 15 days. The balance is by registration.")]


def _read(name: str) -> str:
    return (ESTATES_DIR / name).read_text(encoding="utf-8")


def _csv(rows: list[dict]) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, rows[0].keys())
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def seed_estates(s=None) -> Tenant:
    """Creates the company once; later runs only make sure the sign-in exists."""
    if s is None:
        with Session() as own:
            return seed_estates(own)
    tenant = s.scalars(select(Tenant).where(Tenant.name == ESTATES_TENANT)).first()
    if tenant is None:
        tenant = Tenant(name=ESTATES_TENANT, plan="unlimited", playbook=json.loads(_read("playbook.json")))
        s.add(tenant)
        s.flush()
        import_catalog(s, tenant, _read("plots.csv"))
        s.flush()
        for a in s.scalars(select(Activity).where(Activity.tenant_id == tenant.id, Activity.type == "stock")):
            a.at = now() - timedelta(days=60)  # the plots went on sale two months ago
        for r in parse_csv(_read("brokers.csv")):
            s.add(Client(tenant_id=tenant.id, name=r["name"], phone=r["phone"], email=r["email"], city=r["city"], commission_pct=float(r["commission_pct"])))
        leads = import_contacts(s, tenant, _read("leads.csv"))  # the leads: ours, and the ones each client brought
        at = now()
        sales = parse_csv(_read("sales.csv"))
        for r in sales:  # days_ago keeps the demo fresh whenever it is seeded
            r["date"] = (at - timedelta(days=int(r.pop("days_ago")))).date().isoformat()
        import_orders(s, tenant, _csv(sales))
        s.flush()
        skus = {r["sku"] for r in sales}
        for item in s.scalars(select(Item).where(Item.tenant_id == tenant.id, Item.sku.in_(skus))):
            item.stock = 0  # sold
        first_buy = {norm_phone(r["phone"], "91"): datetime.fromisoformat(r["date"]) for r in sales}
        for i, c in enumerate(leads):  # leads (people) -- enquiries came in a week or two before they bought; the rest are spread over six weeks
            c.created_at = first_buy[c.phone] - timedelta(days=9 + i % 6) if c.phone in first_buy else at - timedelta(days=(i * 5) % 44 + 2)
            c.last_inbound_at = c.created_at + timedelta(days=1)
            if c.phone in first_buy:
                c.stage = "won"
        for b in parse_csv(_read("bookings.csv")):
            c = next(x for x in leads if x.phone == norm_phone(b["phone"], "91"))
            create_quote(s, tenant, c, [{"sku": b["sku"], "qty": 1}])
            c.stage = "quoted"
        for (ask, reply), c in zip(ASKS, leads[3:]):
            s.add(Activity(tenant_id=tenant.id, contact_id=c.id, type="msg_in", channel="whatsapp", body=ask, at=at - timedelta(hours=5)))
            s.add(Activity(tenant_id=tenant.id, contact_id=c.id, type="msg_out", channel="whatsapp", body=reply, at=at - timedelta(hours=5, minutes=-1),
                           data={"via": "fastpath", "kind": "reply"}))
        s.commit()
        _demand_chats(s, tenant)  # before training, so the intent examples see them
        s.commit()
        train_tenant(s, tenant)
        analyse(s, tenant)
        s.commit()
    _demand_chats(s, tenant)  # older databases
    s.commit()
    email, name, role, password = ESTATES_USER
    user = s.scalars(select(User).where(User.email == email)).first()
    if user is None:
        s.add(User(tenant_id=tenant.id, email=email, name=name, role=role, password_hash=hash_password(password)))
    elif not check_password(password, user.password_hash):
        user.password_hash = hash_password(password)
    s.commit()
    return tenant



def _demand_chats(s, tenant: Tenant) -> None:
    """What leads wrote about budget, size and facing (once; also fills databases seeded before these messages existed)."""
    if s.scalars(select(Activity.id).where(Activity.tenant_id == tenant.id, Activity.body == DEMAND_CHATS[6])).first():
        return
    leads = s.scalars(select(Contact).where(Contact.tenant_id == tenant.id).order_by(Contact.id).limit(24)).all()
    for i, text in DEMAND_CHATS.items():
        at = now() - timedelta(days=(i * 3) % 12, hours=3)
        s.add(Activity(tenant_id=tenant.id, contact_id=leads[i].id, type="msg_in", channel="whatsapp", body=text, at=at))
        leads[i].last_inbound_at = at
