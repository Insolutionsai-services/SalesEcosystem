"""Owner console: only OWNER_EMAILS (or the admin key) get in; revenue, costs, plan requests and plan changes add up."""
from salescore.core import config
from salescore.services import pricing
from tests.test_auth import client, company, login  # noqa: F401  (fixture)


def test_owner_console(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "OWNER_EMAILS", {"boss@gamyan.in"})
    company(client, "Gamyan", "boss@gamyan.in")
    company(client, "Acme Paints", "boss@acme.in")

    login(client, "boss@acme.in", "owner-pass-1")                       # a customer's sales lead: no way in
    assert client.get("/v1/owner/overview").status_code == 403
    assert client.get("/v1/owner/users").status_code == 403
    assert "owner" not in client.get("/v1/me").json()                  # the customer app knows nothing about the admin side
    client.post("/v1/plan/request", json={"plan": "growth"})
    client.post("/v1/auth/logout")

    login(client, "boss@gamyan.in", "owner-pass-1")
    assert client.get("/v1/owner/me").json()["email"] == "boss@gamyan.in"
    acme = next(c for c in client.get("/v1/owner/customers").json() if c["name"] == "Acme Paints")
    assert acme["status"] == "unpriced" and acme["revenue"] == 0       # created by the admin key: arranged by hand, no price yet

    reqs = client.get("/v1/owner/requests").json()
    growth = next(r for r in reqs if r["customer"] == "Acme Paints")
    assert growth["status"] == "pending" and growth["monthly"] == 15000
    assert client.post(f"/v1/owner/requests/{growth['id']}/approve").json()["status"] == "approved"
    assert client.post(f"/v1/owner/requests/{growth['id']}/approve").status_code == 422   # decided once
    acme = client.get(f"/v1/owner/customers/{acme['id']}").json()
    assert acme["plan"] == "growth" and acme["revenue"] == 15000 and acme["status"] == "paying"
    assert acme["people"][0]["email"] == "boss@acme.in"

    ov = client.get("/v1/owner/overview").json()
    assert ov["money"]["revenue"] == 15000 and ov["customers"]["total"] >= 2
    assert ov["money"]["profit"] == 15000 - ov["money"]["cost"] and len(ov["signups"]) == 12
    assert ov["by_plan"]["growth"]["customers"] == 1

    r = client.put(f"/v1/owner/customers/{acme['id']}/plan", json={"plan": "custom"})
    assert r.status_code == 422                                          # a custom plan needs its agreed price
    r = client.put(f"/v1/owner/customers/{acme['id']}/plan", json={"plan": "custom", "price": 8000}).json()
    assert r["plan"] == "custom" and r["revenue"] == 8000

    assert client.get("/v1/owner/pricing?companies=25").json()["plans"]["starter"]["price"] == pricing.PRESETS["starter"]["price"]
    sysinfo = client.get("/v1/owner/system").json()
    assert sysinfo["version"] and sysinfo["database"]["rows"]["tenants"] >= 2
    client.post("/v1/auth/logout")
    assert client.get("/v1/owner/overview", headers={"X-Admin-Key": "adm"}).status_code == 200


