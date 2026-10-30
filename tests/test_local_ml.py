"""Trained local models answer routine messages without any LLM call; the rest goes to the LLM."""
import httpx
import pytest
from sqlalchemy import select

from salescore.ai import llm
from salescore.analytics.metrics import ai_usage
from salescore.analytics.training import brain_status, train_tenant
from salescore.core import config
from salescore.core.models import Activity, Contact, Quote
from salescore.core.utils import now
from salescore.ml.intents import IntentModel, extract_qty
from salescore.ml.text_index import TextIndex
from salescore.workflows import inbound


def ask(s, tenant, text, phone="9555500001"):
    reply = inbound.handle_inbound(s, tenant, text, phone=phone, name="Ravi Kumar")
    via = s.scalars(select(Activity).where(Activity.type == "msg_out").order_by(Activity.id.desc())).first().data["via"]
    return reply, via


def test_extract_qty_ignores_pack_sizes():
    assert extract_qty("price of 20L white emulsion") is None
    assert extract_qty("need 5 tins of 10L white") == 5
    assert extract_qty("3 x putty 40kg") == 3
    assert extract_qty("qty: 12 please") == 12


def test_intent_model_and_text_index():
    model = IntentModel({"price": ["rate of weathershield"]})
    assert model.predict("hello")[0] == "greeting"
    assert model.predict("what is the price of white paint")[0] == "price"
    assert model.predict("please connect me to a sales person")[0] == "human"
    index = TextIndex(["a", "b"], ["white emulsion 10L", "white emulsion 20L"])
    assert index.best("white emulsion 20L", 0.3, 0.02)[0] == "b"
    assert index.best("white emulsion", 0.3, 0.05) is None  # too close to call -> LLM clarifies


def test_trained_fast_path_answers_without_llm(s, tenant, llm_stub):
    status = train_tenant(s, tenant)
    assert status["items"] == 4 and status["faq"] == 2 and brain_status(tenant)["trained"]
    fake = llm_stub()  # no scripted turns: any LLM call fails the test

    reply, via = ask(s, tenant, "hi")
    assert via == "fastpath" and reply.startswith("Hi Ravi")
    reply, via = ask(s, tenant, "what is the price of white emulsion paint 10L")
    assert via == "fastpath" and "2,400.00" in reply and "50 available" in reply
    reply, via = ask(s, tenant, "need 3 tins of white emulsion paint 10L, price?")
    q = s.scalars(select(Quote)).one()
    assert via == "fastpath" and q.total == round(3 * 2400 * 1.18, 2) and f"Quote #{q.id}" in reply
    reply, via = ask(s, tenant, "what is the warranty?")
    assert via == "fastpath" and "7-year" in reply
    assert not fake.calls

    ask(s, tenant, "can someone call me back", phone="9555500002")
    assert s.scalars(select(Contact).where(Contact.phone == "919555500002")).one().bot_paused


def test_uncertain_messages_go_to_llm_and_cost_is_tracked(s, tenant, llm_stub, monkeypatch):
    train_tenant(s, tenant)
    monkeypatch.setattr(config, "LLM_PRICE_IN_PER_M", 0.10)
    monkeypatch.setattr(config, "LLM_PRICE_OUT_PER_M", 0.40)
    fake = llm_stub([("text", "For bedrooms we suggest a soft pastel in silk finish.")])
    reply, via = ask(s, tenant, "which colour would suit my bedroom walls?")
    assert via == "llm" and "pastel" in reply and len(fake.calls) == 1
    ask(s, tenant, "hi")  # local
    usage = ai_usage(s, tenant.id, now().replace(year=2000))
    assert usage["replies"] == 2 and usage["replies_local"] == 1 and usage["local_answer_rate"] == 0.5
    assert usage["llm_calls"] == 1 and usage["llm_cost_usd"] == pytest.approx((1000 * 0.10 + 50 * 0.40) / 1e6)


