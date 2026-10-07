"""The marketing chain from the plan: Data -> Strategy -> Creative -> Verification -> Approval -> Distribution
-> Response -> Analytics -> back to Strategy."""
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from salescore.analytics.team import roster
from salescore.core.models import Activity, Campaign, Contact, Quote, Task
from salescore.core.utils import now
from salescore.marketing import (
    attribution,
    distribution,
    orchestrator,
    strategy,
    verification,
)
from salescore.services import contacts
from salescore.workflows import approvals, inbound


def creative_json(a: str, b: str) -> list:
    return [("text", json.dumps({"variants": [{"label": "A", "angle": "benefit", "text": a},
                                              {"label": "B", "angle": "offer", "text": b}],
                                 "image_brief": "Freshly painted living room, warm light"}))]


@pytest.fixture
def customers(s, tenant):
    """40 consented lapsing customers + 10 without consent, all in the win-back segment."""
    rows = "name,phone,consent\n" + "\n".join(f"Cust {i},90001000{i:02d},{'yes' if i < 40 else 'no'}" for i in range(50))
    people = contacts.import_contacts(s, tenant, rows)
    for c in people:
        c.segment = "at_risk"
    s.flush()
    return people


def test_strategy_recommends_data_driven_plays(s, tenant, customers):
    plays = {p.key: p for p in strategy.recommend(s, tenant)}
    win_back = plays["win_back"]
    assert win_back.audience == 50 and win_back.reachable == 40  # only consented contacts count
    assert "5% comeback discount" in win_back.offer            # offer respects the auto-approve discount limit
    assert win_back.expected_replies == pytest.approx(40 * strategy.PRIOR_REPLY_RATE, abs=0.1)
    assert win_back.send_at > now() and win_back.why
    assert "clear_overstock" in plays                           # stock with no recent sales -> sell-down play


def test_verification_catches_wrong_facts(s, tenant):
    ok = verification.verify(s, tenant, "Hi {name}, White Emulsion 10L now INR 2,400 (INR 2,832 incl 18% tax).")
    assert ok["ok"], ok
    discounted = verification.verify(s, tenant, "Hi {name}, 5% off: White Emulsion 10L for INR 2,280 - save INR 120!")
    assert discounted["ok"], discounted
    bad = verification.verify(s, tenant, "Hi {name}! Emulsion only ₹1,999, 20% off, guaranteed returns {code}")
    text = " | ".join(bad["errors"])
    assert not bad["ok"] and "1,999" in text and "20%" in text and "guaranteed returns" in text and "code" in text
    tenant.playbook = {**tenant.playbook, "compliance": {"required_phrases": ["RERA TN/29/1234"]}}
    assert "required text missing" in " ".join(verification.verify(s, tenant, "Hi {name}, plots available")["errors"])


def test_full_chain_with_feedback_loop(s, tenant, customers, llm_stub):
    llm_stub(creative_json("Hi {name}, we miss you! White Emulsion 10L is back in stock at INR 2,400.",
                           "Hi {name}, come back and save 5% on your usual order this week."))
    # Strategy -> Creative -> Verification -> approval task
    campaign = orchestrator.launch_play(s, tenant, "win_back")
    assert campaign.status == "pending_approval" and campaign.checks["ok"] and len(campaign.variants) == 2
    assert campaign.image_brief and campaign.plan["why"] and campaign.send_at > now()
    assert not s.scalars(select(Activity).where(Activity.type == "msg_out")).all()  # nothing sent yet

    # Human approval -> scheduled for the strategy's best time -> Distribution when due
    task = s.scalars(select(Task).where(Task.kind == "approval")).one()
    assert approvals.decide(s, tenant, task, approve=True)["status"] == "scheduled"
    sent = distribution.dispatch_due(s, tenant, campaign.send_at + timedelta(minutes=1))[0]
    assert sent["status"] == "sent" and sent["sent"] == 36 and len(sent["holdout_ids"]) == 5  # 10% holdout of 50
    msgs = s.scalars(select(Activity).where(Activity.type == "msg_out")).all()
    assert {m.data["variant"] for m in msgs} == {"A", "B"} and all(m.data["campaign_id"] == campaign.id for m in msgs)
    assert not {m.contact_id for m in msgs} & set(sent["holdout_ids"])

    # Response: some customers reply and one buys (quote won) inside the attribution window
    campaign.sent_at = now() - timedelta(hours=1)
    repliers = [s.get(Contact, m.contact_id) for m in msgs[:9]]
    llm_stub(*[[("text", "Great, happy to help!")]] * 9)
    for c in repliers:
        inbound.handle_inbound(s, tenant, "yes, tell me more about this offer", phone=c.phone)
    s.add(Quote(tenant_id=tenant.id, contact_id=repliers[0].id, lines=[], subtotal=5000, total=5900, status="won"))
    s.flush()

    # Analytics measures it; Strategy learns from it and puts the play on cooldown
    r = attribution.measure(s, tenant, campaign)
    assert r["sent"] == 36 and r["replied"] == 9 and r["won"] == 1 and r["revenue"] == 5900
    assert r["reply_rate"] == 0.25 and r["holdout"]["reply_rate"] == 0 and r["lift_reply_rate"] == 0.25
    assert set(r["variants"]) == {"A", "B"}
    reply_rate, _, learned = strategy.learned_rates(s, tenant.id, "win_back")
    assert learned == 1 and reply_rate > strategy.PRIOR_REPLY_RATE  # 25% observed pulls the estimate up
    assert "win_back" not in {p.key for p in strategy.recommend(s, tenant)}  # cooldown after sending