def test_admin_page_is_separate_and_manages_every_login(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "OWNER_EMAILS", {"boss@gamyan.in"})
    page = client.get("/admin")
    assert page.status_code == 200 and "GYC Admin" in page.text and page.headers["x-robots-tag"].startswith("noindex")
    customer_page = client.get("/").text
    assert "/v1/owner" not in customer_page and "Owner console" not in customer_page   # nothing of the admin ships to customers

    company(client, "Gamyan", "boss@gamyan.in")
    company(client, "Acme Paints", "boss@acme.in")
    login(client, "boss@acme.in", "owner-pass-1")
    client.post("/v1/users", json={"name": "Ravi", "email": "ravi@acme.in", "password": "ravi-pass-1"})
    client.post("/v1/auth/logout")

    login(client, "boss@gamyan.in", "owner-pass-1")
    users = {u["email"]: u for u in client.get("/v1/owner/users").json()}
    assert users["ravi@acme.in"]["company"] == "Acme Paints" and users["boss@gamyan.in"]["owner"]
    ravi, boss = users["ravi@acme.in"]["id"], users["boss@acme.in"]["id"]
    assert client.put(f"/v1/owner/users/{users['boss@gamyan.in']['id']}", json={"disabled": True}).status_code == 422  # never lock yourself out
    assert client.delete(f"/v1/owner/users/{boss}").status_code == 422      # Acme's only sales lead stays
    assert client.put(f"/v1/owner/users/{ravi}", json={"disabled": True}).status_code == 200
    client.post("/v1/auth/logout")
    assert login(client, "ravi@acme.in", "ravi-pass-1").status_code == 422  # switched off: can't sign in

    login(client, "boss@gamyan.in", "owner-pass-1")
    client.put(f"/v1/owner/users/{ravi}", json={"disabled": False, "password": "fresh-pass-22"})
    client.post("/v1/auth/logout")
    assert login(client, "ravi@acme.in", "fresh-pass-22").status_code == 200
    client.post("/v1/auth/logout")
    login(client, "boss@gamyan.in", "owner-pass-1")
    assert client.delete(f"/v1/owner/users/{ravi}").status_code == 200
    assert "ravi@acme.in" not in {u["email"] for u in client.get("/v1/owner/users").json()}


def test_admin_username_password_sign_in(client, monkeypatch):  # noqa: F811
    from salescore.services import admin_auth, users
    monkeypatch.setattr(admin_auth, "_fails", {})
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    assert client.post("/v1/admin-auth/login", json={"username": "admin", "password": "Anything-1"}).status_code == 422  # not set up yet
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", users.hash_password("Str0ng-Pass!x"))
    assert admin_auth.strong_enough("weakpass") and admin_auth.strong_enough("Str0ng-Pass!x") is None

    assert client.get("/v1/owner/overview").status_code == 401                        # signed out
    assert client.post("/v1/admin-auth/login", json={"username": "admin", "password": "wrong-Pass1!"}).status_code == 422
    assert client.post("/v1/admin-auth/login", json={"username": "root", "password": "Str0ng-Pass!x"}).status_code == 422
    r = client.post("/v1/admin-auth/login", json={"username": "Admin ", "password": "Str0ng-Pass!x"})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert client.get("/v1/owner/me").json()["email"] == "admin"
    assert client.get("/v1/owner/overview").status_code == 200
    assert client.get("/v1/me").status_code == 401                                     # the admin session opens nothing in the customer app

    monkeypatch.setenv("ADMIN_PASSWORD_HASH", users.hash_password("An0ther-Pass!y"))   # a new password signs every admin browser out
    assert client.get("/v1/owner/overview").status_code == 401
    client.post("/v1/admin-auth/login", json={"username": "admin", "password": "An0ther-Pass!y"})
    client.post("/v1/admin-auth/logout")
    assert client.get("/v1/owner/overview").status_code == 401

    for _ in range(5):
        client.post("/v1/admin-auth/login", json={"username": "admin", "password": "bad-Guess-1!"})
    assert "too many" in client.post("/v1/admin-auth/login", json={"username": "admin", "password": "An0ther-Pass!y"}).json()["detail"]


def test_admin_sign_in_replaces_the_old_key(client, monkeypatch):  # noqa: F811
    from salescore.services import admin_auth, users
    monkeypatch.setattr(admin_auth, "_fails", {})
    monkeypatch.delenv("ADMIN_KEY", raising=False)                                    # no key set: the back door is gone
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", users.hash_password("Str0ng-Pass!x"))
    assert client.get("/v1/tenants", headers={"X-Admin-Key": "dev-admin"}).status_code == 401
    assert client.get("/v1/owner/overview", headers={"X-Admin-Key": "dev-admin"}).status_code == 403
    client.post("/v1/admin-auth/login", json={"username": "admin", "password": "Str0ng-Pass!x"})
    assert client.get("/v1/tenants").status_code == 200                               # creating and listing companies works from /admin
    assert client.post("/v1/tenants", json={"name": "New Co"}).status_code == 200
