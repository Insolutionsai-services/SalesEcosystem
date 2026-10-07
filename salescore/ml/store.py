"""Per-tenant trained artifacts ("brain"): saved to disk with joblib, kept in memory for real-time use.
Reloaded automatically when the file changes (e.g. retrained by another process)."""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import joblib

from ..core.config import MODEL_DIR
from .intents import IntentModel
from .text_index import TextIndex


@dataclass
class Brain:
    trained_at: datetime
    catalog: TextIndex                 # keys: SKU
    faq: TextIndex | None              # keys: index into faq_answers
    faq_answers: list[str]
    intents: IntentModel
    lead_model: object | None          # sklearn classifier, None = heuristic until enough won/lost history
    stats: dict = field(default_factory=dict)


_cache: dict[int, tuple[float, Brain]] = {}


def path(tenant_id: int) -> Path:
    return Path(MODEL_DIR) / f"tenant_{tenant_id}.joblib"


def save(tenant_id: int, brain: Brain) -> None:
    p = path(tenant_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    joblib.dump(brain, tmp)
    tmp.replace(p)  # atomic: readers never see a half-written file
    _cache[tenant_id] = (p.stat().st_mtime, brain)


def load(tenant_id: int) -> Brain | None:
    p = path(tenant_id)
    if not p.exists():
        return None
    mtime = p.stat().st_mtime
    cached = _cache.get(tenant_id)
    if cached is None or cached[0] != mtime:
        _cache[tenant_id] = (mtime, joblib.load(p))
    return _cache[tenant_id][1]
