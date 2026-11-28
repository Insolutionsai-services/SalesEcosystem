"""Plans and prices: Starter and Growth at a fixed price, Custom priced per unit from provider costs, and add-on packs."""
import pytest

from salescore.services import pricing
from tests.test_auth import client, company, login  # noqa: F401  (fixture)


def test_every_price_keeps_a_profit():
    sheet = pricing.cost_sheet()
    for k, u in sheet["units"].items():
        if k not in pricing.AT_COST:
            assert u["price"] > u["cost"] * 1.15, k            # never sell a unit near or below what the providers charge us
    for k, p in sheet["plans"].items():
        assert p["profit"] > 0, k                          # even with one customer paying the whole server, fully used
    for k, p in sheet["yearly"].items():
        assert p["profit"] > 0, k                          # yearly: 11 months paid must still cover 12 months of costs
    for k, p in pricing.cost_sheet(companies=25)["plans"].items():
        assert p["margin_pct"] >= 50, k                    # and a healthy margin once ~25 customers share it
    assert sheet["units"]["call_minutes"]["by_provider"]["Exotel"] > 0


def test_ready_plans_are_fixed_and_cheaper_than_building_them():
    for p in pricing.catalog()["presets"]:
        if p["price"]:
            assert p["custom_price"] > p["price"]
    q = pricing.quote({"plan": "growth", "cycle": "year"})
    assert q["monthly"] == 15000 and q["recurring"] == 180000 and q["saving"] == 0   # no free month
    starter = pricing.quote({"plan": "starter"})
    assert starter["monthly"] == 5000 and starter["campaigns"] == 5 and "distribution" in starter["agents"]
    assert pricing.quote({"plan": "growth"})["campaigns"] == 15


def test_custom_is_priced_per_unit():
    q = pricing.quote({"plan": "custom", "agents": ["response", "voice"],
                       "usage": {"ai": 2000, "call_minutes": 500, "members": 5, "wa_campaign": 3000}})
    assert q["usage"]["wa_campaign"] == 0                  # no Distribution agent: no campaign messages
    assert q["monthly"] == pricing.PLATFORM_FEE + 2 * pricing.EXTRA_LOGIN + round(2000 * pricing.PRICE["ai"]) + round(500 * pricing.PRICE["call_minutes"])
    with pytest.raises(ValueError):
        pricing.quote({"plan": "custom", "agents": []})
    with pytest.raises(ValueError):
        pricing.quote({"plan": "custom", "agents": ["response"], "usage": {"ai": 1234}})


def test_addons():
    with pytest.raises(ValueError, match="Voice"):
        pricing.quote({"plan": "starter", "addons": {"call_minutes": {"pack": 0, "qty": 1}}})
    q = pricing.quote({"plan": "starter", "addons": {"ai": {"pack": 1, "qty": 2}}})
    assert q["once"] == round(10000 * pricing.PRICE["ai"]) and q["due_now"] == 5000 + q["once"]


def test_plan_page_and_request(client):  # noqa: F811
    company(client, "Acme Paints", "boss@acme.in")
    login(client, "boss@acme.in", "owner-pass-1")
    page = client.get("/v1/plans").json()
    assert [p["key"] for p in page["presets"]] == ["starter", "growth", "custom"] and page["current"]["arranged"]
    assert "cost" not in str(page["usage"])                 # customers see prices, never our costs
    r = client.post("/v1/plan/request", json={"plan": "growth", "cycle": "year", "addons": {"ai": {"pack": 0, "qty": 1}}})
    assert r.status_code == 200 and r.json()["due_now"] == 180000 + round(1000 * pricing.PRICE["ai"])
    assert client.get("/v1/plans").json()["request"]["label"] == "Growth"
    acme = next(x for x in client.get("/v1/tenants", headers={"X-Admin-Key": "adm"}).json() if x["name"] == "Acme Paints")
    assert acme["plan_request"]["plan"] == "growth"
    assert client.get("/v1/pricing/costs", headers={"X-Admin-Key": "adm"}).json()["plans"]["starter"]["price"] == 5000
    assert client.get("/v1/pricing/costs").status_code == 401   # owner only
    client.post("/v1/users", json={"name": "Ravi", "email": "ravi@acme.in", "password": "ravi-pass-1"})
    client.post("/v1/auth/logout")
    login(client, "ravi@acme.in", "ravi-pass-1")
    assert client.post("/v1/plan/request", json={"plan": "starter"}).status_code == 403
