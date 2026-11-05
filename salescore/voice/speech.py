"""Open-source speech for the demo, all on CPU: faster-whisper (ASR, Telugu + English) and Piper (TTS).

Production upgrade path: ai4bharat/indic-conformer-600m (ASR) and Indic Parler-TTS / IndicF5 (TTS) on a GPU."""

import io

import os
import time

import re

import wave

from pathlib import Path



CACHE = Path(os.getenv("VOICE_CACHE", Path(__file__).parent / "models"))

ASR_MODEL = os.getenv("VOICE_ASR_MODEL", "small")   # small (fast) | medium (better Telugu, ~3x slower)

HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

VOICES = {"te": "te/te_IN/maya/medium/te_IN-maya-medium", "en": "en/en_US/amy/medium/en_US-amy-medium"}

TELUGU = re.compile(r"[ఀ-౿]")

_asr = None

_tts: dict = {}





def lang_of(text: str) -> str:

    return "te" if TELUGU.search(text) or re.search(r"telugu", text, re.I) else "en"   # writing Telugu, or asking for it





def _wav16k(audio) -> bytes:

    import numpy as np

    out = io.BytesIO()

    with wave.open(out, "wb") as w:

        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)

        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())

    return out.getvalue()





def transcribe_groq(audio, hint: str = "auto") -> tuple[str, str]:

    """Hosted open-source Whisper large-v3-turbo, ~0.3 s, free tier. Only Telugu and English count: if Whisper guesses any other

    language (it does on noisy or accented audio) or returns letters neither language uses, both are tried and the likelier one wins."""

    import httpx

    key, base = os.environ["LLM_API_KEY"], os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")



    def ask(lang):

        r = httpx.post(f"{base}/audio/transcriptions", headers={"Authorization": f"Bearer {key}"}, timeout=20,

                       files={"file": ("a.webm", audio, "audio/webm") if isinstance(audio, bytes) else ("a.wav", _wav16k(audio), "audio/wav")},   # bytes: a browser recording as it is

                       data={"model": "whisper-large-v3-turbo", "response_format": "verbose_json", "temperature": "0",

                             **({"language": lang} if lang else {})})

        r.raise_for_status()

        j = r.json()

        segs = j.get("segments") or [{"avg_logprob": -9}]

        got = {"telugu": "te", "english": "en"}.get(j.get("language", "").lower(), j.get("language", "").lower())

        return j.get("text", "").strip(), got, sum(x.get("avg_logprob", -9) for x in segs) / len(segs)

    if hint in ("te", "en"):
        text, lang, _ = ask(hint)
    else:   # Whisper's own guess is unreliable (Telugu came back as Tamil, or as English letters): read it both ways, keep the likelier
        text, lang, _ = _best(ask)
    print(f"[asr] hint={hint} -> {lang} {text[:60]!r}", flush=True)   # shows in the make dev terminal
    return text, lang or "en"





def _plain(text: str) -> bool:
    """Only English or Telugu letters (and usual punctuation): no Icelandic, French or other stray characters."""
    return all(ord(c) < 128 or "ఀ" <= c <= "౿" or c in "‘’“”…₹" for c in text)


def _try(ask, lang):
    try:
        return ask(lang)
    except Exception:   # e.g. a rate limit on one of the two calls: the other still answers
        return "", lang, -9.0


def _best(ask):

    """Both languages forced; keep the one Whisper is more confident in."""

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:   # both at once: one round trip instead of two
        t, e = ex.map(lambda l: _try(ask, l), ("te", "en"))

    best = max((t, e), key=lambda x: x[2])

    return best[0], lang_of(best[0]), best[2]   # label by the script that came back, not by which call won





def transcribe(path, hint: str = "auto") -> tuple[str, str]:

    """-> (text, language 'te'|'en'). path: a file, or a 16 kHz float32 numpy array. VOICE_ASR=groq uses the hosted model."""

    if os.getenv("VOICE_ASR") == "groq" and not isinstance(path, str):

        return transcribe_groq(path, hint)

    global _asr

    if _asr is None:

        from faster_whisper import WhisperModel

        _asr = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8", download_root=str(CACHE / "asr"))

    lang = None if hint == "auto" else hint

    segs, info = _asr.transcribe(path, language=lang, beam_size=1, vad_filter=True, condition_on_previous_text=False)

    text = " ".join(s.text.strip() for s in segs).strip()

    if lang is None and info.language not in ("te", "en"):   # only two languages matter here; pick the likelier one

        probs = dict(info.all_language_probs or [])

        lang = "te" if probs.get("te", 0) > probs.get("en", 0) else "en"

        segs, info = _asr.transcribe(path, language=lang, beam_size=1, vad_filter=True, condition_on_previous_text=False)

        text = " ".join(s.text.strip() for s in segs).strip()

    return text, info.language





