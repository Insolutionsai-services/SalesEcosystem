"""Shared fixtures: an in-memory tenant with a multi-industry catalog, and a scripted fake LLM."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")  # before salescore imports: never touch a real DB

import json  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from salescore.ai import llm  # noqa: E402
from salescore.core.models import Tenant, init_db  # noqa: E402
from salescore.ml import store  # noqa: E402
from salescore.services.catalog import import_catalog  # noqa: E402

CATALOG = """sku,name,price,stock,description,category
PNT-WE-10,White Emulsion Paint 10L,2400,50,interior washable emulsion,paint
PVC-EL-2,PVC Elbow 2 inch,35,1000,plumbing fitting,pipes
PLOT-A12,Plot A-12 East Facing 1200 sqft,3600000,,corner plot near highway,real_estate
IOT-GATE-M,Smart Gate Access Kit (medium),185000,4,RFID + turnstile controller,iot
"""
PLAYBOOK = {"business": "Multi-line demo business", "stages": ["new", "qualified", "quoted", "won", "lost"],
            "pricing": {"max_discount_pct": 10, "auto_approve_discount_pct": 5, "tax_pct": 18},
            "compliance": {"quiet_hours": [0, 0], "max_proactive_per_week": 2}, "followup_days": [1, 3],
            "faq": [{"q": "what is the warranty", "a": "Exterior paints carry a 7-year warranty."},
                    {"q": "do you deliver, how long does delivery take", "a": "Delivery takes 2 days in the city."}]}


@pytest.fixture
def s():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    init_db(engine)
    with sessionmaker(engine, expire_on_commit=False)() as session:
        tenant = Tenant(name="Demo", playbook=PLAYBOOK)
        session.add(tenant)
        session.flush()
        import_catalog(session, tenant, CATALOG)
        session.commit()
        yield session


@pytest.fixture
def tenant(s):
    return s.scalars(select(Tenant)).one()


class FakeLLM:
    """Replays scripted turns in the OpenAI-compatible shape; each turn is [("text", str) | ("tool", name, args)]."""

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def __call__(self, messages, tools=None, lite=False):
        self.calls.append({"messages": list(messages), "tools": tools, "lite": lite})
        if not self.turns:
            raise AssertionError("LLM called but no scripted turn left (expected a local answer?)")
        turn = self.turns.pop(0)
        calls = [{"id": f"call{i}", "type": "function", "function": {"name": b[1], "arguments": json.dumps(b[2])}}
                 for i, b in enumerate(turn) if b[0] == "tool"]
        text = "".join(b[1] for b in turn if b[0] == "text") or None
        message = {"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})}
        return llm.Reply(text=text, tool_calls=calls, message=message, model="fake-model",
                         usage={"prompt_tokens": 1000, "completion_tokens": 50})


@pytest.fixture
def llm_stub(monkeypatch):
    """llm_stub(*turns) installs a FakeLLM as the chat function and returns it."""
    def install(*turns):
        fake = FakeLLM(*turns)
        monkeypatch.setattr(llm, "chat", fake)
        return fake
    return install


@pytest.fixture(autouse=True)
def isolated_models(tmp_path, monkeypatch):
    """Each test trains into its own folder; no brain leaks between tests."""
    monkeypatch.setattr(store, "MODEL_DIR", str(tmp_path / "models"))
    store._cache.clear()
    yield
    store._cache.clear()
