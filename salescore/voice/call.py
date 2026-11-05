"""One live phone call: caller audio in -> silence detection -> ASR -> LLM (streamed) -> TTS per sentence -> audio out.
Audio is 8 kHz 16-bit mono PCM both ways. Half-duplex on purpose.
ponytail: no barge-in (caller can't interrupt mid-sentence), no echo cancellation; add when callers talk over the agent."""
import asyncio
import audioop
import io
import os
import re
import time
import wave

from . import agent, speech

VAD_RMS = int(os.getenv("VOICE_VAD_RMS", 500))   # calibration knob: raise if line noise triggers it, lower if quiet callers are missed
END_QUIET = float(os.getenv("VOICE_END_QUIET", 0.7))   # seconds of silence that end the caller's turn
MIN_SPEECH, MAX_UTT, TAIL, FILLER_AFTER = 0.25, 15.0, 0.4, 3.0
BPS = 16000      # bytes per second of 8 kHz PCM16
CHUNK = 3200     # 200 ms per frame sent; providers want multiples of 320 bytes
FILLER = {"te": "ఒక్క నిమిషం.", "en": "One moment please."}   # spoken if the model is slow: never leave dead air
SILENCE_S = float(os.getenv("VOICE_SILENCE_S", 10))   # the caller says nothing for this long: ask "are you there?" once, then end the call
RING_S = 30           # how long the provider lets the phone ring (place() sends the same number)
WRAP_BEFORE_S = 25    # this many seconds before the maximum length, the agent stops asking and closes
NUDGE, BYE = "Hello, are you there?", "Thank you for your time."
MACHINE = re.compile(r"leave (a|your) message|after the (tone|beep)|voice ?mail|not available|cannot take your call|can't take your call|"
                     r"unable to take your call|please record|mailbox|subscriber|switched off|not reachable|press \d", re.I)   # an answering machine, not a person


def speak_pcm(text: str) -> bytes:
    with wave.open(io.BytesIO(speech.speak(text))) as w:
        return audioop.ratecv(w.readframes(w.getnframes()), 2, 1, w.getframerate(), 8000, None)[0]


