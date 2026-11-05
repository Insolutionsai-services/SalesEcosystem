"""End-to-end check of the phone adapter with no provider: drives /stream the way Twilio or Exotel would (8 kHz audio, start/media/stop
messages) while Piper speaks the caller. The finished call is reported to the running app, so `make dev` must be running with the
same VOICE_KEY. Run in .venv-voice:  python -m salescore.voice.check_stream TENANT_ID [twilio|exotel]"""
from . import env  # noqa: F401  (loads .env.voice first)

import asyncio
import audioop
import base64
import sys
import time

from fastapi.testclient import TestClient

from . import call as C, phone

tenant_id, style = int(sys.argv[1]), (sys.argv[2] if len(sys.argv) > 2 else "twilio")
mulaw, key = style == "twilio", "streamSid" if style == "twilio" else "stream_sid"   # Twilio: mulaw + camelCase. Exotel: PCM16 + snake_case
LINES = ["Yes, go ahead. I run a saree shop in Hyderabad.", "Around fifty messages a day, and I reply to them myself.",
         "Okay, a demo sounds good. Tomorrow at five in the evening works."]

call = C.Call({"tenant_id": tenant_id})
call.to = "+919876543210"
asyncio.run(call.prepare())
phone.CUR["call"] = call
call.sent = 0
orig_say = call.say


async def counting_say(pcm):
    call.sent += len(pcm)
    await orig_say(pcm)
call.say = counting_say
callers = [C.speak_pcm(t) for t in LINES]


def frame(pcm: bytes) -> dict:
    data = audioop.lin2ulaw(pcm, 2) if mulaw else pcm
    return {"event": "media", key: "S1", "media": {"payload": base64.b64encode(data).decode()}}


with TestClient(phone.app).websocket_connect("/stream") as ws:
    heard = 0

    def hear_all():
        """Read the agent's audio until everything it sent so far has arrived."""
        global heard
        while heard < call.sent:
            m = ws.receive_json()
            assert m["event"] == "media" and m[key] == "S1", m
            raw = base64.b64decode(m["media"]["payload"])
            heard += len(audioop.ulaw2lin(raw, 2) if mulaw else raw)
    ws.send_json({"event": "start", key: "S1", "start": {key: "S1"}})
    while not call.sent:
        time.sleep(0.05)
    time.sleep(0.5)
    hear_all()
    print(f"[{style}] greeting played ({heard / 16000:.1f} s of audio): {call.history[0]['content']}")
    for line, pcm in zip(LINES, callers):
        call.mute_until = 0                                   # the caller waited until the agent finished speaking
        t0 = time.perf_counter()
        for i in range(0, len(pcm) + 24000, 640):             # the line, then 1.5 s of silence
            ws.send_json(frame(pcm[i:i + 640] if i < len(pcm) else b"\0" * 640))
        while not call.busy and time.perf_counter() - t0 < 5:
            time.sleep(0.05)
        while call.busy and not call.over:
            time.sleep(0.1)
        time.sleep(0.3)
        hear_all()
        t = call.timings[-1]
        print(f"caller: {line}\n  heard : {t['heard']}\n  agent : {call.history[-1]['content']}\n  first audio after {t['first_audio_s']} s, whole reply {t['total_s']} s")
        if call.over:
            break
    ws.send_json({"event": "stop"})
print("done: call over =", call.over, "| the app should now list this call")
