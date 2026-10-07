from datetime import timedelta

from sqlalchemy import select

from salescore.core.models import Contact, Quote, Task
from salescore.core.utils import now
from salescore.services import channels, contacts
from salescore.services.tasks import add_task
from salescore.workflows import inbound, scheduler


def open_tasks(s, kind):
    return s.scalars(select(Task).where(Task.kind == kind, Task.status == "open")).all()


def test_inbound_quote_followup_and_optout(s, tenant, llm_stub):
    fake = llm_stub(
        [("tool", "search_catalog", {"query": "white emulsion 10L"})],
        [("tool", "create_quote", {"lines": [{"sku": "PNT-WE-10", "qty": 2}]}),
         ("tool", "update_contact", {"stage": "quoted", "facts": {"project": "2BHK repaint"}})],
        [("text", "Here is your quote: 2 × White Emulsion 10L, total ₹5,664.")],
    )
    reply = inbound.handle_inbound(s, tenant, "price for 2 tins white emulsion 10L?", phone="9123456789",
                                   name="Asha Rao", channel="whatsapp", ext_id="wamid.1")
    c = s.scalars(select(Contact)).one()
    assert reply.startswith("Here is your quote")
    assert c.stage == "quoted" and c.attrs["project"] == "2BHK repaint"
    assert s.scalars(select(Quote)).one().total == 5664
    assert len(open_tasks(s, "followup")) == 1
    assert fake.calls[0]["lite"] is False and fake.calls[0]["messages"][0]["role"] == "system"
    assert c.score is not None  # scored in real time

    assert inbound.handle_inbound(s, tenant, "dup", phone="9123456789", ext_id="wamid.1") is None  # redelivery

    inbound.handle_inbound(s, tenant, "STOP", phone="9123456789")
    assert c.opted_out and not open_tasks(s, "followup")
    assert channels.send(s, tenant, c, "promo", "marketing") is False


def test_tool_error_goes_back_to_model(s, tenant, llm_stub):
    fake = llm_stub([("tool", "create_quote", {"lines": [{"sku": "NOPE", "qty": 1}]})], [("text", "Which product exactly?")])
    assert inbound.handle_inbound(s, tenant, "quote me", phone="9123450000") == "Which product exactly?"
    tool_msg = fake.calls[1]["messages"][3]  # [system, user, assistant tool call, tool result]
    assert tool_msg["role"] == "tool" and "unknown sku" in tool_msg["content"]


def test_handoff_pauses_bot(s, tenant, llm_stub):
    llm_stub([("tool", "handoff_to_human", {"reason": "wants site visit with manager"})], [("text", "Connecting you now.")])
    inbound.handle_inbound(s, tenant, "I want to talk to a person", phone="9123451111")
    c = s.scalars(select(Contact)).one()
    assert c.bot_paused and len(open_tasks(s, "handoff")) == 1
    assert inbound.handle_inbound(s, tenant, "hello?", phone="9123451111") is None  # human owns it now
    inbound.human_reply(s, tenant, c, "Hi, this is Priya from sales.", resume_bot=True)
    assert not c.bot_paused and not open_tasks(s, "handoff")


def test_followup_cadence_and_skip(s, tenant, llm_stub):
    c = contacts.find_or_create_contact(s, tenant, phone="9000000002")
    c.last_inbound_at = now() - timedelta(days=2)
    add_task(s, tenant.id, c.id, "followup", now() - timedelta(minutes=1), step=0)
    llm_stub([("text", "Hi! Did the quote work for you?")])
    out = scheduler.tick(s, tenant)
    assert out["tasks_run"] == 1 and out["messages_sent"] == 1 and out["marketing"] == {"dispatched": []}
    nxt = open_tasks(s, "followup")[0]
    assert nxt.data["step"] == 1  # cadence advanced

    nxt.due_at = now() - timedelta(minutes=1)
    llm_stub([("text", "SKIP")])
    assert scheduler.tick(s, tenant)["messages_sent"] == 0
    assert not open_tasks(s, "followup")  # cadence [1, 3] exhausted


def test_daily_tick_runs_analyst(s, tenant, llm_stub):
    contacts.find_or_create_contact(s, tenant, phone="9000000003")
    llm_stub([("text", "- Call hot leads today")])
    out = scheduler.tick(s, tenant, daily=True)
    s.commit()  # metrics must be JSON-serialisable into the activity log
    assert out["insights"].startswith("- Call") and out["metrics"]["contacts_by_stage"] == {"new": 1}