class Call:
    def __init__(self, config: dict, send=None):
        self.c = {**agent.DEFAULT, **config}
        self.c["questions"] = [q.strip() for q in self.c["questions"] if q.strip()]
        self.send, self.history, self.timings = send, [], []   # send(pcm) is an async callable set by the phone adapter
        self.buf, self.voiced, self.quiet = bytearray(), 0.0, 0.0
        self.mute_until, self.busy, self.over = 0.0, False, False
        self.to = ""            # the number being phoned, for the CRM record
        self.created, self.started = time.monotonic(), None   # started: when the caller picked up (the stream opened)
        self.max_s = int(float(self.c.get("max_call_min") or 3) * 60)   # the longest a call may last
        self.last_voice, self.nudged, self.outcome = 0.0, False, None   # outcome: set when the call is cut short
        self.greeting = b""
        self.fillers: dict[str, bytes] = {}

    async def prepare(self):
        """Before dialing: write and voice the opening line, so the caller hears it the moment they pick up."""
        text, done = await asyncio.to_thread(agent.turn, self.c, [])
        self.history = [{"role": "assistant", "content": text}]
        self.greeting = await asyncio.to_thread(speak_pcm, text)
        for lang, line in FILLER.items():
            self.fillers[lang] = await asyncio.to_thread(speak_pcm, line)
        self.nudge, self.bye = await asyncio.to_thread(speak_pcm, NUDGE), await asyncio.to_thread(speak_pcm, BYE)
        self.over = done

    def start(self):
        self.started = self.last_voice = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.started else 0.0

    def expired(self) -> bool:
        """Past the maximum length (plus a few seconds for the goodbye to finish): the call is cut, whatever is being said."""
        return bool(self.started) and self.elapsed() > self.max_s + 8

    async def say(self, pcm: bytes):
        self.mute_until = max(self.mute_until, time.monotonic()) + len(pcm) / BPS   # we are "talking" until this audio has played
        for i in range(0, len(pcm), CHUNK):
            await self.send(pcm[i:i + CHUNK])

    def _reset(self):
        self.buf, self.voiced, self.quiet = bytearray(), 0.0, 0.0

    async def feed(self, frame: bytes):
        """Caller audio, any frame size. Ignored while the agent is talking or thinking."""
        if self.over or not frame:
            return
        if self.busy or time.monotonic() < self.mute_until + TAIL:
            return self._reset()
        sec, now = len(frame) / BPS, time.monotonic()
        if audioop.rms(frame, 2) > VAD_RMS:
            self.voiced, self.quiet, self.last_voice, self.nudged = self.voiced + sec, 0.0, now, False
            self.buf += frame
        elif not self.voiced and now - max(self.last_voice, self.mute_until) > SILENCE_S:   # nobody is speaking: nudge once, then end
            self.last_voice = now
            if self.nudged:
                self.over, self.outcome = True, "no_response"
                return asyncio.create_task(self.say(self.bye))
            self.nudged = True
            return asyncio.create_task(self.say(self.nudge))
        elif self.voiced:
            self.quiet += sec
            self.buf += frame
        if self.voiced and (self.quiet >= END_QUIET or len(self.buf) / BPS >= MAX_UTT):
            pcm, voiced = bytes(self.buf), self.voiced
            self._reset()
            if voiced >= MIN_SPEECH:
                self.busy = True
                asyncio.create_task(self.respond(pcm))

    async def _filler(self, lang: str):
        await asyncio.sleep(FILLER_AFTER)
        if lang in self.fillers:
            await self.say(self.fillers[lang])

    async def respond(self, pcm: bytes):
        t0 = time.perf_counter()
        filler = None
        try:
            import numpy as np
            wide = audioop.ratecv(pcm, 2, 1, 8000, 16000, None)[0]
            heard, lang = await asyncio.to_thread(speech.transcribe, np.frombuffer(wide, np.int16).astype(np.float32) / 32768,
                                                  self.c["language"])
            if not heard.strip():
                return
            if MACHINE.search(heard):   # a voicemail greeting: do not talk to a machine; end quietly
                self.over = True
                self.outcome = "voicemail"
                return
            t_asr = time.perf_counter() - t0
            self.history.append({"role": "user", "content": heard})
            if self.started and self.elapsed() > self.max_s - WRAP_BEFORE_S:
                self.c["wrap_up"] = True   # nearly out of time: the next reply closes the call instead of asking more
            filler = asyncio.create_task(self._filler(lang))
            q, loop, first, reply = asyncio.Queue(), asyncio.get_running_loop(), None, ""

            def produce():
                for s in agent.stream_turn(self.c, self.history, lang):
                    loop.call_soon_threadsafe(q.put_nowait, s)
                loop.call_soon_threadsafe(q.put_nowait, None)
            asyncio.create_task(asyncio.to_thread(produce))
            while (s := await q.get()) is not None:
                if first is None:
                    first = time.perf_counter() - t0
                    filler.cancel()
                if agent.END in s:
                    self.over = True
                s = s.replace(agent.END, "").strip()
                if s:
                    reply += " " + s
                    await self.say(await asyncio.to_thread(speak_pcm, s))
            if reply.strip().endswith(("?", "？")):
                self.over = False                      # an unanswered question is never the end of the call, whatever the model says
            self.history.append({"role": "assistant", "content": reply.strip()})
            self.timings.append({"heard": heard, "asr_s": round(t_asr, 2), "first_audio_s": round(first or 0, 2),
                                 "total_s": round(time.perf_counter() - t0, 2)})
        finally:
            if filler:
                filler.cancel()
            self.busy = False

    def finished(self) -> bool:
        """Over and the goodbye has been heard; or past the maximum length; or never answered (ring time + maximum length + a minute)."""
        t = time.monotonic()
        return (self.over and t > self.mute_until + 1) or self.expired() or (self.started is None and t > self.created + RING_S + 15)
