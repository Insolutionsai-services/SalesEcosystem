"""Sign-in: company users, roles, sessions, and that one company never sees another's data."""
from datetime import timedelta

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


@pytest.fixture
def mail(monkeypatch):
    """Email set up, with sent codes captured instead of mailed."""
    from salescore.services import otp
    for k in otp.SMTP_KEYS:
        monkeypatch.setenv(k, "x")
    sent = {}
    monkeypatch.setattr(otp, "_mail", lambda to, code: sent.__setitem__(to, code))
    monkeypatch.setattr(otp, "COOLDOWN_S", 0)
    return sent


def test_sign_in_needs_no_code_even_with_email_set_up(client, mail):
    company(client, "Acme Paints", "boss@acme.in")
    assert login(client, "boss@acme.in", "owner-pass-1").json()["email"] == "boss@acme.in" and "boss@acme.in" not in mail
    assert client.get("/v1/me").json()["user"]["email"] == "boss@acme.in"


def test_code_attempts_are_limited(client, mail):
    client.post("/v1/auth/signup", json={"name": "Meera", "company": "Meera Interiors", "email": "meera@studio.in", "password": "meera-pass-1"})
    for _ in range(5):
        client.post("/v1/auth/verify", json={"email": "meera@studio.in", "code": "000000"})
    r = client.post("/v1/auth/verify", json={"email": "meera@studio.in", "code": mail["meera@studio.in"]})
    assert r.status_code == 422 and "too many" in r.json()["detail"]  # even the right code no longer works


def test_sign_up_creates_a_workspace_after_the_code(client, mail):
    r = client.post("/v1/auth/signup", json={"name": "Meera", "company": "Meera Interiors", "email": "Meera@Studio.in", "password": "meera-pass-1"})
    assert r.json()["otp"] and client.post("/v1/auth/login", json={"email": "meera@studio.in", "password": "meera-pass-1"}).status_code == 422  # not created yet
    assert client.post("/v1/auth/verify", json={"email": "meera@studio.in", "code": mail["meera@studio.in"]}).json()["role"] == "admin"
    assert client.get("/v1/me").json()["name"] == "Meera Interiors"
    assert client.post("/v1/auth/signup", json={"email": "meera@studio.in", "password": "meera-pass-1"}).status_code == 422  # taken
    assert client.post("/v1/auth/signup", json={"email": "new@studio.in", "password": "short"}).status_code == 422


def test_without_email_set_up_sign_in_is_plain_and_sign_up_is_direct(client, monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    company(client, "Acme Paints", "boss@acme.in")
    assert login(client, "boss@acme.in", "owner-pass-1").json()["email"] == "boss@acme.in"
    client.post("/v1/auth/logout")
    r = client.post("/v1/auth/signup", json={"name": "Meera", "company": "Meera Interiors", "email": "meera@studio.in", "password": "meera-pass-1"})
    assert r.status_code == 200 and r.json()["role"] == "admin" and client.get("/v1/me").json()["name"] == "Meera Interiors"
    assert [u["email"] for u in client.get("/v1/users").json()] == ["meera@studio.in"]  # her own empty company, never Acme's
    assert client.post("/v1/auth/signup", json={"email": "sales@demo.in", "password": "x" * 8}).status_code == 422  # the demo login can't be claimed
    assert client.post("/v1/auth/signup", json={"email": "x@demo.in", "password": "x" * 8}).status_code == 422
    assert client.get("/v1/auth/options").json() == {"google": False, "email_codes": False}


def test_demo_account_skips_the_code(client, mail):
    company(client, "Demo Paints", "sales@demo.in")
    assert login(client, "sales@demo.in", "owner-pass-1").json()["email"] == "sales@demo.in" and "sales@demo.in" not in mail


def test_forgot_password_emails_a_new_one(client, mail, monkeypatch):
    from salescore.services import otp
    sent = {}
    monkeypatch.setattr(otp, "mail", lambda to, subject, text: sent.__setitem__(to, text))
    company(client, "Acme Paints", "boss@acme.in")
    r = client.post("/v1/auth/forgot", json={"email": "nobody@acme.in"})
    assert r.status_code == 422 and "create your account" in r.json()["detail"]
    assert client.post("/v1/auth/forgot", json={"email": "boss@acme.in"}).json() == {"sent": "boss@acme.in"}
    new = sent["boss@acme.in"].split("password is: ")[1].split()[0]
    assert login(client, "boss@acme.in", "owner-pass-1").status_code != 200  # old one no longer works
    assert login(client, "boss@acme.in", new).status_code == 200


def test_session_idles_out_and_has_a_hard_limit(client, s, monkeypatch):
    company(client, "Acme Paints", "boss@acme.in")
    r = login(client, "boss@acme.in", "owner-pass-1")
    assert "max-age" not in r.headers["set-cookie"].lower()            # browser drops it on close
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    t0 = users.now()
    clock = lambda dt: monkeypatch.setattr(users, "now", lambda: t0 + dt)  # noqa: E731
    for h in range(1, 15):                                               # active every 50 min: stays signed in (to 11h40)...
        clock(timedelta(minutes=50 * h))
        assert client.get("/v1/me").status_code == 200, h
    clock(timedelta(hours=12, minutes=1))                                # ...but never past 12 hours, though last seen 21 min ago
    assert client.get("/v1/me").status_code == 401
    login(client, "boss@acme.in", "owner-pass-1")
    clock(timedelta(hours=12, minutes=1) + users.SESSION_IDLE + timedelta(minutes=1))  # idle over an hour
    assert client.get("/v1/me").status_code == 401


def test_cross_site_writes_are_blocked(client):
    company(client, "Acme Paints", "boss@acme.in")
    evil = client.post("/v1/auth/login", json={"email": "boss@acme.in", "password": "owner-pass-1"}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert client.post("/v1/auth/login", json={"email": "boss@acme.in", "password": "owner-pass-1"}, headers={"Origin": "http://testserver"}).status_code == 200


def test_gemini_key_is_lead_only_checked_and_never_shown(client, monkeypatch):
    from salescore.ai import llm
    company(client, "Acme Paints", "boss@acme.in")
    login(client, "boss@acme.in", "owner-pass-1")
    def pick(key):
        if key != "AIza-good-key-1234":
            raise ValueError("Google didn't accept this key")
        return "gemini-2.5-flash-lite"
    monkeypatch.setattr(llm, "pick_gemini_model", pick)
    assert client.put("/v1/ai/key", json={"key": "nope"}).status_code == 422
    r = client.put("/v1/ai/key", json={"key": "AIza-good-key-1234"}).json()
    assert r == {"has_key": True, "hint": "••••1234"}
    assert "AIza-good" not in client.get("/v1/ai").text                # only the last 4 ever leave the server
    client.post("/v1/users", json={"name": "Ravi", "email": "ravi@acme.in", "password": "ravi-pass-1"})
    client.post("/v1/auth/logout")
    login(client, "ravi@acme.in", "ravi-pass-1")
    assert client.put("/v1/ai/key", json={"key": ""}).status_code == 403   # sales reps can't change it
    assert client.get("/v1/ai").json()["has_key"]
