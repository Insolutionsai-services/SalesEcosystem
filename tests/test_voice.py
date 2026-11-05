"""Phone calls in the app: a finished call becomes a CRM record that Analytics, the Voice agent and the feed all read.
The voice service itself (speech, telephony) runs in its own environment; see salescore/voice/check_call.py."""
import pytest
from fastapi.testclient import TestClient

from salescore.analytics.team import feed, roster
from salescore.api import deps
from salescore.api.app import app
from salescore.core.models import Contact

RESULT = {"outcome": "interested", "needs": "faster replies on WhatsApp", "feedback": "worried about the monthly cost",
          "answers": {"What kind of business do you run?": "saree shop"}, "next_step": "Demo tomorrow 5 pm"}


@pytest.fixture
def client(s, monkeypatch):
    monkeypatch.setenv("VOICE_KEY", "k")
    app.dependency_overrides[deps.db] = lambda: s
    yield TestClient(app)
    app.dependency_overrides.clear()


def _report(client, tenant, to="98765 43210", key="k"):
    body = {"tenant_id": tenant.id, "to": to, "summary": RESULT, "timings": [{"first_audio_s": 1.9}, {"first_audio_s": 2.5}],
            "transcript": [{"role": "assistant", "content": "Hi"}, {"role": "user", "content": "Hello"}]}
    return client.post("/v1/voice/result", json=body, headers={"X-Voice-Key": key})


def test_finished_call_reaches_crm_analytics_agent_and_feed(client, s, tenant):
    assert _report(client, tenant, key="wrong").status_code == 403      # only the voice service may report calls
    s.add(Contact(tenant_id=tenant.id, name="Lakshmi", phone="919876543210", stage="lead"))
    s.flush()
    assert _report(client, tenant).json() == {"ok": True}
    h = {"X-API-Key": tenant.api_key}

    calls = client.get("/v1/voice/calls", headers=h).json()
    assert calls[0]["who"] == "Lakshmi" and calls[0]["summary"]["outcome"] == "interested" and len(calls[0]["transcript"]) == 2

    c = client.get("/v1/analytics?days=30", headers=h).json()["calls"]
    assert c["made"] == 1 and c["outcomes"] == {"interested": 1} and c["reply_s"] == 2.9    # mean first audio 2.2 s + 0.7 s wait
    assert c["recent"][0]["needs"] == "faster replies on WhatsApp" and c["recent"][0]["feedback"] == "worried about the monthly cost"

    stats = {a["key"]: a["stats"] for a in roster(s, tenant)}["voice"]
    assert stats["calls (30d)"] == 1 and stats["interested"] == 1
    assert any(e["agent"] == "Voice" and "Lakshmi" in e["text"] for e in feed(s, tenant))


def test_calls_need_consent_the_agent_switched_on_and_nobody_opted_out(client, s, tenant, monkeypatch):
    tenant.plan = "unlimited"
    h = {"X-API-Key": tenant.api_key}
    assert client.post("/v1/voice/call", json={"to": "9876543210"}, headers=h).status_code == 400            # no consent tick
    assert "Switch the Voice agent on" in client.post("/v1/voice/call", json={"to": "9876543210", "agreed": True}, headers=h).json()["detail"]
    client.put("/v1/agents/voice", json={"enabled": True, "values": {"voice.main_idea": "x", "voice.goal": "y"}}, headers=h)
    s.add(Contact(tenant_id=tenant.id, name="Ravi", phone="919876543210", stage="lead", opted_out=True))
    s.flush()
    assert client.post("/v1/voice/call", json={"to": "9876543210", "agreed": True}, headers=h).status_code == 403   # opted out: never called


def test_call_plan_covers_problem_question_and_close():
    from salescore.voice import agent
    asked = lambda q: "not as written): " + q   # the step that asks q (other steps only mention it as a fallback)
    plan = agent._plan({**agent.DEFAULT})
    assert plan[0].startswith("STEP: OPENING") and plan[-1].startswith("STEP: CLOSE") and "farewell" in plan[-1]
    i = next(n for n, x in enumerate(plan) if asked(agent.DISCOVERY) in x)   # our problem question is added when the company has none...
    assert "THEIR PROBLEM" in plan[i + 1]                                   # ...and answered right after it
    own = agent._plan({**agent.DEFAULT, "questions": ["What is the key issue you face?"]})
    assert not any(asked(agent.DISCOVERY) in x for x in own)                 # the company's own problem question is used, not doubled


