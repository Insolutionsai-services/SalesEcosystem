"""The tenant's catalog: anything they sell (SKU, plot, service, plan). Prices live here, nowhere else."""
from sqlalchemy import select

from ..core.models import Item, Tenant
from ..core.playbook import setting
from ..core.utils import parse_csv
from ..ml import store
from ..ml.text_index import TextIndex

MIN_MATCH = 0.05


def item_dict(item: Item) -> dict:
    return {"sku": item.sku, "name": item.name, "description": item.description, "price": item.price,
            "currency": item.currency, "stock": item.stock, **item.attrs}


def item_doc(item: Item) -> str:
    """The text an item is matched on."""
    return " ".join(map(str, [item.sku, item.name, item.description or "", *item.attrs.values()]))


def get_items(s, tenant_id: int, skus) -> dict[str, Item]:
    return {i.sku: i for i in s.scalars(select(Item).where(Item.tenant_id == tenant_id, Item.sku.in_(list(skus))))}


def build_index(s, tenant_id: int) -> TextIndex:
    items = s.scalars(select(Item).where(Item.tenant_id == tenant_id)).all()
    return TextIndex([i.sku for i in items], [item_doc(i) for i in items])


def catalog_index(s, tenant_id: int) -> TextIndex:
    """The trained index (instant); built on the fly only if the tenant has never been trained."""
    brain = store.load(tenant_id)
    return brain.catalog if brain else build_index(s, tenant_id)


def search_catalog(s, tenant_id: int, query: str, k: int = 5) -> list[dict]:
    """Fuzzy match free text ("2 inch pvc elbow", "east facing plot"). Price and stock are read live from the DB."""
    # ponytail: TF-IDF char n-grams are fine to ~50k items; move to pgvector + embeddings beyond that
    hits = catalog_index(s, tenant_id).search(query, k, MIN_MATCH)
    items = get_items(s, tenant_id, [sku for sku, _ in hits])
    return [item_dict(items[sku]) | {"match": round(score, 3)} for sku, score in hits if sku in items]


def import_catalog(s, tenant: Tenant, text: str) -> int:
    """CSV with sku, name, price; optional description, currency, stock; other columns become attributes."""
    rows = parse_csv(text)
    existing = get_items(s, tenant.id, [r.get("sku") for r in rows])
    for row in rows:
        if not row.get("sku") or not row.get("name") or not row.get("price"):
            raise ValueError(f"catalog row needs sku, name, price: {row}")
        sku = row.pop("sku")
        item = existing.get(sku) or Item(tenant_id=tenant.id, sku=sku)
        item.name, item.price = row.pop("name"), float(row.pop("price"))
        item.description = row.pop("description", None) or None
        item.currency = row.pop("currency", None) or setting(tenant, "currency")
        stock = row.pop("stock", None)
        item.stock = float(stock) if stock else None
        item.attrs = {k: v for k, v in row.items() if v}
        s.add(item)
        existing[sku] = item
    return len(rows)


def list_items(s, tenant_id: int, limit: int = 1000) -> list[dict]:
    return [item_dict(i) for i in s.scalars(select(Item).where(Item.tenant_id == tenant_id).order_by(Item.sku).limit(limit))]
