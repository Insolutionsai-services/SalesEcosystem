"""The real-estate demo company: plots by venture, who bought what, brokers, and that other companies never see any of it."""
import pytest

from salescore.dev_estates import ESTATES_USER, seed_estates
from salescore.services import realestate
from salescore.services.users import check_login


@pytest.fixture
def estates(s):
    return seed_estates(s)


def test_plots_by_venture_with_buyers(s, estates):
    ventures = {v["name"]: v for v in realestate.plots(s, estates)["ventures"]}
    assert list(ventures) == ["Sunrise Meadows", "Lakeview Heights"]
    assert all(len(v["plots"]) == 25 and v["available"] + v["booked"] + v["sold"] == 25 for v in ventures.values())
    sold = next(p for p in ventures["Sunrise Meadows"]["plots"] if p["status"] == "sold")
    assert sold["buyer"]["price"] == sold["price"] and sold["buyer"]["source"] in ("Our lead", "Broker")


def test_clients_are_brokers_with_their_leads(s, estates):
    data = realestate.clients(s, estates)
    assert [c["name"] for c in data["clients"]][0] == "Ramesh Reddy" and len(data["clients"]) == 4
    ramesh = data["clients"][0]
    assert ramesh["buyers"] and ramesh["score"] > 0 and sum(x["value"] for x in ramesh["list"]) == ramesh["value"]
    assert ramesh["commission"] == round(ramesh["paid"] * ramesh["commission_pct"] / 100, 2)
    assert data["own"]["leads"] == 8 and data["leads"] == 24  # our own leads are not a client
    assert data["via_clients"] + data["own"]["value"] == data["sold_value"]


def test_sign_in_is_in_the_database_and_only_estates_has_the_pages(s, tenant, estates):  # tenant first: its fixture expects one company
    email, _, _, password = ESTATES_USER
    assert check_login(s, email, password).tenant_id == estates.id
    assert realestate.enabled(estates) and not realestate.enabled(tenant)
    assert seed_estates(s).id == estates.id  # running twice adds nothing


def test_messages_become_wants_and_matches(s, tenant, estates):
    from salescore.services import estate_ai
    words = {"lakeview": "Lakeview Heights", "sunrise": "Sunrise Meadows"}
    w = estate_ai.parse_wants("need a 200 yard east facing plot under 38 lakhs in Lakeview", words)
    assert w == {"budget": 3_800_000, "size": 200, "facing": "east", "venture": "Lakeview Heights"}
    assert estate_ai.parse_wants("budget 1.2 crore", words) == {"budget": 12_000_000}
    plot = {"price": 3_800_000, "size": 200, "facing": "North-East", "venture": "Lakeview Heights"}
    assert estate_ai.match_score(plot, w) == 100
    assert estate_ai.match_score({**plot, "price": 4_600_000}, w) is None  # more than 15% over budget
    assert estate_ai.match_score({**plot, "venture": "Sunrise Meadows"}, w) is None  # named venture must match
    assert estate_ai.inr(6046200) == "₹60,46,200"


def test_alerts_and_exact_answers(s, tenant, estates):
    from salescore.core.utils import now
    from salescore.services import estate_ai
    found = {a["key"]: a for a in estate_ai.alerts(s, estates, now())}
    assert found["double"]["severity"] == "critical" and found["double"]["count"] == 2
    assert found["discount"]["count"] == 1 and found["dues"]["count"] >= 1 and all(a["formula"] for a in found.values())
    assert "Ramesh Reddy" in estate_ai.quick_answer(s, estates, "Which broker has the most pending dues?")
    assert estate_ai.quick_answer(s, estates, "tell me a joke") is None
    assert "Available plots" in estate_ai.quick_answer(s, estates, "how many plots are available in lakeview?")


def test_add_and_delete_client(s, tenant):
    from fastapi.testclient import TestClient

    from salescore.api import deps
    from salescore.api.app import app
    from salescore.core.models import Contact

    tenant.playbook = {**tenant.playbook, "industry": "realestate"}
    s.add(Contact(tenant_id=tenant.id, name="Ravi", phone="919840000001", attrs={"client": "Sri Realty"}))
    s.commit()
    app.dependency_overrides[deps.db] = lambda: s
    try:
        web, h, url = TestClient(app), {"X-API-Key": tenant.api_key}, "/v1/realestate/clients"
        new = web.post(url, json={"name": " Sri Realty ", "commission_pct": 2}, headers=h).json()
        assert web.post(url, json={"name": "sri realty"}, headers=h).status_code == 422  # same broker twice
        assert web.post(url, json={"name": "X", "commission_pct": 101}, headers=h).status_code == 422
        assert web.get(url, headers=h).json()["clients"][0]["leads"] == 1
        assert web.delete(f"{url}/{new['id']}", headers=h).status_code == 200
        after = web.get(url, headers=h).json()
        assert after["clients"] == [] and after["own"]["leads"] == 1  # the lead is kept
    finally:
        app.dependency_overrides.clear()


def test_buy_advice_rule():
    from salescore.services.estate_ai import advice

    assert advice(available=4, waiting=5, recent=0, before=0) == (None, "Buy more")      # more waiting than left
    assert advice(available=4, waiting=0, recent=2, before=2) == (2.0, "Buy more")       # 2 sales a month: 2 months left
    assert advice(available=10, waiting=0, recent=2, before=2) == (5.0, "Enough stock")
    assert advice(available=10, waiting=0, recent=0, before=0) == (None, "Hold buying")


def test_suggestion_figures_match_the_data(s, tenant, estates):
    import statistics

    from sqlalchemy import select

    from salescore.core.models import Item, Quote
    from salescore.core.utils import now
    from salescore.services import estate_ai

    data = estate_ai.suggestions(s, estates, now())
    won = s.scalars(select(Quote).where(Quote.tenant_id == estates.id, Quote.status == "won")).all()
    owed = round(sum(q.total - (q.paid_amount or 0) for q in won if q.total - (q.paid_amount or 0) > 1), 2)
    pay = data["payments"]
    assert pay["total"] == owed == round(sum(a["amount"] for a in pay["ages"]), 2)

    items = {i.sku: i for i in s.scalars(select(Item).where(Item.tenant_id == estates.id))}
    for row in data["pricing"]:
        rates = [ln["amount"] / float(items[ln["sku"]].attrs["size_sqyd"]) for q in won for ln in q.lines
                 if items[ln["sku"]].attrs["venture"] == row["venture"]]
        assert row["sales"] == len(rates) and row["low"] == round(min(rates)) and row["median"] == round(statistics.median(rates))
        assert row["history_min"] == (row["median"] if row["high_demand"] else row["low"])
    assert sum(r["sold"] for r in data["market"]["venture"]) == len({ln["sku"] for q in won for ln in q.lines})
    assert data["engage"] and all(a["quiet_days"] >= b["quiet_days"] for a, b in zip(data["engage"], data["engage"][1:]))
