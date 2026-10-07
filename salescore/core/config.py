"""Process-wide settings, read once from the environment (.env is loaded by the CLI).
Secrets (API keys, ADMIN_KEY, WA_*, SMTP_*) are read where used, so rotating them needs no code change."""
import json
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///sales.db")
MODEL_DIR = os.getenv("MODEL_DIR", "models")  # trained per-tenant ML artifacts
MEDIA_DIR = os.getenv("MEDIA_DIR", "media")    # campaign posters (generated or uploaded)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")  # how social tools reach /media

# LLM: any OpenAI-compatible Chat Completions endpoint (see ai/llm.py for provider presets)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")  # gemini | openrouter | ollama | custom
LLM_BASE_URL = os.getenv("LLM_BASE_URL")            # overrides the preset
LLM_MODEL = os.getenv("LLM_MODEL")                  # main model: live customer replies, campaign drafts
LLM_MODEL_LITE = os.getenv("LLM_MODEL_LITE")        # cheap model: follow-ups, reorders, first touch, insights
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))
LLM_PRICE_IN_PER_M = float(os.getenv("LLM_PRICE_IN_PER_M", "0"))    # USD per 1M input tokens (cost tracking only)
LLM_PRICE_OUT_PER_M = float(os.getenv("LLM_PRICE_OUT_PER_M", "0"))  # USD per 1M output tokens
# Extra JSON merged into every request, e.g. turn off Qwen thinking on vLLM/SGLang:
# LLM_EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}
LLM_EXTRA_BODY = json.loads(os.getenv("LLM_EXTRA_BODY") or "{}")
# Comma-separated fallback models, tried in order on rate limits/outages, e.g. "qwen/qwen3.8-27b:free,google/gemma-4-26b-a4b-it:free"
LLM_FALLBACK_MODELS = [m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "").split(",") if m.strip()]
AGENT_MAX_TURNS = int(os.getenv("SALES_AGENT_MAX_TURNS", "6"))

WA_API_VERSION = os.getenv("WA_API_VERSION", "v23.0")
