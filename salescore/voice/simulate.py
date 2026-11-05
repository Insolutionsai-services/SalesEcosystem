"""Simulated phone call, no provider needed: Piper speaks the caller's lines, the real call loop answers (Groq ASR + LLM, Piper TTS).
Run: python -m salescore.voice.simulate      Writes the whole call to sim_call.wav (8 kHz) so you can listen to it."""
import asyncio
import json
import os
import time
import wave

from . import env  # noqa: F401  (loads .env.voice before anything reads the LLM settings)
from . import agent, call as C

LINES = [("en", "Yes, go ahead. I run a saree shop in Hyderabad."),
         ("te", "రోజుకు సుమారు యాభై మెసేజ్‌లు వస్తాయి, నేనే రిప్లై ఇస్తాను."),
         ("en", "Okay, a demo sounds good. Tomorrow at five in the evening is fine.")]


async def main():
    out, tape = [], bytearray()

    async def send(pcm):
        out.append(pcm)
        tape.extend(pcm)
    c = C.Call({}, send)
    t = time.perf_counter()
    await c.prepare()
    print(f"[prepare {time.perf_counter() - t:.1f}s, before the phone rings] agent: {c.history[0]['content']}")
    await c.say(c.greeting)
    for lang, text in LINES:
        caller = C.speak_pcm(text)
        tape.extend(caller)
        c.mute_until = 0                      # simulation: pretend the greeting has already played
        n = len(out)
        for i in range(0, len(caller), 320):
            await c.feed(caller[i:i + 320])
        t0 = None
        for _ in range(60):                   # 1.2 s of line silence: ends the turn after END_QUIET
            await c.feed(b"\0" * 640)
            if c.busy and t0 is None:
                t0 = time.perf_counter()
        while c.busy and len(out) == n and time.perf_counter() - t0 < 30:   # until the first agent audio appears
            await asyncio.sleep(0.02)
        first = time.perf_counter() - t0
        while c.busy:
            await asyncio.sleep(0.05)
        tm = c.timings[-1] if c.timings else {}
        print(f"\ncaller ({lang}): {text}\n  heard : {tm.get('heard')}\n  agent : {c.history[-1]['content']}")
        print(f"  latency: caller stops -> first agent audio ~ {first + C.END_QUIET:.1f}s "
              f"(asr {tm.get('asr_s')}s, first sentence ready {tm.get('first_audio_s')}s, whole reply {tm.get('total_s')}s)")
        print("  call over:", c.over)
        if c.over:
            break
    print("\nsummary:", json.dumps(agent.summarize(c.c, c.history), ensure_ascii=False, indent=1))
    path = os.getenv("SIM_WAV", "sim_call.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(bytes(tape))
    print("saved", path)

asyncio.run(main())
