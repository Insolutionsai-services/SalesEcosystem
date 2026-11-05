"""Logic check for the call loop, no models and no network: python -m salescore.voice.check_call  (in .venv-voice; not collected by pytest, which runs on Python 3.14)"""
import asyncio
import os

from . import agent, call as C, speech

LOUD = b"\x00\x20" * 160     # 20 ms of loud audio (RMS 8192)
QUIET = b"\x00\x00" * 160    # 20 ms of silence


async def main():
    sent = []

    async def send(pcm):
        sent.append(pcm)
    speech.transcribe = lambda audio, hint: ("we run a saree shop", "en")
    agent.stream_turn = lambda c, h, lang=None: iter(["Lovely, saree shop.", "Thanks for your time. " + agent.END])
    C.speak_pcm = lambda text: b"\x01\x00" * 1600     # 200 ms of "voice" per sentence
    c = C.Call({}, send)
    c.history = [{"role": "assistant", "content": "hi"}]
    c.fillers = {"en": b"\x01\x00" * 800}

    for _ in range(20): await c.feed(LOUD)             # 0.4 s of caller speech
    for _ in range(30): await c.feed(QUIET)            # 0.6 s pause: not yet the end of their turn
    assert not c.busy and not sent, "must wait for 0.7 s of silence"
    for _ in range(10): await c.feed(QUIET)
    assert c.busy, "silence should end the caller's turn"
    for _ in range(200):                                # wait for the turn to finish (first numpy import is slow)
        if not c.busy:
            break
        await asyncio.sleep(0.05)
    assert len(sent) == 2 and c.over, (len(sent), c.over)   # two sentences spoken, [END] ends the call
    assert c.history[-2:] == [{"role": "user", "content": "we run a saree shop"},
                              {"role": "assistant", "content": "Lovely, saree shop. Thanks for your time."}], c.history
    n = len(sent)
    await c.feed(LOUD)                                  # caller speech after the goodbye is ignored
    assert len(sent) == n and not c.finished(), "goodbye still playing"

    d = C.Call({}, send)                                # a noise blip shorter than MIN_SPEECH is not a turn
    for _ in range(5): await d.feed(LOUD)
    for _ in range(40): await d.feed(QUIET)
    assert not d.busy and not d.history
    print("ok: end-of-turn, half-duplex, [END], blip filter")

    # call handling: silence, voicemail, maximum length, never answered
    C.SILENCE_S = 0.2
    e = C.Call({}, send); e.nudge, e.bye = bytes(160), bytes(160); e.start()
    sent.clear()
    for _ in range(60):
        await e.feed(QUIET); await asyncio.sleep(0.01)
        if e.over: break
    assert e.nudged and e.outcome == "no_response" and e.over, "silence: ask 'are you there?' once, then end"
    assert C.MACHINE.search("Please leave a message after the tone") and C.MACHINE.search("the subscriber is not reachable"), "voicemail phrases"
    assert not C.MACHINE.search("yes I run a saree shop, tell me more")
    f = C.Call({"max_call_min": 1}, send); assert f.max_s == 60 and not f.expired() and not f.finished()
    f.start(); f.started -= 60 + 9; assert f.expired() and f.finished(), "past the maximum length the call is cut"
    g = C.Call({}, send); g.created -= C.RING_S + 16; assert g.finished(), "a number that never answered must free the line"
    print("ok: silence, voicemail, max length, unanswered")


def key_never_reaches_the_page():
    """A refused call: httpx puts the URL in its error, and Exotel's URL holds the account key."""
    import httpx
    from fastapi.testclient import TestClient
    from . import phone

    async def ready(self): self.history = [{"role": "assistant", "content": "Hi"}]

    def refuse(to, max_s): raise httpx.ConnectError("https://KEY123:TOKEN456@api.exotel.com/v1/Accounts/x")
    phone.status, phone.place, phone.Call.prepare = (lambda: {"phone_ready": True}), refuse, ready
    os.environ["VOICE_KEY"] = "k"
    r = TestClient(phone.app).post("/dial", json={"to": "+919876543210"}, headers={"X-Voice-Key": "k"})
    assert r.status_code == 502 and "KEY123" not in r.text and "TOKEN456" not in r.text and "exotel" not in r.text.lower(), r.text
    print("ok: a refused call shows no key")

asyncio.run(main())
key_never_reaches_the_page()
