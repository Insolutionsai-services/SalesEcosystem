"""A customer sends a photo of what they want (handwritten list, bill, product shot). The agent reads it, checks the catalog and
replies with a quote to confirm: what is available, how many, and the other makes and types the company stocks."""
import re

from sqlalchemy import select

from ..core import agents
from ..core.models import Contact, Quote, Tenant
from ..core.playbook import setting
from ..core.utils import now
from ..services import channels, quotes
from ..services.activity import log_activity
from ..services.catalog import search_catalog
from ..services.tasks import close_open_tasks
from .followups import schedule_followup

YES = re.compile(r"\s*(yes|y|yep|yeah|confirm|confirmed|ok|okay|sure|haan)\W*", re.IGNORECASE)
MARK = "photo order"  # on the quote's note: only these are confirmed by a bare "yes"


def read_items(data: bytes) -> list[dict]:
    """The lines on the photo as [{text, qty}]."""
    # ponytail: mock. Swap for a vision model returning this shape, e.g. a free OpenRouter vision model
    return [{"text": "white emulsion 20L", "qty": 2}, {"text": "wall putty 40kg", "qty": 5}, {"text": "exterior emulsion 20L", "qty": 1}]


def _stock(item: dict, qty: float) -> str:
    stock = item.get("stock")
    if stock is None:
        return "available"
    if stock <= 0:
        return "out of stock"
    return f"in stock ({stock:g} available)" if stock >= qty else f"only {stock:g} in stock"


def handle_photo(s, tenant: Tenant, contact: Contact, image: str, data: bytes) -> str | None:
    """Logs the photo as the customer's message, then sends the quote to confirm. None when a person owns the chat."""
    contact.last_inbound_at = now()
    log_activity(s, tenant.id, contact.id, "msg_in", body="Sent a photo", channel="ui", image=image)
    close_open_tasks(s, contact.id, "followup", status="cancelled")
    if contact.bot_paused or not agents.enabled(tenant, "response"):  # a person owns the chat, or replies are switched off
        return None
    lines, picked, currency = [], [], setting(tenant, "currency")
    for n, want in enumerate(read_items(data), 1):
        hits = search_catalog(s, tenant.id, want["text"], k=4)
        if not hits:
            lines.append(f"{n}. {want['text']} × {want['qty']:g}: not in our catalog, a team member will check.")
            continue
        top, others = hits[0], hits[1:3]
        picked.append({"sku": top["sku"], "qty": want["qty"]})
        text = (f"{n}. {want['text']} × {want['qty']:g}\n   {top['name']}: {currency} {top['price']:,.0f} each, {_stock(top, want['qty'])}")
        if others:
            text += "\n   Also available: " + "; ".join(f"{o['name']} ({currency} {o['price']:,.0f})" for o in others)
        lines.append(text)
    if not picked:
        reply = "Thanks for the photo. I couldn't match anything in it to our products. Could you type what you need, or send a clearer picture?"
    else:
        quote = quotes.create_quote(s, tenant, contact, picked, note=MARK)
        ask = ("Our team is confirming availability and will get back to you shortly." if quote.status == "pending_approval"
               else "Please check the items and quantities. Reply YES to confirm the order, or tell me what to change.")
        reply = (f"Thanks! I read your photo and found {len(lines)} item{'s' if len(lines) != 1 else ''}:\n" + "\n".join(lines)
                 + f"\n\n{quotes.format_quote(quote, currency)}\n\n{ask}")
    if channels.send(s, tenant, contact, reply, "reply", via="photo"):
        schedule_followup(s, tenant, contact, step=0)
    return reply


def confirm(s, tenant: Tenant, contact: Contact, text: str) -> str | None:
    """A plain "yes" to the quote we sent from their photo places the order."""
    if not YES.fullmatch(text or ""):
        return None
    quote = s.scalars(select(Quote).where(Quote.contact_id == contact.id, Quote.status == "sent")
                      .order_by(Quote.id.desc())).first()
    if quote is None or not (quote.note or "").startswith(MARK):
        return None
    quotes.close_quote(s, tenant, quote, "won")
    currency = setting(tenant, "currency")
    return f"Confirmed! Order #{quote.id} is placed for {currency} {quote.total:,.2f}. We'll share the payment details and delivery time shortly."
