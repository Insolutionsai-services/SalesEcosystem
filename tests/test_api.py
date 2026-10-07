import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from salescore.api import deps
from salescore.api.app import app


@pytest.fixture
def client(s, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "adm")
    monkeypatch.setenv("WA_APP_SECRET", "sec")
    app.dependency_overrides[deps.db] = lambda: s
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_tenant_isolation_and_imports(client):
    assert client.post("/v1/tenants", json={"name": "x"}, headers={"X-Admin-Key": "wrong"}).status_code == 401
    keys = [client.post("/v1/tenants", json={"name": n}, headers={"X-Admin-Key": "adm"}).json()["api_key"]
            for n in ("Acme Paints", "Skyline Realty")]
    paints, realty = ({"X-API-Key": k} for k in keys)

    r = client.post("/v1/import/catalog", content="sku,name,price,finish\nWE10,White Emulsion 10L,2400,matt\n", headers=paints)
    assert r.json()["imported"] == 1 and r.json()["training"]["items"] == 1  # imports retrain immediately
    assert client.post("/v1/import/catalog", content="sku,name\nX,no price\n", headers=paints).status_code == 422
    assert client.post("/v1/import/unknown", content="", headers=paints).status_code == 404
    assert client.get("/v1/catalog/search", params={"q": "white paint"}, headers=paints).json()[0]["finish"] == "matt"
    assert client.get("/v1/catalog/search", params={"q": "white paint"}, headers=realty).json() == []  # isolated

    assert client.post("/v1/import/contacts", content="name,phone,consent\nRavi,9876500000,yes\n", headers=paints).json()["imported"] == 1
    assert client.get("/v1/insights", headers=paints).json()["contacts_by_stage"] == {"new": 1}
    assert client.get("/v1/tasks", headers={"X-API-Key": "nope"}).status_code == 401


def test_whatsapp_signature(client):
    assert client.post("/webhooks/whatsapp", content=b"{}", headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 401
    body = b'{"entry": []}'
    sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig}).status_code == 200


def test_channels_ready_when_details_are_filled(client, monkeypatch):
    from salescore.services import channels
    key = client.post("/v1/tenants", json={"name": "Acme Paints"}, headers={"X-Admin-Key": "adm"}).json()["api_key"]
    h = {"X-API-Key": key}
    for k in channels.SMTP_KEYS:
        monkeypatch.delenv(k, raising=False)
    st = client.get("/v1/channels", headers=h).json()
    assert st["channel"] == "auto" and not st["whatsapp"]["ready"] and not st["email"]["ready"]
    assert client.post("/v1/channels/test", json={"to": "9840011001"}, headers=h).status_code == 422  # not connected yet

    st = client.put("/v1/channels/whatsapp", json={"phone_id": "1234", "token": "tok"}, headers=h).json()
    assert st["whatsapp"] == {**st["whatsapp"], "ready": True, "phone_id": "1234", "has_token": True}
    sent = []
    monkeypatch.setattr(channels, "_whatsapp", lambda t, c, text, image=None: sent.append(c.phone))
    assert client.post("/v1/channels/test", json={"to": "98400 11001"}, headers=h).json() == {"sent": "whatsapp"}
    assert sent == ["919840011001"]
    assert not client.put("/v1/channels/whatsapp", json={"phone_id": ""}, headers=h).json()["whatsapp"]["has_token"]


def test_auto_channel_prefers_whatsapp_then_email(monkeypatch):
    from types import SimpleNamespace

    from salescore.services import channels
    for k in channels.SMTP_KEYS:
        monkeypatch.setenv(k, "x")
    t = SimpleNamespace(playbook={}, wa_phone_id=None, wa_token=None)
    both, mail_only = SimpleNamespace(phone="91", email="a@b.in"), SimpleNamespace(phone=None, email="a@b.in")
    assert channels.pick(t, both) == "email"            # WhatsApp not connected yet
    t.wa_phone_id, t.wa_token = "1", "tok"
    assert channels.pick(t, both) == "whatsapp" and channels.pick(t, mail_only) == "email"
    assert channels.pick(t, both, "console") == "console"
