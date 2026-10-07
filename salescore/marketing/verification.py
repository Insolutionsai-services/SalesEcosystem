"""Verification agent: checks every price, discount and claim in outgoing copy against the catalog and the
company's rules BEFORE a human sees it. Plain code, never an LLM - a hallucinated price cannot pass."""
import re

from sqlalchemy import select

from ..core.models import Item, Tenant
from ..core.playbook import setting

MONEY = re.compile(r"(?:₹|\brs\.?|\binr)\s*([\d,]+(?:\.\d+)?)", re.I)
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
ALLOWED_PLACEHOLDERS = {"name"}
DEFAULT_BANNED = ["guaranteed returns", "100% guaranteed", "risk-free", "risk free", "lowest price guaranteed",
                  "no questions asked", "free forever"]
WHATSAPP_BODY_LIMIT = 1000


def _amount(raw: str) -> float:
    return float(raw.replace(",", ""))


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1.0, 0.005 * b)  # rounding tolerance: 1 unit or 0.5%


def verify(s, tenant: Tenant, text: str, personal: bool = True) -> dict:
    """{"ok": bool, "errors": [...], "warnings": [...]}. Errors block approval; warnings are shown to the manager."""
    errors, warnings = [], []
    lower = text.lower()
    tax = setting(tenant, "pricing.tax_pct")
    max_disc, auto_disc = setting(tenant, "pricing.max_discount_pct"), setting(tenant, "pricing.auto_approve_discount_pct")

    percents = [float(p) for p in PERCENT.findall(text)]
    for pct in percents:
        if pct == tax or pct == 100:  # "18% GST", "100% cotton"
            continue
        if pct > max_disc:
            errors.append(f"{pct:g}% discount exceeds this company's maximum of {max_disc:g}%")
        elif pct > auto_disc:
            warnings.append(f"{pct:g}% discount is above the {auto_disc:g}% auto-approve limit")

    prices = [i.price for i in s.scalars(select(Item).where(Item.tenant_id == tenant.id))]
    discounts = [p / 100 for p in percents if p <= max_disc]
    factors = [1.0] + [1 - d for d in discounts] + discounts  # full price, discounted price, the saving itself
    allowed = [p * f * t for p in prices for f in factors for t in (1.0, 1 + tax / 100)]
    for raw in MONEY.findall(text):
        amount = _amount(raw)
        if not any(_close(amount, a) for a in allowed):
            errors.append(f"price {raw} does not match any catalog price (with the stated discount/tax)")

    for phrase in DEFAULT_BANNED + setting(tenant, "compliance.banned_phrases"):
        if phrase.lower() in lower:
            errors.append(f"banned claim: '{phrase}'")
    for phrase in setting(tenant, "compliance.required_phrases"):
        if phrase.lower() not in lower:
            errors.append(f"required text missing: '{phrase}'")

    unknown = set(PLACEHOLDER.findall(text)) - ALLOWED_PLACEHOLDERS
    if unknown:
        errors.append(f"unfilled placeholders: {sorted(unknown)}")
    if personal and "{name}" not in text:
        warnings.append("not personalised: no {name}")
    if len(text) > WHATSAPP_BODY_LIMIT:
        warnings.append(f"{len(text)} characters: long for WhatsApp (keep under {WHATSAPP_BODY_LIMIT})")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def verify_all(s, tenant: Tenant, variants: list[dict], poster: dict | None = None) -> dict:
    """Checks every variant (and the poster's words); issues are prefixed with where they were found."""
    merged = {"ok": True, "errors": [], "warnings": []}
    items = [(v["label"], v["text"], True) for v in variants]
    if poster:
        items.append(("poster", " ".join(poster.get(k, "") for k in ("headline", "subline", "cta")), False))
    for label, text, personal in items:
        check = verify(s, tenant, text, personal)
        merged["ok"] &= check["ok"]
        merged["errors"] += [f"{label}: {e}" for e in check["errors"]]
        merged["warnings"] += [f"{label}: {w}" for w in check["warnings"]]
    if not variants:
        merged.update(ok=False, errors=["no copy was produced"])
    return merged
