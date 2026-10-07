"""Agent switches and settings: turning an agent off really stops it; presets run only what a company needs."""
import pytest
from sqlalchemy import select

from salescore.core import agents
from salescore.core.models import Task
from salescore.core.playbook import setting
from salescore.marketing import orchestrator, strategy
from salescore.services import contacts
from salescore.workflows import inbound


def test_settings_are_validated_and_saved(tenant):
    pb = agents.update(tenant.playbook, "verification", {"pricing.max_discount_pct": "12",
                                                         "compliance.quiet_hours": [22, 7],
                                                         "compliance.banned_phrases": "cheapest, best in india"})
    tenant.playbook = pb
    assert setting(tenant, "pricing.max_discount_pct") == 12.0
    assert setting(tenant, "compliance.quiet_hours") == [22, 7]
    assert setting(tenant, "compliance.banned_phrases") == ["cheapest", "best in india"]
    with pytest.raises(ValueError, match="Maximum discount"):
        agents.update(pb, "verification", {"pricing.max_discount_pct": 300})
    with pytest.raises(ValueError, match="isn't a setting"):
        agents.update(pb, "verification", {"channel": "whatsapp"})
    with pytest.raises(ValueError, match="can't be switched off"):
        agents.update(pb, "verification", on=False)


def test_replies_only_preset(s, tenant, llm_stub):
    tenant.playbook = agents.apply_preset(tenant.playbook, "replies")
    assert agents.state(tenant, "strategy") == "off" and agents.state(tenant, "response") == "on"
    assert strategy.recommend(s, tenant) == []
    with pytest.raises(ValueError, match="Strategy agent is off"):
        orchestrator.launch_play(s, tenant, "win_back")


def test_response_off_hands_messages_to_a_person(s, tenant, llm_stub):
    llm_stub()  # any AI call would fail the test
    tenant.playbook = agents.update(tenant.playbook, "response", on=False)
    assert inbound.handle_inbound(s, tenant, "price of paint?", phone="9000011111") is None
    inbound.handle_inbound(s, tenant, "hello?", phone="9000011111")
    assert len(s.scalars(select(Task).where(Task.kind == "handoff")).all()) == 1  # one task, not one per message


def test_broadcast_preset_sends_my_own_text(s, tenant, llm_stub):
    llm_stub()  # Creative is off: no AI call
    tenant.playbook = agents.apply_preset(tenant.playbook, "broadcast")
    contacts.import_contacts(s, tenant, "name,phone,consent\nAsha,9000022222,yes\n")
    c = orchestrator.launch_custom(s, tenant, "Diwali hours", "We're open till 10 PM all Diwali week.", {})
    assert c.variants == [{"label": "A", "angle": "written by you", "text": "Hi {name}, We're open till 10 PM all Diwali week."}]
    assert orchestrator.approve(s, tenant, c, send_now=True)["sent"] == 1


def test_distribution_off_blocks_sending(s, tenant, llm_stub):
    tenant.playbook = agents.apply_preset(tenant.playbook, "broadcast")
    c = orchestrator.launch_custom(s, tenant, "Note", "Shop closed Sunday.", {})
    tenant.playbook = agents.update(tenant.playbook, "distribution", on=False)
    with pytest.raises(ValueError, match="Distribution agent is off"):
        orchestrator.approve(s, tenant, c, send_now=True)