def test_wrong_copy_is_blocked_until_fixed(s, tenant, customers, llm_stub):
    llm_stub(creative_json("Hi {name}, emulsion now just INR 999!", "Hi {name}, 30% off everything!"))
    campaign = orchestrator.launch_play(s, tenant, "win_back")
    assert campaign.status == "needs_fix" and len(campaign.checks["errors"]) == 2
    task = s.scalars(select(Task).where(Task.kind == "approval")).one()
    with pytest.raises(ValueError, match="verification failed"):
        orchestrator.approve(s, tenant, campaign)
    campaign.send_at = None  # send immediately once fixed
    fixed = orchestrator.approve(s, tenant, campaign, "Hi {name}, White Emulsion 10L at INR 2,400 - ready stock.")
    assert fixed["status"] == "sent" and fixed["sent"] == 36 and task.status == "open"


def test_social_posts_go_to_webhook(s, tenant, customers, llm_stub, monkeypatch):
    posted = []
    monkeypatch.setattr(distribution.httpx, "post", lambda url, **kw: posted.append((url, kw["json"])) or httpx.Response(200, request=httpx.Request("POST", url)))
    tenant.playbook = {**tenant.playbook, "distribution": {"webhook_url": "https://hooks.example/social"}}
    llm_stub(creative_json("Hi {name}, monsoon-ready walls start here.", "Hi {name}, ask us about waterproofing."))
    campaign = orchestrator.launch_custom(s, tenant, "Monsoon post", "Promote waterproofing", {}, channel="social")
    campaign.send_at = None
    assert orchestrator.approve(s, tenant, campaign)["posted"] is True
    url, payload = posted[0]
    assert url == "https://hooks.example/social" and payload["text"].startswith("Hi there,") and payload["image_brief"]


def test_team_roster_matches_the_plan(s, tenant):
    team = {a["key"]: a for a in roster(s, tenant)}
    assert list(team) == ["data_crm", "strategy", "creative", "verification", "approval", "distribution",
                          "response", "voice", "analytics", "orchestrator"]
    assert team["voice"]["status"] == "planned" and team["verification"]["status"] == "always"
    assert all(team[k]["status"] == "on" for k in ("strategy", "creative", "distribution", "response", "analytics"))


def test_feed_and_copilot(s, tenant, customers, llm_stub):
    from salescore.analytics.team import feed
    from salescore.workflows import copilot
    llm_stub([("tool", "whats_waiting", {}), ("tool", "strategy_recommendations", {})],
             [("tool", "draft_campaign", {"play": "win_back"})],
             creative_json("Hi {name}, we miss you!", "Hi {name}, 5% off your usual order."),
             [("text", "Drafted the win-back campaign; approve it in Approvals.")])
    out = copilot.ask(s, tenant, "What needs me, and draft the best campaign", [{"role": "user", "content": "hi"}])
    assert "Approvals" in out["reply"] and out["actions"][0]["type"] == "campaign"
    assert s.get(Campaign, out["actions"][0]["id"]).status == "pending_approval"  # drafted, not sent
    events = feed(s, tenant)
    assert any(e["agent"] == "Creative" for e in events) and all(e["text"] for e in events)
