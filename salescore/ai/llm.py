"""Provider-neutral LLM client over the OpenAI-compatible Chat Completions protocol.
Gemini, Ollama, vLLM, LM Studio, Groq, OpenRouter, Together... all speak it: switch with .env, no code change."""
import os
import re
import time
from dataclasses import dataclass, field

import httpx

from ..core import config

PRESETS = {
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY",
               "model": "gemini-3.8-flash", "model_lite": "gemini-3.5-flash-lite"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None,  # open-source models on your own machine
               "model": "qwen3:8b", "model_lite": "qwen3:4b"},
    # Free tier (Sept 2026 picks): 50 requests/day, 1,000/day after buying $10 credits; free providers may train on
    # prompts, so use it for pilots, not real customer data. Fallbacks kick in on rate limits and downtime.
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
                   "model": "qwen/qwen3.8-27b:free", "model_lite": "google/gemma-4-26b-a4b-it:free",
                   # free pools are often congested; this order was checked live on 30 Sep 2026
                   "fallbacks": ["qwen/qwen3.8-27b:free", "google/gemma-4-26b-a4b-it:free", "google/gemma-4-31b-it:free",
                                 "nvidia/nemotron-3-super-120b-a12b:free", "nvidia/nemotron-3-ultra-550b-a55b:free"],
                   "free_only": True,  # refuse any non-':free' model id; OPENROUTER_ALLOW_PAID=1 lifts it
                   "extra_body": {"reasoning": {"effort": "low", "exclude": True},  # Qwen3.8 defaults to xhigh reasoning
                                  "provider": {"max_price": {"prompt": 0, "completion": 0}}}},  # never route to a paid endpoint
    "custom": {"base_url": None, "key_env": "LLM_API_KEY", "model": None, "model_lite": None},  # vLLM, SGLang, Groq...
}
RETRY_STATUS = {429, 500, 502, 503, 504}   # worth retrying the same model
NEXT_MODEL_STATUS = RETRY_STATUS | {403, 404}  # try the next model (403: model restricted, 404: no provider)
THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.S)  # reasoning models may inline their thinking
ATTEMPTS = 2  # per model; then the next fallback model is tried


class LLMUnavailable(RuntimeError):
    """The configured LLM can't be reached or isn't configured."""


class ModelUnavailable(LLMUnavailable):
    """This model is rate-limited or down right now; another model may work."""


@dataclass(frozen=True)
class Settings:
    provider: str
    base_url: str | None
    api_key: str | None
    key_env: str | None
    model: str | None
    model_lite: str | None
    fallbacks: tuple[str, ...] = ()   # tried in order when the chosen model is rate-limited or down
    extra_body: dict = field(default_factory=dict)
    free_only: bool = False


@dataclass
class Reply:
    text: str | None
    tool_calls: list[dict]
    message: dict                  # raw assistant message; append as-is (keeps provider extras like thought signatures)
    model: str
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def cost_usd(self) -> float:
        return (self.usage.get("prompt_tokens", 0) * config.LLM_PRICE_IN_PER_M
                + self.usage.get("completion_tokens", 0) * config.LLM_PRICE_OUT_PER_M) / 1e6


def settings() -> Settings:
    preset = PRESETS.get(config.LLM_PROVIDER, PRESETS["custom"])
    key_env = preset["key_env"]
    model = config.LLM_MODEL or preset["model"]
    fallbacks = config.LLM_FALLBACK_MODELS or preset.get("fallbacks", [])
    return Settings(provider=config.LLM_PROVIDER, base_url=(config.LLM_BASE_URL or preset["base_url"] or "").rstrip("/") or None,
                    api_key=(os.getenv(key_env) if key_env else None) or os.getenv("LLM_API_KEY"), key_env=key_env,
                    model=model, model_lite=config.LLM_MODEL_LITE or preset["model_lite"] or model,
                    fallbacks=tuple(fallbacks), extra_body={**preset.get("extra_body", {}), **config.LLM_EXTRA_BODY},
                    free_only=preset.get("free_only", False) and os.getenv("OPENROUTER_ALLOW_PAID") != "1")


def _checked() -> Settings:
    cfg = settings()
    if not cfg.base_url or not cfg.model:
        raise LLMUnavailable("set LLM_BASE_URL and LLM_MODEL in .env (LLM_PROVIDER=custom)")
    if cfg.key_env and not cfg.api_key:
        raise LLMUnavailable(f"set {cfg.key_env} in .env (provider '{cfg.provider}')")
    if cfg.free_only and (paid := [m for m in (cfg.model, cfg.model_lite, *cfg.fallbacks) if not m.endswith(":free")]):
        raise LLMUnavailable(f"free-only mode: {paid} are not ':free' models (set OPENROUTER_ALLOW_PAID=1 to allow)")
    return cfg


def _headers(cfg: Settings) -> dict:
    return {"Authorization": f"Bearer {cfg.api_key or 'none'}"}


def _post(cfg: Settings, body: dict) -> dict:
    for attempt in range(ATTEMPTS):
        last = attempt == ATTEMPTS - 1
        try:
            r = httpx.post(f"{cfg.base_url}/chat/completions", headers=_headers(cfg), json=body, timeout=config.LLM_TIMEOUT)
        except httpx.TransportError as e:
            if last:
                raise ModelUnavailable(f"cannot reach {cfg.base_url}: {e}") from e
        else:
            if r.status_code < 400:
                return r.json()
            if last or r.status_code not in RETRY_STATUS:
                error = ModelUnavailable if r.status_code in NEXT_MODEL_STATUS else LLMUnavailable
                raise error(f"{cfg.provider} {body['model']} returned {r.status_code}: {r.text[:300]}")
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def chat(messages: list[dict], tools: list[dict] | None = None, lite: bool = False) -> Reply:
    cfg = _checked()
    first = cfg.model_lite if lite else cfg.model
    candidates = [first, *(m for m in cfg.fallbacks if m != first)]
    started = time.perf_counter()
    for i, model in enumerate(candidates):  # client-side fallback: works for every provider
        body = {"model": model, "messages": messages, **cfg.extra_body}
        if tools:
            body |= {"tools": tools, "tool_choice": "auto"}
        try:
            data = _post(cfg, body)
            break
        except ModelUnavailable:
            if i == len(candidates) - 1:
                raise
    msg = data["choices"][0]["message"]
    text = THINK_BLOCK.sub("", msg.get("content") or "") or None  # never send reasoning to a customer
    return Reply(text=text, tool_calls=msg.get("tool_calls") or [], message=msg, model=model,
                 usage=data.get("usage") or {}, seconds=round(time.perf_counter() - started, 3))


def list_models() -> list[str]:
    cfg = _checked()
    r = httpx.get(f"{cfg.base_url}/models", headers=_headers(cfg), timeout=config.LLM_TIMEOUT)
    r.raise_for_status()
    return sorted(m["id"] for m in r.json().get("data", []))