def test_auto_calling_window_gap_consent(s, tenant):
    from datetime import datetime, timedelta

    from salescore.services import contacts, voice_auto
    tenant.plan = "unlimited"
    tenant.playbook = {**tenant.playbook, "agents": {"voice": {"enabled": True}},
                       "voice": {"auto_call": "on", "call_from": "10:00", "call_until": "18:00", "gap_min": "5"}}
    contacts.import_contacts(s, tenant, "phone,name,business,consent\n9876543210,Ravi,paint shop,yes\n9876543211,Sita,hardware,no\n9876543212,Anil,tile shop,yes\n")
    s.flush()
    ist = lambda h, m=0: datetime(2026, 11, 4, h, m) - timedelta(hours=5, minutes=30)   # naive UTC for an IST clock time
    assert voice_auto.next_contact(s, tenant, ist(8))[1] == "outside the calling window"
    first, _ = voice_auto.next_contact(s, tenant, ist(11))
    assert first.name == "Ravi" and voice_auto.business(first) == "paint shop"        # the CSV's business column reaches the call
    contacts.update_contact(s, tenant, first, attrs={"voice_tried_at": ist(11).isoformat()})
    assert voice_auto.next_contact(s, tenant, ist(11, 2))[1] == "waiting out the gap since the last call"
    assert voice_auto.next_contact(s, tenant, ist(11, 6))[0].name == "Anil"           # Sita has no consent: skipped
    tenant.playbook["voice"]["call_from"] = "06:00"
    assert voice_auto.next_contact(s, tenant, ist(7))[1] == "outside the calling window"   # never before 09:00
    tenant.playbook["voice"]["auto_call"] = "off"
    assert voice_auto.next_contact(s, tenant, ist(11, 6))[0] is None


def test_minutes_budget_stops_calls(s, tenant):
    from salescore.services import voice_auto
    from salescore.services.activity import log_activity
    tenant.plan = "unlimited"                              # the free plan has its own fixed minutes
    tenant.playbook = {**tenant.playbook, "voice": {"minutes_budget": "10", "max_call_min": "3"}}
    for secs in (61, 120, 0):                              # 61 s bills as 2 minutes, 120 s as 2, an unanswered call as 0
        log_activity(s, tenant.id, None, "call", "x", "voice", outcome="interested", duration_s=secs)
    s.flush()
    assert voice_auto.minutes_used(s, tenant) == 4 and voice_auto.out_of_minutes(s, tenant) is None
    log_activity(s, tenant.id, None, "call", "x", "voice", outcome="interested", duration_s=170)   # 3 more: 7 used, 3 left = room for one full call
    s.flush()
    assert voice_auto.out_of_minutes(s, tenant) is None
    log_activity(s, tenant.id, None, "call", "x", "voice", outcome="interested", duration_s=30)    # 8 used, 2 left < 3: stop
    s.flush()
    assert "used up" in voice_auto.out_of_minutes(s, tenant)


def _script(client, h):
    client.put("/v1/agents/voice", json={"enabled": True, "values": {"voice.main_idea": "x", "voice.goal": "y", "voice.minutes_budget": "500", "voice.max_call_min": "60"}}, headers=h)


def _fake_voice(monkeypatch):
    """Stands in for the voice service and remembers what it was asked to do."""
    from salescore.api import routes
    seen = []

    def fake(method, path, timeout=30, **kw):
        seen.append((path, kw.get("json")))
        return {"busy": False, "phone_ready": False, "ai_ready": True, "provider": "secret-co", "missing": ["SECRET_NAME"], "reply": "Hello?", "status": "dialing"}
    monkeypatch.setattr(routes, "_voice", fake)
    return seen