def _voice(lang: str):

    if lang not in _tts:

        from piper import PiperVoice

        import httpx

        base = VOICES[lang]

        model = CACHE / "tts" / Path(base).name

        model.parent.mkdir(parents=True, exist_ok=True)

        for ext in (".onnx", ".onnx.json"):

            f = model.with_name(model.name + ext)

            if not f.exists():

                r = httpx.get(f"{HF}/{base}{ext}", follow_redirects=True, timeout=300)

                r.raise_for_status()

                f.write_bytes(r.content)

        _tts[lang] = PiperVoice.load(str(model.with_name(model.name + ".onnx")))

    return _tts[lang]





_gem_off: dict = {}   # model -> time until which it is skipped after it failed (free-tier quota is ~3-4 requests a minute PER MODEL)
GEM_MODELS = "gemini-3.8-flash-lite-tts,gemini-3.8-flash-tts,gemini-3.1-flash-tts-preview"


def _gemini(text: str) -> bytes | None:
    """Gemini speech (free tier key in .env.voice): natural Telugu, English and the mix of both in one voice. WAV, or None to use Piper.
    Each model has its own quota, so they are tried in turn; streamed and collected, a short sentence is done in ~1-2 s.
    ponytail: ~10 requests a minute in total on the free tier; beyond that, or on any error, Piper speaks (the voice changes)."""
    key = os.getenv("GEMINI_API_KEY")
    if not key or os.getenv("GEMINI_TTS") != "on":   # opt-in: the free tier allows only a few requests per DAY per model
        return None
    import base64, json
    import httpx
    for model in os.getenv("GEMINI_TTS_MODELS", GEM_MODELS).split(","):
        if time.monotonic() < _gem_off.get(model, 0):
            continue
        try:
            pcm = []
            with httpx.stream("POST", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse",
                              headers={"x-goog-api-key": key}, timeout=httpx.Timeout(8, read=8),
                              json={"contents": [{"parts": [{"text": text}]}],
                                    "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": os.getenv("GEMINI_VOICE", "Sulafat")}}}}}) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        for part in json.loads(line[5:])["candidates"][0]["content"].get("parts", []):
                            if part.get("inlineData"):
                                pcm.append(base64.b64decode(part["inlineData"]["data"]))
            if not pcm:
                raise ValueError("no audio")
        except Exception as e:
            _gem_off[model] = time.monotonic() + 60
            print(f"[tts] {model} unavailable ({type(e).__name__}), next one", flush=True)
            continue
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(b"".join(pcm) + bytes(int(24000 * 0.15) * 2))
        return out.getvalue()
    return None


_cloud_off = 0.0


def _cloud(text: str) -> bytes | None:
    """Google Cloud Text-to-Speech (own key GOOGLE_TTS_KEY in .env.voice): the proper lively Indian English and Telugu voices, with pace and pitch.
    WAV, or None to try the next voice. Off until the key is set. GOOGLE_TTS_RATE (default 1.0 = medium) and GOOGLE_TTS_PITCH (default 1.5, a little brighter)
    tune the tone; GOOGLE_TTS_VOICE_EN / _TE pick the voices.
    ponytail: untested against the live API until the key exists; any failure (API not enabled, quota) pauses it for 5 minutes."""
    global _cloud_off
    key = os.getenv("GOOGLE_TTS_KEY")
    if not key or time.monotonic() < _cloud_off:
        return None
    import base64
    import httpx
    voice = {"en": ("en-IN", os.getenv("GOOGLE_TTS_VOICE_EN", "en-IN-Neural2-A")), "te": ("te-IN", os.getenv("GOOGLE_TTS_VOICE_TE", "te-IN-Standard-A"))}
    try:
        pcm = []
        for sentence in (x for x in re.split(r"(?<=[.!?।])\s+", text) if x.strip()):
            for run in (r for r in re.findall(r"[ఀ-౿][ఀ-౿\s,]*|[^ఀ-౿]+", sentence) if re.search(r"\w", r)):
                code, name = voice[lang_of(run)]
                r = httpx.post("https://texttospeech.googleapis.com/v1/text:synthesize", params={"key": key}, timeout=10,
                               json={"input": {"text": run.strip()}, "voice": {"languageCode": code, "name": name},
                                     "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000,
                                                     "speakingRate": float(os.getenv("GOOGLE_TTS_RATE", "1.0")), "pitch": float(os.getenv("GOOGLE_TTS_PITCH", "1.5"))}})
                r.raise_for_status()
                with wave.open(io.BytesIO(base64.b64decode(r.json()["audioContent"]))) as w:
                    pcm.append(w.readframes(w.getnframes()) + bytes(int(24000 * 0.05) * 2))
            pcm.append(bytes(int(24000 * 0.2) * 2))
    except Exception as e:
        _cloud_off = time.monotonic() + 300
        print(f"[tts] Google Cloud voice unavailable ({type(e).__name__}), next voice for 5 minutes", flush=True)
        return None
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"".join(pcm))
    return out.getvalue()


