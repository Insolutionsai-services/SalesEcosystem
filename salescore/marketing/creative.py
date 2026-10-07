"""Creative agent: writes the campaign copy (two A/B variants), poster words and an image brief, from real catalog facts only."""
import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select

from ..ai import agent, llm
from ..core.models import Item, Quote, Tenant
from ..core.playbook import setting
from ..core.utils import to_json

MAX_CHARS = 600
JSON_OBJECT = re.compile(r"\{.*\}", re.S)

SYSTEM = f"""You are the marketing copywriter of the business in the PLAYBOOK. Write ONE campaign as JSON only:
{{"variants": [{{"label": "A", "angle": "...", "text": "..."}}, {{"label": "B", "angle": "...", "text": "..."}}],
  "poster": {{"headline": "max 6 words", "subline": "the offer in max 14 words", "cta": "max 4 words, e.g. Reply to order"}},
  "image_brief": "one paragraph describing a matching image or short video for social/WhatsApp"}}
Rules:
- Two genuinely different angles (e.g. benefit-led vs offer-led). Each text under {MAX_CHARS} characters, WhatsApp style.
- Start with "Hi {{name}}," ({{name}} is replaced per customer). No other placeholders, no links.
- Use ONLY products, prices and facts given below. Write prices exactly as given (e.g. INR 6,100) or omit them.
- Never promise anything not in the facts or the offer; no "guaranteed" claims.
- Match the playbook tone and language. Do not add an opt-out line (added automatically)."""


def product_facts(s, tenant: Tenant, skus: list[str], limit: int = 6) -> list[dict]:
    """The products the copy may mention: the play's SKUs, else the best sellers."""
    items = s.scalars(select(Item).where(Item.tenant_id == tenant.id)).all()
    if not skus:
        sold = {}
        for q in s.scalars(select(Quote).where(Quote.tenant_id == tenant.id, Quote.status == "won")):
            for ln in q.lines:
                sold[ln["sku"]] = sold.get(ln["sku"], 0) + ln["amount"]
        skus = sorted(sold, key=sold.get, reverse=True)[:limit]
    chosen = [i for i in items if i.sku in skus] or items[:limit]
    return [{"sku": i.sku, "name": i.name, "price": f"{setting(tenant, 'currency')} {i.price:,.0f}",
             "in_stock": i.stock is None or i.stock > 0, **i.attrs} for i in chosen[:limit]]


@dataclass
class Draft:
    variants: list[dict]
    image_brief: str | None = None
    poster: dict = field(default_factory=dict)  # {"headline", "subline", "cta"} for the poster designer


def parse(text: str | None) -> Draft:
    """Model output -> Draft. Tolerates prose around the JSON; falls back to one plain variant."""
    if not text:
        return Draft([])
    if m := JSON_OBJECT.search(text):
        try:
            data = json.loads(m.group(0))
            variants = [{"label": v.get("label") or chr(65 + i), "angle": v.get("angle", ""), "text": v["text"].strip()}
                        for i, v in enumerate(data.get("variants", [])) if v.get("text")]
            poster = {k: str(v).strip() for k, v in (data.get("poster") or {}).items() if k in ("headline", "subline", "cta") and v}
            if variants:
                return Draft(variants[:2], data.get("image_brief"), poster)
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
            pass
    return Draft([{"label": "A", "angle": "single", "text": text.strip()}])


def offer_line(text: str, limit: int = 110) -> str:
    """The opening of a message as a poster line: greeting removed, whole sentences up to about `limit` characters."""
    body = text.replace("Hi {name},", "").replace("{name}", "").strip()
    out = ""
    for sentence in body.replace("! ", "!|").replace(". ", ".|").split("|"):
        if out and len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    return out[:limit]


def poster_words(draft: Draft, name: str, offer: str) -> dict:
    """Poster text: the AI's words when it wrote them, else the campaign name and the start of the offer."""
    return {"headline": draft.poster.get("headline") or name, "subline": draft.poster.get("subline") or offer_line(offer),
            "cta": draft.poster.get("cta") or "Reply to order"}


def write(s, tenant: Tenant, goal: str, offer: str, audience_note: str, skus: list[str]) -> Draft:
    brief = to_json({"goal": goal, "offer": offer, "audience": audience_note,
                     "products": product_facts(s, tenant, skus), "tax_pct": setting(tenant, "pricing.tax_pct")})
    reply = llm.chat([{"role": "system", "content": f"{SYSTEM}\n\nPLAYBOOK:\n{to_json(tenant.playbook)}"},
                      {"role": "user", "content": f"CAMPAIGN BRIEF:\n{brief}"}])
    agent.record_usage(s, tenant, None, "creative", reply)
    draft = parse(reply.text)
    draft.variants = draft.variants[: int(setting(tenant, "creative.variants"))]
    return draft


def manual(text: str) -> Draft:
    """The manager's own words are the message (chosen in the form, or because Creative is switched off)."""
    text = text.strip()
    return Draft([{"label": "A", "angle": "written by you", "text": text if "{name}" in text else f"Hi {{name}}, {text}"}])