def test_free_plan_gets_15_minutes_and_settings_cannot_raise_them(client, s, tenant, monkeypatch):
    from salescore.services import voice_auto
    from salescore.services.activity import log_activity
    tenant.plan = "trial"
    h, seen = {"X-API-Key": tenant.api_key}, _fake_voice(monkeypatch)
    _script(client, h)
    assert voice_auto.budget(tenant) == 15                                     # the company asked for 500
    assert voice_auto.call_limit(s, tenant) == 15                              # and a 60-minute call: capped to what is left
    r = client.post("/v1/voice/call", json={"to": "9876543210", "agreed": True}, headers=h)
    assert r.status_code == 402 and r.json()["detail"]["limit"] == "phone_calls" and not [p for p, _ in seen if p == "/dial"]   # real calls: contact us
    log_activity(s, tenant.id, None, "call", "x", "voice", duration_s=13 * 60)
    s.flush()
    assert voice_auto.call_limit(s, tenant) == 2                               # the last 2 minutes can still be used
    log_activity(s, tenant.id, None, "call", "x", "voice", duration_s=2 * 60)
    s.flush()
    assert client.post("/v1/voice/chat", json={"history": []}, headers=h).status_code == 402
    assert client.post("/v1/voice/say", json={"text": "hi"}, headers=h).status_code == 400
    assert client.get("/v1/plan", headers=h).json()["usage"]["call_minutes"] == 15


def test_practice_uses_the_free_minutes_too(client, s, tenant, monkeypatch):
    tenant.plan = "trial"
    h = {"X-API-Key": tenant.api_key}
    _fake_voice(monkeypatch)
    _script(client, h)
    codes = [client.post("/v1/voice/chat", json={"history": []}, headers=h).status_code for _ in range(46)]
    assert codes == [200] * 45 + [402]                                         # 45 exchanges of 20 s = 15 minutes
    assert not [e for e in feed(s, tenant) if "voice_turn" in str(e)]


def test_owner_companies_keep_their_own_budget(client, s, tenant, monkeypatch):
    from salescore.services import voice_auto
    tenant.plan = "unlimited"                                                  # Gamyan, the demos, real estate
    h = {"X-API-Key": tenant.api_key}
    _fake_voice(monkeypatch)
    _script(client, h)
    assert voice_auto.budget(tenant) == 500 and voice_auto.call_limit(s, tenant) == 60
    for _ in range(50):
        assert client.post("/v1/voice/chat", json={"history": []}, headers=h).status_code == 200
    assert voice_auto.minutes_used(s, tenant) == 0                             # practice is not counted for them


def test_no_late_night_calls_and_a_zero_budget_means_no_calls(client, s, tenant, monkeypatch):
    from datetime import datetime

    from salescore.api import routes
    from salescore.services import voice_auto
    tenant.plan = "unlimited"
    h = {"X-API-Key": tenant.api_key}
    seen = _fake_voice(monkeypatch)
    _script(client, h)
    monkeypatch.setattr(routes, "now", lambda: datetime(2026, 11, 4, 18, 0))           # 23:30 in India
    r = client.post("/v1/voice/call", json={"to": "9876543210", "agreed": True}, headers=h)
    assert r.status_code == 400 and "21:00" in r.json()["detail"] and not [p for p, _ in seen if p == "/dial"]
    tenant.playbook = {**tenant.playbook, "voice": {"minutes_budget": "0", "max_call_min": "3"}}
    assert "used up" in voice_auto.out_of_minutes(s, tenant)


def test_status_never_names_the_phone_company_or_its_settings(client, tenant, monkeypatch):
    _fake_voice(monkeypatch)
    text = client.get("/v1/voice/status", headers={"X-API-Key": tenant.api_key}).text
    assert "secret-co" not in text and "SECRET_NAME" not in text and '"phone_ready":false' in text


def test_free_plan_never_dials_automatically(s, tenant):
    from datetime import datetime

    from salescore.services import voice_auto
    tenant.plan = "trial"
    tenant.playbook = {**tenant.playbook, "agents": {"voice": {"enabled": True}}, "voice": {"auto_call": "on"}}
    assert voice_auto.next_contact(s, tenant, datetime(2026, 11, 4, 6))[0] is None


def test_the_draft_is_told_where_it_can_go(client, tenant):
    got = client.get("/v1/marketing/destinations", headers={"X-API-Key": tenant.api_key}).json()
    assert [d["label"] for d in got] == ["WhatsApp", "Email", "Instagram", "Meta Ads", "Google Ads"]
    assert not any(d["ready"] for d in got if d["key"] in ("instagram", "meta_ads", "google_ads"))   # not connected until the link is set
