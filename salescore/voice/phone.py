"""The voice service: Exotel (Voicebot/Stream applet) or Twilio Media Streams <-> Call, plus the endpoints the app's Voice agent page uses.
One call at a time. Started by `make dev` (or `make voice`), in its own Python 3.12 environment (.venv-voice).
ponytail: the phone-provider side is UNTESTED against a real provider until you have an account; test the loop with simulate.py."""
from . import env  # noqa: F401  (loads .env.voice before anything reads the LLM settings)

import asyncio
import audioop
import base64
import json
import logging
import os
import time

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel

from . import agent, speech
from .call import RING_S, Call

app = FastAPI()
CUR: dict = {"call": None}   # the single live call
LOG = os.getenv("VOICE_CALL_LOG", str(env.ROOT / "calls.jsonl"))
PROVIDER_VARS = {"twilio": ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM"),
                 "exotel": ("EXOTEL_SID", "EXOTEL_KEY", "EXOTEL_TOKEN", "EXOTEL_CALLER_ID", "EXOTEL_FLOW_ID")}


@app.on_event("startup")
async def warm():
    """Load both voices now: the first sentence otherwise takes ~19 s (the greeting would be silent)."""
    asyncio.get_running_loop().run_in_executor(None, lambda: [speech._voice(k) for k in ("en", "te")])


def guard(x_voice_key: str | None = Header(None)):
    """/dial starts a real phone call: only the app (which holds VOICE_KEY) may use it. Fails closed when no key is set.
    The provider's /stream websocket is separate and only does anything while a call this app started is pending."""
    key = os.getenv("VOICE_KEY")
    if not key:
        raise HTTPException(503, "VOICE_KEY is not set")
    if x_voice_key != key:
        raise HTTPException(401, "bad voice key")


class Dial(BaseModel):
    to: str                 # customer's number, +91XXXXXXXXXX
    config: dict = {}       # main_idea / goal / questions / language: see agent.DEFAULT


class Chat(BaseModel):
    config: dict = {}
    history: list[dict] = []   # [{role: user|assistant, content}]; empty = the call just connected
    end: bool = False          # the person ends the call: just summarize


def _config(raw: dict) -> dict:
    c = {**agent.DEFAULT, **{k: v for k, v in raw.items() if v not in (None, "", [])}}
    c["questions"] = [q.strip() for q in c["questions"] if q.strip()]
    return c


def _provider() -> str:
    return os.getenv("PHONE_PROVIDER", "exotel")


def place(to: str, max_s: int = 180):
    host, e = os.environ["PUBLIC_HOST"], os.environ
    if _provider() == "twilio":   # Twilio takes the stream instruction inline, no app to configure
        r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{e['TWILIO_SID']}/Calls.json", auth=(e["TWILIO_SID"], e["TWILIO_TOKEN"]),
                       data={"To": to, "From": e["TWILIO_FROM"], "Twiml": f'<Response><Connect><Stream url="wss://{host}/stream"/></Connect></Response>',
                             "Timeout": RING_S,             # stop ringing after this many seconds (no answer = no cost)
                             "TimeLimit": max_s + 30})      # Twilio's own hard cut if ours fails: a call can never run on and eat the minutes
    else:   # Exotel: rings the customer, then runs your flow, which must contain a Voicebot applet pointing at wss://PUBLIC_HOST/stream
        r = httpx.post(f"https://{e['EXOTEL_KEY']}:{e['EXOTEL_TOKEN']}@{e.get('EXOTEL_HOST', 'api.exotel.com')}/v1/Accounts/{e['EXOTEL_SID']}/Calls/connect.json",
                       data={"From": to, "CallerId": e["EXOTEL_CALLER_ID"], "Url": f"http://my.exotel.com/{e['EXOTEL_SID']}/exoml/start_voice/{e['EXOTEL_FLOW_ID']}"})
    r.raise_for_status()


@app.get("/status", dependencies=[Depends(guard)])
def status():
    missing = [v for v in ("PUBLIC_HOST", *PROVIDER_VARS.get(_provider(), ())) if not os.getenv(v)]
    return {"busy": bool(CUR["call"] and not CUR["call"].finished()), "phone_ready": not missing, "ai_ready": bool(os.getenv("LLM_API_KEY"))}


@app.post("/chat", dependencies=[Depends(guard)])
def chat(b: Chat):
    """Text rehearsal of the call: same prompt, same model, no phone. The page's 'Try it' box."""
    c = _config(b.config)
    last = next((m["content"] for m in reversed(b.history) if m["role"] == "user"), "")
    if b.end:
        return {"summary": agent.summarize(c, b.history)}
    parts = list(agent.stream_turn(c, b.history, speech.lang_of(last) if last else None))   # same path as a live call, Telugu guard included
    done = any(agent.END in p for p in parts)
    text = " ".join(p.replace(agent.END, "").strip() for p in parts).strip()
    if text.endswith("?"):
        done = False                  # same rule as a live call: an unanswered question never ends it
    out = {"reply": text, "done": done}
    if done:
        out["summary"] = agent.summarize(c, [*b.history, {"role": "assistant", "content": text}])
    return out


