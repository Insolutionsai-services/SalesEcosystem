"""Quotes and orders (an order is a quote with status 'won'). Priced from the tenant catalog, never by the LLM."""
from datetime import datetime, timedelta

from sqlalchemy import select, update

from ..core.models import Contact, Quote, Tenant
from ..core.playbook import setting
from ..core.utils import now, parse_csv, parse_date
from .activity import log_activity
from .catalog import get_items
from .contacts import find_or_create_contact, update_contact
from .tasks import add_task

PAID_SLACK = 0.5  # rupee rounding: an order within this of its total counts as paid


def _line(sku: str, name: str, qty: float, unit_price: float) -> dict:
    return {"sku": sku, "name": name, "qty": qty, "unit_price": unit_price, "amount": round(unit_price * qty, 2)}


def create_quote(s, tenant: Tenant, contact: Contact, lines: list[dict], discount_pct: float = 0,
                 note: str = "") -> Quote:
    """Catalog price × qty, then playbook.pricing limits. Over the auto-approve discount or short on stock
    or above the auto-send amount -> status pending_approval plus an approval task for a manager."""
    max_disc = setting(tenant, "pricing.max_discount_pct")
    if not lines:
        raise ValueError("quote needs at least one line")
    if not 0 <= discount_pct <= max_disc:
        raise ValueError(f"discount must be between 0 and {max_disc}% for this business")

    items = get_items(s, tenant.id, (ln["sku"] for ln in lines))
    priced, reasons = [], []  # anything here sends the quote to a person
    for ln in lines:
        item, qty = items.get(ln["sku"]), float(ln.get("qty", 1))
        if item is None:
            raise ValueError(f"unknown sku {ln['sku']!r}; use search_catalog")
        if qty <= 0:
            raise ValueError("qty must be positive")
        if item.stock is not None and qty > item.stock:
            reasons.append(f"{item.sku}: asked {qty:g}, in stock {item.stock:g}")
        priced.append(_line(item.sku, item.name, qty, item.price))

    subtotal = round(sum(ln["amount"] for ln in priced), 2)
    net = subtotal * (1 - discount_pct / 100)
    tax = round(net * setting(tenant, "pricing.tax_pct") / 100, 2)
    total = round(net + tax, 2)
    limit = setting(tenant, "pricing.auto_approve_max_total")  # 0 = no amount limit
    if limit and total > limit:
        reasons.append(f"total {total:,.0f} is above the auto-send limit {limit:,.0f}")
    needs_approval = discount_pct > setting(tenant, "pricing.auto_approve_discount_pct") or bool(reasons)
    quote = Quote(tenant_id=tenant.id, contact_id=contact.id, lines=priced, subtotal=subtotal,
                  discount_pct=discount_pct, tax=tax, total=total,
                  status="pending_approval" if needs_approval else "sent",
                  note="; ".join(filter(None, [note, *reasons])) or None,
                  valid_until=now() + timedelta(days=setting(tenant, "pricing.validity_days")))
    s.add(quote)
    s.flush()
    if needs_approval:
        add_task(s, tenant.id, contact.id, "approval", quote_id=quote.id,
                 reason=quote.note or f"discount {discount_pct:g}%")
    log_activity(s, tenant.id, contact.id, "quote", quote_id=quote.id, total=quote.total, status=quote.status)
    return quote


def format_quote(quote: Quote, currency: str) -> str:
    rows = [f"Quote #{quote.id}"]
    rows += [f"- {ln['name']} × {ln['qty']:g} @ {ln['unit_price']:,.2f} = {ln['amount']:,.2f}" for ln in quote.lines]
    if quote.discount_pct:
        rows.append(f"Discount: {quote.discount_pct:g}%")
    if quote.tax:
        rows.append(f"Tax: {quote.tax:,.2f}")
    rows.append(f"Total: {currency} {quote.total:,.2f} (valid till {quote.valid_until:%d %b %Y})")
    return "\n".join(rows)


def close_quote(s, tenant: Tenant, quote: Quote, status: str, reason: str = "") -> None:
    """Sales team records the outcome; won/lost labels are what lead scoring learns from."""
    if status not in ("won", "lost"):
        raise ValueError("status must be won or lost")
    quote.status, quote.note = status, "; ".join(filter(None, [quote.note, reason])) or None
    quote.closed_at = now()
    contact = s.get(Contact, quote.contact_id)
    if status in (setting(tenant, "stages") or [status]):
        update_contact(s, tenant, contact, stage=status)
    log_activity(s, tenant.id, contact.id, "quote", quote_id=quote.id, status=status, reason=reason)


def record_payment(s, tenant: Tenant, quote: Quote, amount: float) -> Quote:
    """Money received against an order (full or part)."""
    if quote.status != "won":
        raise ValueError("payments are recorded against won orders")
    due = round(quote.total - (quote.paid_amount or 0), 2)
    if amount <= 0:
        raise ValueError("enter the amount received")
    if amount > due + PAID_SLACK:
        raise ValueError(f"only {due:,.2f} is still due on this order")
    quote.paid_amount, quote.paid_at = round((quote.paid_amount or 0) + amount, 2), now()
    log_activity(s, tenant.id, quote.contact_id, "payment", quote_id=quote.id, amount=amount, due=round(due - amount, 2))
    return quote


def expire_quotes(s, tenant_id: int, at: datetime) -> None:
    s.execute(update(Quote).where(Quote.tenant_id == tenant_id, Quote.status == "sent", Quote.valid_until < at)
              .values(status="expired"))


def import_orders(s, tenant: Tenant, text: str) -> int:
    """Past orders (phone/email, sku, qty, optional price/item/date) become won quotes: history for ML and retention."""
    rows = parse_csv(text)
    items = get_items(s, tenant.id, {r.get("sku") for r in rows})
    for row in rows:
        contact = find_or_create_contact(s, tenant, row.get("phone"), row.get("email"), row.get("name") or None, "orders")
        item = items.get(row.get("sku"))
        if not row.get("price") and item is None:
            raise ValueError(f"order row has no price and unknown sku: {row}")
        line = _line(row["sku"], row.get("item") or (item.name if item else row["sku"]), float(row.get("qty") or 1),
                     float(row["price"]) if row.get("price") else item.price)
        at = parse_date(row.get("date")) or now()
        paid = float(row["paid"]) if row.get("paid") else line["amount"]  # past orders count as paid unless a `paid` column says otherwise
        s.add(Quote(tenant_id=tenant.id, contact_id=contact.id, status="won", lines=[line], subtotal=line["amount"],
                    total=line["amount"], created_at=at, closed_at=at, paid_amount=paid, paid_at=at if paid else None))
    return len(rows)


def list_quotes(s, tenant_id: int, status: str | None = None, limit: int = 500) -> list[Quote]:
    q = select(Quote).where(Quote.tenant_id == tenant_id).order_by(Quote.created_at.desc(), Quote.id.desc())
    if status:
        q = q.where(Quote.status == status)
    return s.scalars(q.limit(limit)).all()
