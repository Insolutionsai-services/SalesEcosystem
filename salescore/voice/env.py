"""Loads .env.voice (Groq key, phone provider) into the environment. Imported first by the voice service only, so the
main app's own LLM settings are never touched. Variables already set win (dev passes VOICE_KEY this way)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_f = ROOT / ".env.voice"
for _line in (_f.read_text(encoding="utf-8").splitlines() if _f.is_file() else []):
    _k, _, _v = _line.strip().partition("=")
    if _k and not _k.startswith("#") and _v.strip().strip("\"'"):
        os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))
