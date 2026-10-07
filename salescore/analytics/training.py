"""Learns a tenant's business before it goes live: catalog index, FAQ index, intent model, lead-scoring model.
Runs after imports, on playbook save, daily, or `make train`. Real-time paths only *use* what this builds."""
import time

from sqlalchemy import select

from ..core.models import Item, Tenant
from ..core.utils import now
from ..ml import store
from ..ml.intents import IntentModel
from ..ml.text_index import TextIndex
from ..services.catalog import item_doc
from . import scoring
from .metrics import load_frames


def faq_pairs(playbook: dict) -> list[tuple[str, str]]:
    """playbook.faq as [{"q": ..., "a": ...}]; a free-text faq stays LLM-only context."""
    faq = playbook.get("faq")
    return [(f["q"], f["a"]) for f in faq if f.get("q") and f.get("a")] if isinstance(faq, list) else []


def train_tenant(s, tenant: Tenant) -> dict:
    started = time.perf_counter()
    items = s.scalars(select(Item).where(Item.tenant_id == tenant.id)).all()
    faq = faq_pairs(tenant.playbook)
    frames = load_frames(s, tenant.id)
    lead_model = scoring.fit_lead_model(*frames, now())
    intents = IntentModel(tenant.playbook.get("intent_examples"))

    stats = {"items": len(items), "faq": len(faq), "intent_examples": len(intents.texts),
             "contacts": len(frames[0]), "lead_model": "gradient_boosting" if lead_model is not None else "heuristic"}
    brain = store.Brain(trained_at=now(), catalog=TextIndex([i.sku for i in items], [item_doc(i) for i in items]),
                        faq=TextIndex(list(range(len(faq))), [q for q, _ in faq]) if faq else None,
                        faq_answers=[a for _, a in faq], intents=intents, lead_model=lead_model, stats=stats)
    stats["seconds"] = round(time.perf_counter() - started, 3)
    store.save(tenant.id, brain)
    return {"trained_at": brain.trained_at, **stats}


def brain_status(tenant: Tenant) -> dict:
    brain = store.load(tenant.id)
    return {"trained": False} if brain is None else {"trained": True, "trained_at": brain.trained_at, **brain.stats}