_goog_off = 0.0


def _google(text: str) -> bytes | None:
    """Google Translate's voice (no key): Indian English accent (google.co.in) for English, Telugu for Telugu, each run of words in its own
    voice. WAV, or None to use Piper. ponytail: an unofficial endpoint (fine for demos, no guarantee); Google Cloud Text-to-Speech
    (en-IN, te-IN voices, own key) is the supported version of the same thing."""
    global _goog_off
    if os.getenv("GOOGLE_TTS", "on") == "off" or time.monotonic() < _goog_off:
        return None
    import httpx, miniaudio
    try:
        pcm = []
        for sentence in (x for x in re.split(r"(?<=[.!?।])\s+", text) if x.strip()):
            for run in (r for r in re.findall(r"[ఀ-౿][ఀ-౿\s,]*|[^ఀ-౿]+", sentence) if re.search(r"\w", r)):
                r = httpx.get("https://translate.google.co.in/translate_tts", timeout=8, headers={"User-Agent": "Mozilla/5.0"},
                              params={"ie": "UTF-8", "q": run.strip()[:190], "tl": lang_of(run), "client": "tw-ob"})
                r.raise_for_status()
                pcm.append(miniaudio.decode(r.content, miniaudio.SampleFormat.SIGNED16, 1, 24000).samples.tobytes())
                pcm.append(bytes(int(24000 * 0.06) * 2))
            pcm.append(bytes(int(24000 * 0.2) * 2))
    except Exception as e:
        _goog_off = time.monotonic() + 30
        print(f"[tts] Google voice unavailable ({type(e).__name__}), using the local voice for 30 s", flush=True)
        return None
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2)
        w.setframerate(int(24000 * float(os.getenv("GOOGLE_TTS_SPEED", "1.15"))))   # played faster: quicker and brighter, the free voice is slow and flat
        w.writeframes(b"".join(pcm))
    return out.getvalue()


def speak(text: str) -> bytes:

    """Text -> WAV bytes. Each sentence uses the voice of its own script, so mixed Telugu/English replies sound right."""

    text = text.replace(" ", " ").replace("‌", "").replace("‑", "-")   # invisible characters models like to emit
    if wav := _cloud(text) or _gemini(text) or _google(text):
        return wav

    pcm, rate = [], 22050

    for sentence in (x for x in re.split(r"(?<=[.!?।])\s+", text) if x.strip()):

        runs = re.findall(r"[ఀ-౿][ఀ-౿\s,]*|[^ఀ-౿]+", sentence)   # Telugu words and English words (GYC, AI, brand names) each get their own voice

        for i, run in enumerate(r for r in runs if re.search(r"\w", r)):

            buf = io.BytesIO()

            with wave.open(buf, "wb") as w:

                _voice(lang_of(run)).synthesize_wav(run, w)

            buf.seek(0)

            with wave.open(buf) as w:

                rate = w.getframerate()

                pcm.append(w.readframes(w.getnframes()) + bytes(int(rate * 0.05) * 2))

        pcm.append(bytes(int(rate * 0.25) * 2))   # short pause between sentences

    out = io.BytesIO()

    with wave.open(out, "wb") as w:

        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)

        w.writeframes(b"".join(pcm))

    return out.getvalue()