class Say(BaseModel):
    text: str


@app.post("/tts", dependencies=[Depends(guard)])
async def tts(b: Say):
    """The agent's voice for the page's rehearsal chat: the same Piper voices a real call uses."""
    return Response(await asyncio.to_thread(speech.speak, b.text.replace(agent.END, "")[:600]), media_type="audio/wav")


@app.post("/hear", dependencies=[Depends(guard)])
async def hear(req: Request, lang: str = "auto"):
    """The page's microphone: a recording in, the words out, through the same Whisper model a real call uses."""
    audio = await req.body()
    if len(audio) < 2000:
        return {"text": "", "lang": lang}
    text, got = await asyncio.to_thread(speech.transcribe_groq, audio, lang if lang in ("te", "en") else "auto")
    return {"text": text, "lang": got}


@app.post("/dial", dependencies=[Depends(guard)])
async def dial(d: Dial):
    if CUR["call"] and not CUR["call"].finished():
        raise HTTPException(409, "A call is already in progress.")
    status_ = status()
    if not status_["phone_ready"]:
        raise HTTPException(503, "The phone line isn't connected yet.")
    call = Call(_config(d.config))
    call.to = d.to
    await call.prepare()          # greeting is written and voiced before the phone rings
    CUR["call"] = call
    try:
        await asyncio.to_thread(place, d.to, call.max_s)
    except httpx.HTTPError as e:
        CUR["call"] = None
        logging.error("the phone company refused a call: %s", type(e).__name__)   # never the error text: its URL can hold the account key
        raise HTTPException(502, "The call couldn't be placed. Check the number and try again.")
    asyncio.create_task(_unanswered(call))
    return {"status": "dialing", "greeting": call.history[0]["content"]}


async def _report(call: Call, summary: dict):
    """Write the call to calls.jsonl and tell the app (it becomes a CRM record, Analytics numbers, and counts against the minutes budget)."""
    secs = round(call.elapsed()) if call.started else 0
    with open(LOG, "a", encoding="utf8") as f:
        f.write(json.dumps({"at": time.strftime("%F %T"), "company": call.c["company"], "tenant": call.c.get("tenant_id"), "summary": summary,
                            "transcript": call.history, "timings": call.timings, "duration_s": secs}, ensure_ascii=False) + chr(10))
    if call.c.get("tenant_id"):
        try:
            await asyncio.to_thread(httpx.post, os.getenv("VOICE_APP_URL", "http://127.0.0.1:8000") + "/v1/voice/result", timeout=10,
                                    headers={"X-Voice-Key": os.getenv("VOICE_KEY", "")},
                                    json={"tenant_id": call.c["tenant_id"], "to": call.to, "summary": summary, "transcript": call.history,
                                          "timings": call.timings, "duration_s": secs})
        except httpx.HTTPError:
            pass   # ponytail: still in calls.jsonl; add a replay when results must never be lost


async def _unanswered(call: Call):
    """The phone rang out, was busy or was refused: the stream never opened. Free the line and record it, so the next call is not blocked."""
    await asyncio.sleep(RING_S + 15)
    if call.started is None and CUR["call"] is call:
        call.over = True
        await _report(call, {"outcome": "no_answer"})


@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    call, sid, key, mulaw = CUR["call"], None, "stream_sid", False
    if call is None or call.started is not None or call.finished():   # only a call waiting to connect, and only once
        return await ws.close()

    async def send(pcm: bytes):
        data = audioop.lin2ulaw(pcm, 2) if mulaw else pcm
        await ws.send_json({"event": "media", key: sid, "media": {"payload": base64.b64encode(data).decode()}})
    call.send = send
    try:
        async for raw in ws.iter_text():
            m = json.loads(raw)
            ev = m.get("event")
            if ev == "start":
                mulaw, key = "streamSid" in m, ("streamSid" if "streamSid" in m else "stream_sid")   # Twilio: mulaw + camelCase
                sid = m.get(key) or (m.get("start") or {}).get(key)
                call.start()
                await call.say(call.greeting)
            elif ev == "media":
                data = base64.b64decode(m["media"]["payload"])
                await call.feed(audioop.ulaw2lin(data, 2) if mulaw else data)
            elif ev == "stop":
                break
            if call.finished():
                break
    finally:
        await ws.close()
        call.over = True
        summary = {"outcome": call.outcome} if call.outcome else await asyncio.to_thread(agent.summarize, call.c, call.history) if len(call.history) > 1 else {"outcome": "no_answer"}
        await _report(call, summary)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("VOICE_HOST", "127.0.0.1"), port=int(os.getenv("VOICE_PORT", 8766)))
