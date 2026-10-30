"""Real-estate view of the same tables: a plot is a catalog item, a sale is a won order, a booking is an open quote,
a client is a broker who brings leads (a lead's attrs["client"] names theirs; source "lead" = our own lead)."""
from collections import defaultdict

from sqlalchemy import select

from ..core.models import Client, Contact, Item, Quote, Tenant
from ..core.playbook import setting
from .quotes import PAID_SLACK


def enabled(tenant: Tenant) -> bool:
    return setting(tenant, "industry") == "realestate"


def _sales(s, tid: int) -> tuple[dict, dict]:
    """(sku -> won order, sku -> open quote) with each customer attached. The latest quote wins when a plot has several."""
    sold, booked = {}, {}
    rows = s.execute(select(Quote, Contact).join(Contact, Contact.id == Quote.contact_id).where(
        Quote.tenant_id == tid, Quote.status.in_(("won", "sent", "pending_approval"))).order_by(Quote.id)).all()
    for q, c in rows:
        for ln in q.lines:
            (sold if q.status == "won" else booked)[ln["sku"]] = (q, c)
    return sold, booked


def _who(q: Quote, c: Contact) -> dict:
    paid = q.paid_amount or 0
    client = c.attrs.get("client")
    return {"quote_id": q.id, "contact_id": c.id, "name": c.name or c.phone, "phone": c.phone, "price": q.total, "paid": paid,
            "due": round(max(0, q.total - paid), 2), "date": (q.closed_at or q.created_at).date().isoformat(),
            "source": "Client" if client else "Our lead", "client": client or None}


def plots(s, tenant: Tenant) -> dict:
    sold, booked = _sales(s, tenant.id)
    ventures: dict[str, dict] = {}
    for item in s.scalars(select(Item).where(Item.tenant_id == tenant.id).order_by(Item.id)):
        a = item.attrs
        num = lambda k: int(float(a[k])) if a.get(k) else None  # noqa: E731  -- CSV attributes arrive as text
        status = "sold" if item.sku in sold else "booked" if item.sku in booked else "available"
        who = _who(*(sold.get(item.sku) or booked[item.sku])) if status != "available" else None
        v = ventures.setdefault(a.get("venture") or "Other", {"name": a.get("venture") or "Other", "location": a.get("location"),
                                                             "plots": [], "available": 0, "booked": 0, "sold": 0, "sold_value": 0.0})
        v[status] += 1
        v["sold_value"] += item.price if status == "sold" else 0
        v["plots"].append({"sku": item.sku, "no": num("plot_no"), "size": num("size_sqyd"), "facing": a.get("facing"),
                           "road": num("road_ft"), "corner": bool(a.get("corner")), "price": item.price, "status": status, "buyer": who})
    for v in ventures.values():
        v["sold_value"] = round(v["sold_value"], 2)
        v["plots"].sort(key=lambda p: p["no"] or 0)
    return {"ventures": list(ventures.values())}


def _score(leads: int, buyers: int, value: float, top_value: float, collected: float, billed: float) -> int:
    """0-100: how many of the leads they brought bought (50), how much they sold vs the best client (30), how much of it was paid (20)."""
    conversion = buyers / leads if leads else 0
    return round(50 * conversion + 30 * (value / top_value if top_value else 0) + 20 * (collected / billed if billed else 0))


def _lead_rows(s, tid: int, leads: list[Contact]) -> list[dict]:
    won = defaultdict(list)
    for q in s.scalars(select(Quote).where(Quote.tenant_id == tid, Quote.status == "won")):
        won[q.contact_id].append(q)
    rows = []
    for c in leads:
        orders = won[c.id]
        billed, paid = sum(q.total for q in orders), sum(q.paid_amount or 0 for q in orders)
        rows.append({"id": c.id, "name": c.name or c.phone, "phone": c.phone, "stage": c.stage, "score": c.score,
                     "plots": [ln["sku"] for q in orders for ln in q.lines], "value": round(billed, 2), "paid": round(paid, 2),
                     "due": round(billed - paid, 2) if billed - paid > PAID_SLACK else 0, "city": c.attrs.get("city"), "budget": c.attrs.get("budget")})
    return sorted(rows, key=lambda x: (-x["value"], -(x["score"] or 0)))


def _totals(leads: list[dict]) -> dict:
    return {"leads": len(leads), "buyers": sum(bool(x["plots"]) for x in leads), "plots": sum(len(x["plots"]) for x in leads),
            "value": round(sum(x["value"] for x in leads), 2), "paid": round(sum(x["paid"] for x in leads), 2),
            "due": round(sum(x["due"] for x in leads), 2), "list": leads}


def clients(s, tenant: Tenant) -> dict:
    """Clients (the brokers who bring leads), each with their leads' purchases, commission and a score; our own leads are listed apart."""
    by_client = defaultdict(list)
    for c in s.scalars(select(Contact).where(Contact.tenant_id == tenant.id)):
        by_client[c.attrs.get("client")].append(c)
    rows = []
    for cl in s.scalars(select(Client).where(Client.tenant_id == tenant.id).order_by(Client.id)):
        row = {"id": cl.id, "name": cl.name, "phone": cl.phone, "city": cl.city, "commission_pct": cl.commission_pct,
               **_totals(_lead_rows(s, tenant.id, by_client.pop(cl.name, [])))}
        row["commission"] = round(row["paid"] * cl.commission_pct / 100, 2)  # earned on the money actually received
        rows.append(row)
    top = max((r["value"] for r in rows), default=0)
    for r in rows:
        r["score"] = _score(r["leads"], r["buyers"], r["value"], top, r["paid"], r["value"])
    rows.sort(key=lambda r: -r["value"])
    own = _totals(_lead_rows(s, tenant.id, [c for k, group in by_client.items() for c in group]))  # no client, or one not on the list
    return {"clients": rows, "own": own, "leads": own["leads"] + sum(r["leads"] for r in rows),
            "sold_value": round(own["value"] + sum(r["value"] for r in rows), 2), "via_clients": round(sum(r["value"] for r in rows), 2),
            "commission": round(sum(r["commission"] for r in rows), 2), "due": round(own["due"] + sum(r["due"] for r in rows), 2)}