def test_provider_presets(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_MODEL", None)
    monkeypatch.setattr(config, "LLM_MODEL_LITE", None)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    cfg = llm.settings()
    assert "generativelanguage.googleapis.com" in cfg.base_url and cfg.api_key == "g-key" and cfg.model_lite

    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    assert llm.settings().base_url == "http://localhost:11434/v1"

    monkeypatch.setattr(config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(config, "LLM_EXTRA_BODY", {})
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)
    sent, busy = [], httpx.Response(429, text="temporarily rate-limited upstream")
    ok = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    monkeypatch.setattr(llm, "_down", {})
    replies = [busy, busy, ok, ok]
    monkeypatch.setattr(llm._client, "post", lambda url, **kw: sent.append(kw["json"]) or replies.pop(0))
    assert llm.chat([{"role": "user", "content": "hi"}], lite=True).text == "ok"
    lightning, gemma = "nvidia/nemotron-3.5-lightning:free", "google/gemma-4-26b-a4b-it:free"
    assert [b["model"] for b in sent] == [lightning, gemma, gemma]  # rate-limited: straight to the next model; the last one retries
    sent.clear()
    llm.chat([{"role": "user", "content": "hi"}], lite=True)
    assert [b["model"] for b in sent] == [gemma]  # lightning rests for a minute instead of failing every message
    assert all(b["model"].endswith(":free") for b in sent)
    assert sent[0]["reasoning"] == {"effort": "low", "exclude": True}
    assert sent[0]["provider"] == {"max_price": {"prompt": 0, "completion": 0}}
    monkeypatch.setattr(config, "LLM_MODEL", "qwen/qwen3.8-27b")  # the paid variant
    with pytest.raises(llm.LLMUnavailable, match="free-only"):
        llm.chat([{"role": "user", "content": "hi"}])
    monkeypatch.setattr(config, "LLM_MODEL", None)

    monkeypatch.setattr(config, "LLM_PROVIDER", "custom")
    with pytest.raises(llm.LLMUnavailable):
        llm.chat([{"role": "user", "content": "hi"}])


def test_chat_retries_and_parses_tool_calls(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "LLM_MODEL", "qwen3:8b")
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)
    monkeypatch.setattr(config, "LLM_EXTRA_BODY", {"chat_template_kwargs": {"enable_thinking": False}})
    responses = [httpx.Response(429, text="slow down"), httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "<think>plan the lookup</think>Checking.", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "search_catalog", "arguments": '{"query": "paint"}'}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}})]
    sent = []
    monkeypatch.setattr(llm, "_down", {})
    monkeypatch.setattr(llm._client, "post", lambda url, **kw: sent.append(kw["json"]) or responses.pop(0))
    reply = llm.chat([{"role": "user", "content": "paint?"}], tools=[{"type": "function"}])
    assert len(sent) == 2 and sent[0]["model"] == "qwen3:8b" and sent[0]["tool_choice"] == "auto"
    assert reply.tool_calls[0]["function"]["name"] == "search_catalog" and reply.usage["prompt_tokens"] == 10
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False} and reply.text == "Checking."


def test_llm_outage_hands_off_instead_of_failing(s, tenant, monkeypatch):
    def down(*a, **k):
        raise llm.LLMUnavailable("provider down")
    monkeypatch.setattr(llm, "chat", down)
    reply, via = ask(s, tenant, "which colour would suit my bedroom walls?")
    contact = s.scalars(select(Contact)).one()
    assert via == "fallback" and "team member" in reply and not contact.bot_paused
    assert s.scalars(select(Activity).where(Activity.type == "msg_in")).one()  # the customer's message is kept


def test_company_gemini_key_first_then_open_source(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(llm, "_down", {})
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)
    ok = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    sent = []
    monkeypatch.setattr(llm._client, "post", lambda url, **kw: sent.append((url, kw["json"]["model"], kw["headers"])) or ok)
    assert llm.chat([{"role": "user", "content": "hi"}], own=("g-company", "gemini-3.5-flash-lite")).model == "gemini-3.5-flash-lite"
    assert "generativelanguage" in sent[0][0] and sent[0][2]["Authorization"] == "Bearer g-company"

    sent.clear()  # a revoked key: Google says 400, the open-source model answers instead
    replies = [httpx.Response(400, text="API key not valid"), ok]
    monkeypatch.setattr(llm._client, "post", lambda url, **kw: sent.append(kw["json"]["model"]) or replies.pop(0))
    assert llm.chat([{"role": "user", "content": "hi"}], own=("g-revoked", "gemini-3.5-flash-lite")).model == "nvidia/nemotron-3.5-lightning:free"
    assert sent == ["gemini-3.5-flash-lite", "nvidia/nemotron-3.5-lightning:free"]


def test_free_tier_key_gets_the_newest_flash_lite_it_can_use(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)
    ids = ["models/gemini-3.8-flash", "models/gemini-2.5-flash-lite", "models/gemini-3.5-flash-lite", "models/gemini-3.6-flash-lite-preview",
           "models/gemini-flash-lite-latest", "models/gemini-2.5-flash-lite-preview-tts", "models/gemini-3.1-flash-lite"]
    assert llm._lite_first(ids) == ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-3.5-flash-lite", "gemini-flash-lite-latest",
                                    "gemini-3.6-flash-lite-preview"]
    monkeypatch.setattr(llm._client, "get", lambda url, **kw: httpx.Response(200, json={"data": [{"id": i} for i in ids]}))
    tried = []
    no_quota = httpx.Response(429, text="free tier: limit 0")
    ok = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    monkeypatch.setattr(llm._client, "post", lambda url, **kw: tried.append(kw["json"]["model"]) or (no_quota if kw["json"]["model"] == "gemini-3.1-flash-lite" else ok))
    assert llm.pick_gemini_model("AIza-free") == "gemini-2.5-flash-lite"  # 3.1 has no free quota on this key: the next preferred
    assert tried == ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]
    monkeypatch.setattr(llm._client, "get", lambda url, **kw: httpx.Response(400, text="API key not valid"))
    with pytest.raises(ValueError, match="didn't accept"):
        llm.pick_gemini_model("bad")
