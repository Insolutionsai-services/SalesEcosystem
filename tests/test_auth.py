"""Sign-in: company users, roles, sessions, and that one company never sees another's data."""
import pytest
from fastapi.testclient import TestClient

from salescore.api import deps
from salescore.api.app import app
from salescore.services import users


@pytest.fixture
def client(s, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "adm")
    monkeypatch.setattr(users, "_fails", {})
    app.dependency_overrides[deps.db] = lambda: s
    yield TestClient(app)
    app.dependency_overrides.clear()


def company(client, name, email):
    r = client.post("/v1/tenants", headers={"X-Admin-Key": "adm"},
                    json={"name": name, "owner_email": email, "owner_name": "Owner", "owner_password": "owner-pass-1"})
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


def login(client, email, password):
    return client.post("/v1/auth/login", json={"email": email, "password": password})


def test_sign_in_roles_and_isolation(client):
    company(client, "Acme Paints", "boss@acme.in")
    company(client, "Skyline Realty", "boss@skyline.in")
    assert client.get("/v1/me").status_code == 401                       # signed out
    assert login(client, "boss@acme.in", "wrong-pass").status_code == 422
    assert login(client, "nobody@acme.in", "wrong-pass").json()["detail"] == "wrong email or password"  # no hint which

    r = login(client, " Boss@Acme.in ", "owner-pass-1")
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    me = client.get("/v1/me").json()
    assert me["name"] == "Acme Paints" and me["user"]["role"] == "admin"

    assert client.post("/v1/users", json={"name": "Ravi", "email": "ravi@acme.in", "password": "short"}).status_code == 422
    assert client.post("/v1/users", json={"name": "Ravi", "email": "ravi@acme.in", "password": "ravi-pass-1"}).status_code == 200
    assert client.post("/v1/users", json={"email": "boss@skyline.in", "password": "x" * 8}).status_code == 422  # taken
    client.post("/v1/auth/logout")
    assert client.get("/v1/me").status_code == 401

    login(client, "ravi@acme.in", "ravi-pass-1")                        # sales member: daily work yes, limits no
    assert client.get("/v1/tasks").status_code == 200
    assert client.put("/v1/agents/approval", json={"values": {"pricing.auto_approve_discount_pct": 50}}).status_code == 403
    assert client.post("/v1/users", json={"email": "x@acme.in", "password": "x" * 8}).status_code == 403
    assert [u["email"] for u in client.get("/v1/users").json()] == ["boss@acme.in", "ravi@acme.in"]  # never Skyline's


def test_last_admin_stays_and_lockout(client, s):
    company(client, "Acme Paints", "boss@acme.in")
    login(client, "boss@acme.in", "owner-pass-1")
    me = client.get("/v1/me").json()["user"]
    assert client.put(f"/v1/users/{me['id']}", json={"role": "member"}).status_code == 422
    assert client.delete(f"/v1/users/{me['id']}").status_code == 422
    client.post("/v1/auth/logout")
    for _ in range(users.MAX_FAILS):
        login(client, "boss@acme.in", "guess-guess")
    assert "too many attempts" in login(client, "boss@acme.in", "owner-pass-1").json()["detail"]


def test_api_key_still_works_for_integrations(client):
    key = company(client, "Acme Paints", "boss@acme.in")
    assert client.get("/v1/me", headers={"X-API-Key": key}).json()["user"] is None
    assert client.get("/v1/me", headers={"X-API-Key": "nope"}).status_code == 401
