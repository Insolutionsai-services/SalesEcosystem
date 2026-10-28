"""Trial plan: self-created companies get 14 days and small lifetime caps; owner-created companies have none."""
import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from salescore.api import deps
from salescore.api.app import app
from salescore.core.models import Tenant
from salescore.core.utils import now


@pytest.fixture
def client(s, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "adm")
    app.dependency_overrides[deps.db] = lambda: s
    yield TestClient(app)
    app.dependency_overrides.clear()


CAMPAIGN = {"name": "Hello", "goal": "say hi", "writer": "own", "message": "Hi there, this is a test message"}


def test_trial_caps_then_read_only(client, s):
    r = client.post("/v1/auth/signup", json={"name": "Meera", "company": "Meera Interiors", "email": "meera@studio.in", "password": "meera-pass-1"})
    assert r.status_code == 200, r.text
    st = client.get("/v1/plan").json()
    assert st["trial"] and st["days_left"] == 14 and st["usage"]["members"] == 1 and st["limits"]["emails"] == 3
    assert client.post("/v1/campaigns", json=CAMPAIGN).status_code == 200
    assert client.post("/v1/campaigns", json=CAMPAIGN | {"name": "Two"}).status_code == 200
    r = client.post("/v1/campaigns", json=CAMPAIGN | {"name": "Three"})
    assert r.status_code == 402 and r.json()["detail"]["limit"] == "campaigns" and "demo_url" in r.json()["detail"]
    assert client.get("/v1/plan").json()["usage"]["campaigns"] == 2
    assert client.post("/v1/users", json={"email": "rep@studio.in", "name": "Rep", "password": "rep-pass-12", "role": "member"}).status_code == 200
    assert client.post("/v1/users", json={"email": "rep2@studio.in", "name": "Rep2", "password": "rep-pass-12", "role": "member"}).status_code == 402
    t = s.scalars(select(Tenant).where(Tenant.name == "Meera Interiors")).first()
    t.plan_started_at = now() - timedelta(days=15)
    s.commit()
    assert client.get("/v1/plan").json()["expired"]
    assert client.get("/v1/campaigns").status_code == 200  # still readable
    assert client.post("/v1/campaigns", json=CAMPAIGN).json()["detail"]["limit"] == "trial_over"


def test_owner_created_companies_have_no_caps(client):
    client.post("/v1/tenants", headers={"X-Admin-Key": "adm"},
                json={"name": "Gamyan", "owner_email": "me@gamyan.in", "owner_name": "C", "owner_password": "owner-pass-1"})
    client.post("/v1/auth/login", json={"email": "me@gamyan.in", "password": "owner-pass-1"})
    assert client.get("/v1/plan").json()["trial"] is False
    for i in range(3):
        assert client.post("/v1/campaigns", json=CAMPAIGN | {"name": f"c{i}"}).status_code == 200


def test_nothing_a_company_sees_names_the_models(client):
    """Plain English only: no model, provider or algorithm names in the UI, the agent roster or the settings labels."""
    leak = re.compile(r"local model|model call|gradient.boost|tf-idf|scikit|ollama|openrouter|gemini|qwen|gpt-|llama", re.IGNORECASE)
    root = Path(__file__).resolve().parents[1] / "salescore"
    for f in ("web/index.html", "analytics/team.py", "core/agents.py"):
        assert not leak.search((root / f).read_text(encoding="utf-8")), f
    client.post("/v1/tenants", headers={"X-Admin-Key": "adm"}, json={"name": "Acme", "owner_email": "a@acme.in", "owner_name": "A", "owner_password": "owner-pass-1"})
    client.post("/v1/auth/login", json={"email": "a@acme.in", "password": "owner-pass-1"})
    for path in ("/v1/me", "/v1/agents"):
        assert not leak.search(client.get(path).text), path


def test_email_cap_blocks_the_send_instead_of_crashing_the_sender(s, monkeypatch):
    """The 4th email of a trial company is logged as blocked and send() returns False; automated senders keep going."""
    from salescore.core.models import Activity, Contact
    from salescore.services import channels, otp, users

    for k in otp.SMTP_KEYS:
        monkeypatch.setenv(k, "x")
    sent = []
    monkeypatch.setattr(channels, "_email", lambda t, c, text, image=None: sent.append(c.email))
    owner = users.create_workspace(s, "Meera Interiors", "meera@studio.in", "Meera", users.hash_password("meera-pass-1"))
    tenant = s.get(Tenant, owner.tenant_id)
    c = Contact(tenant_id=tenant.id, name="Arun", email="arun@example.in", last_inbound_at=now())  # inside the reply window
    s.add(c)
    s.flush()
    results = [channels.send(s, tenant, c, f"hello {i}", "reply", via="human") for i in range(4)]
    assert results == [True, True, True, False] and len(sent) == 3
    blocked = s.scalars(select(Activity).where(Activity.tenant_id == tenant.id, Activity.type == "blocked")).all()
    assert len(blocked) == 1 and "emails" in blocked[0].data["reason"]


def test_signups_remember_the_outreach_link_they_came_from(client):
    client.post("/v1/auth/signup", json={"name": "Asha", "company": "Asha Lights", "email": "asha@lights.in", "password": "asha-pass-12", "source": "mail city=pune kind=smart"})
    rows = client.get("/v1/tenants", headers={"X-Admin-Key": "adm"}).json()
    asha = next(r for r in rows if r["name"] == "Asha Lights")
    assert asha["source"] == "mail city=pune kind=smart" and asha["plan"] == "trial" and asha["members"] == ["asha@lights.in"]
    assert client.get("/v1/tenants").status_code == 422  # no admin key, no list


def test_outreach_sends_and_replies_show_in_gyc(client):
    client.post("/v1/tenants", headers={"X-Admin-Key": "adm"}, json={"name": "Gamyan", "owner_email": "me@gamyan.in", "owner_name": "C", "owner_password": "owner-pass-1"})
    key = next(r for r in client.get("/v1/tenants", headers={"X-Admin-Key": "adm"}).json() if r["name"] == "Gamyan")["api_key"]
    h = {"X-API-Key": key}
    mail = {"email": "devi@shop.in", "name": "Devi Marketing", "subject": "Hello", "text": "body", "touch": 0, "kind": "hardware"}
    assert client.post("/v1/outreach", headers=h, json=mail).status_code == 200
    assert client.post("/v1/outreach", headers=h, json=mail | {"touch": 1}).status_code == 200
    assert client.post("/v1/outreach", headers=h, json={"email": "arun@sofa.in", "name": "Arun Sofas", "subject": "x", "text": "y", "kind": "furniture"}).status_code == 200
    assert client.post("/v1/outreach", headers=h, json={"email": "devi@shop.in", "direction": "in", "subject": "Re: Hello", "text": "Please call me tomorrow"}).status_code == 200
    assert client.post("/v1/outreach", headers=h, json={"email": "arun@sofa.in", "direction": "in", "subject": "Re: x", "text": "STOP"}).status_code == 200
    s = client.get("/v1/outreach", headers=h).json()
    assert s["companies_mailed"] == 2 and s["mails_sent"] == 3 and s["replied"] == ["Devi Marketing"] and s["stopped"] == ["Arun Sofas"]
    devi = next(c for c in s["companies"] if c["name"] == "Devi Marketing")
    assert devi["mails"] == 2 and devi["kind"] == "hardware" and devi["replied"] and not devi["stopped"]
    cs = client.get("/v1/contacts", headers=h).json()
    assert {c["source"] for c in cs} == {"outreach"} and any(c["opted_out"] for c in cs)
