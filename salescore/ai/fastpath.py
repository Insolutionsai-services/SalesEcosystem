"""Answers routine messages locally with the tenant's trained models: ~milliseconds, zero LLM cost.
Anything uncertain returns None and goes to the LLM. Prices and stock always come live from the database."""
from dataclasses import dataclass

from ..core.models import Contact, Item, Tenant
from ..core.playbook import setting
from ..core.utils import first_name
from ..ml import store
from ..ml.intents import extract_qty, product_query
from ..services import quotes
from ..services.catalog import get_items

SMALL_TALK_MAX_WORDS = 5  # "hi, which colour suits my bedroom?" is a question, not a greeting


@dataclass
class FastAnswer:
    text: str
    intent: str
    handoff: bool = False


def _tpl(tenant: Tenant, key: str, **values) -> str:
    return setting(tenant, f"templates.{key}").format(**values)


def _stock_line(tenant: Tenant, item: Item) -> str:
    if item.stock is None:
        return ""
    return _tpl(tenant, "in_stock", stock=f"{item.stock:g}") if item.stock > 0 else _tpl(tenant, "out_of_stock")


def _product_answer(s, tenant: Tenant, contact: Contact, brain: store.Brain, intent: str, text: str) -> FastAnswer | None:
    match = brain.catalog.best(product_query(text), setting(tenant, "fastpath.catalog_min_score"),
                               setting(tenant, "fastpath.catalog_min_margin"))
    if match is None:
        return None  # no product, or two products too close to call: the LLM asks a clarifying question
    item = get_items(s, tenant.id, [match[0]]).get(match[0])
    if item is None:
        return None
    currency, qty = setting(tenant, "currency"), extract_qty(text)
    if intent == "price" and qty:
        q = quotes.create_quote(s, tenant, contact, [{"sku": item.sku, "qty": qty}])
        if q.status == "sent":
            return FastAnswer(_tpl(tenant, "quote_sent", quote=quotes.format_quote(q, currency)), "quote")
        return FastAnswer(_tpl(tenant, "quote_pending", item=item.name), "quote")
    stock = _stock_line(tenant, item)
    if intent == "stock":
        return FastAnswer(f"{item.name}: {stock}" if stock else f"{item.name} is available to order.", intent)
    tax = setting(tenant, "pricing.tax_pct")
    return FastAnswer(_tpl(tenant, "price", item=item.name, price=f"{currency} {item.price:,.2f}",
                           tax=f" + {tax:g}% tax" if tax else "", stock=stock).replace("  ", " "), intent)


def answer(s, tenant: Tenant, contact: Contact, text: str) -> FastAnswer | None:
    if not setting(tenant, "fastpath.enabled") or (brain := store.load(tenant.id)) is None:
        return None
    intent, confidence = brain.intents.predict(text)
    confident = confidence >= setting(tenant, "fastpath.min_confidence")

    if confident and intent == "human":
        return FastAnswer(setting(tenant, "handoff_reply"), intent, handoff=True)
    if confident and intent in ("price", "stock"):
        if found := _product_answer(s, tenant, contact, brain, intent, text):
            return found
    if brain.faq and (hit := brain.faq.best(text, setting(tenant, "fastpath.faq_min_score"),
                                            setting(tenant, "fastpath.faq_min_margin"))):
        return FastAnswer(brain.faq_answers[hit[0]], "faq")
    if confident and intent in ("greeting", "thanks") and len(text.split()) <= SMALL_TALK_MAX_WORDS:
        return FastAnswer(_tpl(tenant, intent, name=first_name(contact.name), business=tenant.name), intent)
    return None
