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
