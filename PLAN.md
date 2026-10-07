# AI Sales Team Platform — Plan & Core

One platform that runs the **whole sales team** (reach, promotion, replies, qualification, quoting,
follow-up, retention, analysis) for **any business** — paint, IoT automation, real estate, factory —
configured per client, never coded per client.

## 1. The unified core logic

Every sales activity, in every industry, is the same loop:

```
 EVENT ─────────► AGENT JOB ─────────► TOOLS ─────────► GATE ─────────► CHANNEL ────► LOG
 message in        reply               search_catalog    consent         WhatsApp      activity
 task due          followup            create_quote      opt-out         email         (feeds ML
 new lead          first_touch         update_contact    quiet hours     console        + analytics)
 campaign          campaign            create_task       frequency cap
 daily             insights / reorder  handoff_to_human  24h window
```

Three rules make it industry-agnostic and safe:

1. **The tenant's data decides, not the code.** Prices, stock, products, stages, qualifying questions,
   tone, languages, limits — all come from the client's **catalog** and **playbook**. Nothing is hard-coded.
2. **The LLM never computes a price or decides legality.** It understands and writes; deterministic code
   (`services/quotes.py`, `services/compliance.py`) prices and gates.
3. **Humans approve what's risky.** Discounts above the auto limit, stock shortfalls, and every campaign
   wait in the approval queue. Any conversation can be handed to a person.

## 2. The agent roster from the plan document, mapped to code

The chain, exactly as in *AI-Sales-Ecosystem-Full-Solution.docx* §4, run by the Orchestrator:

```
Data/CRM → Strategy → Creative → Verification → Human approval → Distribution → Response → Analytics
   ▲                                                                                          │
   └──────────────────────── campaign results feed the next Strategy cycle ────────────────────┘
```

| Agent (document §3) | Layer | What it does here | Code | Status |
|---|---|---|---|---|
| **Data / CRM** | Foundation | Contacts, consent ledger, catalog = fact sheet, CSV imports, every message auto-logged, per-company model training | `services/contacts, catalog, activity` · `analytics/training` · `ml/` | ✅ |
| **Strategy** | Thinking | Ranks plays (reward champions, win back, close hot leads, welcome new leads, clear overstock): audience, consented reach, offer within discount limits, best send hour, expected replies/revenue. Learns reply/conversion rates from past campaigns; cooldown per play. No LLM, $0 | `marketing/strategy` | ✅ |
| **Creative** | Doing | Two A/B variants per campaign + image/video brief, only from real catalog facts | `marketing/creative` | ✅ (image generation: later) |
| **Verification / Compliance** | Doing | Every price in the copy must match the catalog (with stated discount/tax/saving); discount ≤ max; banned claims; required text (e.g. RERA no.); placeholders. Plus consent, opt-out, quiet hours, frequency caps, WhatsApp 24h rule on every send | `marketing/verification` · `services/compliance` | ✅ |
| **Human approval** | Control | Queue: approve at planned time / send now / edit (re-verified) / reject; quotes above limits; hand-offs | `workflows/approvals` · Queue tab | ✅ |
| **Distribution** | Doing | WhatsApp/email broadcast at the planned time, stable A/B split, holdout group, every message tagged with campaign + variant; social/ad posts to a webhook (Postiz, Zapier, Make, n8n) | `marketing/distribution` · `services/channels` | ✅ (native ad APIs: later) |
| **Response** | Doing | Replies in seconds (local models first, LLM when needed), qualifies, quotes, follow-up cadence, reorder nudges, hand-off | `workflows/inbound, followups` · `ai/` | ✅ |
| **Voice** | Doing (later phase) | Calls for engaged leads (IndicConformer ASR + Indic Parler-TTS) | — | ⏳ deferred, as planned |
| **Analytics** | Learning | Per campaign: replies, quotes, sales, revenue within the attribution window, per variant, vs holdout (lift), A/B winner. Plus dashboard, lead scoring, RFM, speed-to-lead, AI cost | `marketing/attribution` · `analytics/metrics, scoring` | ✅ |
| **Orchestrator** | Control | Runs Strategy→Creative→Verification→approval; sends scheduled campaigns every tick; daily: measure results, optionally auto-draft the next best play (`marketing.auto_draft`) | `marketing/orchestrator` · `workflows/scheduler` | ✅ |

The **Sales team** tab in the UI shows this roster live, with each agent's activity over the last 30 days;
the **Marketing** tab shows every campaign's progress through the chain.

Verified live on 30 Sep 2026 (free OpenRouter model): Strategy recommended 4 plays → Creative wrote A/B copy +
video brief for "clear overstock" → Verification caught a number it couldn't prove (fixed a false positive on stated
savings) → approved → 6 sent (3/3 A/B), 2 blocked for no consent → measured → play put on cooldown.

Sales roles covered by these agents: lead intake/SDR (Data/CRM + Response first touch), responder and quoting desk
(Response), follow-up and account management (Response: cadence, reorders), marketing (Strategy → Distribution),
sales manager (approval + insights), analyst (Analytics), compliance officer (Verification), human salesperson (hand-off).

## 3. Code layout

```
salescore/
  core/        config · utils (pure helpers) · playbook (per-tenant settings + defaults) · models
  services/    activity · tasks · contacts · catalog · quotes · compliance · channels   ← deterministic business logic
  ml/          text_index (TF-IDF matcher) · intents (classifier + qty parsing) · store (per-tenant trained "brain")
  ai/          llm (provider-neutral client) · prompts · tools · agent (tool-use loop) · fastpath (local answers)
  analytics/   scoring (pure pandas/sklearn) · metrics (snapshot, real-time scoring, AI usage) · training · team (roster)
  marketing/   audience · strategy · creative · verification · distribution · attribution · orchestrator
  workflows/   inbound · followups · approvals · scheduler                 ← the sales-team jobs
  api/         deps · schemas · routes · webhooks · app                                  ← HTTP only, no logic
  web/         index.html                                                               ← the whole UI, one file
  demo/        playbook.json · catalog.csv · contacts.csv · orders.csv                   ← seeded by make dev
  dev.py       demo seeding + dev server;  __main__.py  CLI (dev · seed · tick)
tests/         conftest (fixtures + fake LLM) · one test file per layer
Makefile       make dev · test · lint · tick · daily · seed · clean
```

Dependency direction is one-way: `api → workflows → marketing → ai/analytics → services → ml → core`.

## 4. Technology & the cost design

**Learn first, answer locally, pay for the LLM only when needed.**

```
customer message
  → trained local models (scikit-learn, per company, ~2 ms, $0)
       intent classifier ─ greeting / thanks / price / stock / human / other
       product matcher   ─ which catalog item (price + stock read live from the DB)
       FAQ matcher       ─ playbook Q&A
       → confident?  answer instantly: price, stock, quote, FAQ, greeting, hand-off
  → otherwise the LLM (Gemini / open-source), with tools
       main model: live replies + campaign drafts · lite model: follow-ups, reorders, first touch, insights
  → LLM down or no key? polite hand-off to a human; nothing is lost
```

Measured on the demo company: **14 of 19 typical messages answered locally, median 1.8 ms, $0**. The 5 sent
to the LLM were exactly the ones that need reasoning (colour advice, a complaint, a discount negotiation,
and two vague product requests where it must ask "which one?").

| Need | Choice | Why |
|---|---|---|
| LLM | Any **OpenAI-compatible** endpoint via `ai/llm.py`: **Gemini** (default: `gemini-3.8-flash` + `gemini-3.5-flash-lite`), **Ollama** open-source models (`qwen3:8b` / `qwen3:4b`), or `custom` (vLLM, LM Studio, Groq, OpenRouter...) | One `.env` change switches provider; no vendor SDK |
| Cost control | Local fast path first; lite model for background jobs; 12-message context window; stable system prompt first (provider-side prompt caching); per-call token + cost log | Most traffic never reaches the LLM |
| Training | `analytics/training.py` builds each company's brain after imports, playbook saves, daily, or `make train` (~0.2 s for the demo) | Real-time paths only *use* trained artifacts |
| Intent classifier | TF-IDF char n-grams + LogisticRegression; seed examples (English + Tamil/Hindi transliterations) + `playbook.intent_examples` | Instant routing, works on typos and Tanglish/Hinglish |
| Catalog + FAQ matching | TF-IDF char n-grams, filler words stripped, thresholds + top-2 margin | Ambiguous queries fall through to the LLM instead of guessing |
| Lead scoring | `HistGradientBoostingClassifier` trained daily on won/lost history (heuristic until 40+ outcomes); applied in real time on every message | Score is always fresh, training cost is once a day |
| Segmentation / retention | pandas RFM + median reorder interval | Explainable to clients |
| Artifacts | joblib files in `MODEL_DIR`, cached in memory, reloaded when retrained | Millisecond access |
| API | FastAPI | Typed, async webhook handling |
| Data | SQLAlchemy 2: SQLite for dev, **PostgreSQL** in production (`DATABASE_URL`) | JSON `attrs` columns let any industry's fields fit |
| Channels | WhatsApp Cloud API (signed webhooks, idempotent), SMTP email, console | Meta direct, no reseller markup |

Tuning per company (playbook `fastpath`): `min_confidence` 0.55, `catalog_min_score` 0.45, `catalog_min_margin`
0.05, `faq_min_score` 0.6. Raise them for fewer, safer local answers; `templates` translate the local replies.

## 5. Run it: one command

Prerequisites: [`uv`](https://docs.astral.sh/uv/) and `make`. `uv` installs Python 3.12 and every library itself.

```bash
make dev
```

This installs all libraries (`uv sync`), creates `.env` from `.env.example`, creates the database, seeds a
demo company (Demo Paints & Co: catalog, 8 leads, order history, FAQ), **trains its local models**, starts the
API and opens the UI in your browser, already logged in. Put `GEMINI_API_KEY` in `.env` (or switch to Ollama)
for LLM replies; routine questions, the dashboard, catalog, imports, analytics and queue work without it.

| Command | What it does |
|---|---|
| `make dev` | everything above: http://127.0.0.1:8000 (UI) and /docs (API). `RELOAD=1` restarts on code changes, `PORT=9000` |
| `make test` | 16 tests, no API key needed (fake LLM) |
| `make lint` | ruff |
| `make tick` / `make daily` | run due follow-ups / retrain + analytics + AI insights (schedule with cron) |
| `make train` | relearn every company's data now (also automatic after imports and playbook saves) |
| `make models` | list the models your configured LLM provider offers |
| `make seed` | create the demo tenant, print its API key |
| `make clean` | delete the local SQLite database and trained models |

Without make: `uv run python -m salescore dev`.

The UI is one file, `salescore/web/index.html` (no framework, no build step), served at `/`. It is built around
the agents: teal marks agent work, marigold marks what needs you.
- **Command centre**: the agent chain with live counts (a ring shows agents active in the last 15 minutes), status
  line, KPIs (incl. instant-answer rate and AI cost), *Needs you* with inline approvals, a live activity feed of what
  each agent did (refreshes every 15 s), Strategy's top plays, the analyst's notes.
- **Ask your team** (copilot, press `/`): plain-language questions and instructions ("What needs me today?", "Draft
  the best campaign"). It answers from live data through tools and can draft campaigns; approving and sending stay
  with you. Endpoint: `POST /v1/copilot`.
- **Needs you**, **Conversations**, **Marketing** (chain pipeline per campaign), **Quotes & orders**, **Agents**
  (roster, 30-day stats, autopilot switches), **Catalog**, **Settings** (playbook).

Production: `uv run uvicorn salescore.api.app:app --host 0.0.0.0` with `DATABASE_URL` pointing at
PostgreSQL (`uv add "psycopg[binary]"`), plus cron for `python -m salescore tick` (every 5 min) and
`tick --daily`. Open-source serving in production: `ollama serve` on a GPU box, `LLM_PROVIDER=ollama`,
`LLM_BASE_URL=http://gpu-box:11434/v1`.

Onboard a client:

```bash
curl -X POST localhost:8000/v1/tenants -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{"name": "Acme Paints", "playbook": {...}}'          # returns api_key
curl -X POST localhost:8000/v1/import/catalog  -H "X-API-Key: $KEY" --data-binary @catalog.csv
curl -X POST localhost:8000/v1/import/contacts -H "X-API-Key: $KEY" --data-binary @leads.csv
curl -X POST localhost:8000/v1/import/orders   -H "X-API-Key: $KEY" --data-binary @orders.csv
curl -X POST localhost:8000/v1/inbound -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"phone": "9876543210", "text": "price for 20L white emulsion?"}'
```

WhatsApp: set `WA_VERIFY_TOKEN`, `WA_APP_SECRET`; per tenant `wa_phone_id`, `wa_token`,
`playbook.channel = "whatsapp"` and `playbook.whatsapp_template` for sends outside the 24h window.
Point Meta's webhook at `/webhooks/whatsapp`.

## 6. Playbooks — same code, three industries

Any key not set falls back to `core/playbook.py:DEFAULTS`. Free-form keys (`business`, `qualify`,
`handoff_rules`, `faq`, `tone`…) go straight to the agent.

```json
{"business": "Paint brand, 400 dealers in Tamil Nadu. Sells to dealers, contractors, homeowners.",
 "languages": ["ta", "en"], "qualify": ["project type", "area in sq ft", "shade", "timeline"],
 "faq": "Coverage: emulsion 120 sq ft/L for 2 coats...",
 "stages": ["new", "qualified", "quoted", "won", "lost"],
 "pricing": {"tax_pct": 18, "max_discount_pct": 8, "auto_approve_discount_pct": 3}}
```
```json
{"business": "IoT access-control integrator for factories.",
 "qualify": ["sites and gates", "headcount", "existing system", "cloud or on-prem", "timeline"],
 "handoff_rules": ["more than 5 gates", "asks for site survey", "tender/RFP"],
 "pricing": {"tax_pct": 18, "max_discount_pct": 10, "auto_approve_discount_pct": 0, "validity_days": 30}}
```
```json
{"business": "Plotted development near the Chennai bypass, RERA TN/29/Layout/1234/2025.",
 "qualify": ["budget", "plot size", "facing", "loan needed", "visit date"],
 "handoff_rules": ["ready to pay token", "wants price negotiation"],
 "compliance": {"quiet_hours": [20, 10], "max_proactive_per_week": 2},
 "pricing": {"max_discount_pct": 2, "auto_approve_discount_pct": 0, "validity_days": 3}}
```

## 7. Roadmap (each phase is sellable on its own)

| Phase | Build | Exit KPI |
|---|---|---|
| **0 ✅ Core (this repo)** | Multi-tenant model, catalog-priced quotes, agent + tools, gate, follow-ups, campaigns, approvals, ML scoring/RFM/reorders, WhatsApp webhook | Tests green; first pilot tenant imports catalog + leads |
| **1 Pilot** | Postgres + Alembic migrations, WhatsApp number live, a manager inbox UI (tasks, approvals, hand-offs), 3 pilot clients (paint, IoT, real estate) | First reply < 1 min; 0 leads untouched after 24h |
| **2 More channels** | Instagram DM, web-chat widget, Meta/Google lead ads, IndiaMART/99acres lead pulls → `/v1/inbound` | All lead sources in one timeline |
| **3 Quote-to-cash** | PDF quotes (Typst), e-sign (DocuSeal), UPI/Razorpay payment links, ERP/Tally stock sync | Quote turnaround in minutes |
| **4 Learn** | Holdout-based lift report per campaign, win-probability-driven follow-up priority, demand forecast | Proven revenue lift vs holdout |
| **5 Voice & field** | Call transcription → CRM facts, inbound voice agent (LiveKit/Pipecat), field-sales PWA | Rep admin time cut |
| **6 Scale** | Postgres row-level security, per-tenant rate/cost caps, Temporal for durable workflows, Setup agent (catalog PDF/website → draft playbook) | New client onboarded in < 1 day |

## 8. Known ceilings (marked `ponytail:` in code)

- TF-IDF matchers (catalog, FAQ, intents) are trained per company and fine to ~50k items; then pgvector + embeddings.
- Training runs inline after imports (~0.2 s demo); move to a background job for very large catalogs.
- Local answers use English templates; set `playbook.templates` per language (Tamil etc.) or other languages fall through to the LLM.
- Campaign segment filters run in Python — fine to ~100k contacts; then SQL.
- Failed sends are logged, not retried.
- One SMTP account from env; per-tenant sender domains later.
- `wa_token` stored in plaintext — encrypt (KMS/pgcrypto) before real clients.
- Phone normalisation is digits-only (+91 default); use `phonenumbers` for multi-country.

## 9. Libraries and models

### AI models
| Where | Model | Cost |
|---|---|---|
| Routine replies: greeting, thanks, price, stock, instant quotes, FAQ, hand-off | **Local scikit-learn models trained per company** (intent classifier, catalog + FAQ matchers) | $0, ~2 ms |
| Lead scoring (every message), RFM segments, reorder prediction | **Local** `HistGradientBoostingClassifier` + pandas | $0 |
| Live customer replies the local models can't handle, campaign drafts | **Gemini `gemini-3.8-flash`** (default) or any open-source model | per token |
| Follow-ups, reorders, first touch, manager insights | **Gemini `gemini-3.5-flash-lite`** (cheapest) | per token |
| Free / private alternative | **Ollama**: `qwen3:8b` (main), `qwen3:4b` (lite), or any tool-calling model you pull | $0 (your hardware) |
| Planning and writing this code | Claude Opus 5.5 in Claude Code (not used by the app at runtime) | — |

All LLM choices are `.env` settings (`LLM_PROVIDER`, `LLM_MODEL`, `LLM_MODEL_LITE`, `LLM_BASE_URL`); `make models`
lists what your provider offers.

### Runtime libraries (installed by `make dev` / `uv sync`, versions from `uv.lock`)
| Library | Version | Used for |
|---|---|---|
| fastapi | 0.142.1 | HTTP API, webhooks, serving the UI |
| starlette | 1.7.0 | (via FastAPI) ASGI toolkit |
| pydantic | 2.13.5 | (via FastAPI) request validation |
| uvicorn | 0.54.0 | ASGI server |
| sqlalchemy | 2.1.1 | ORM; SQLite (dev) / PostgreSQL (prod) |
| httpx | 0.28.1 | LLM calls (OpenAI-compatible protocol) and WhatsApp Cloud API |
| scikit-learn | 1.9.1 | intent classifier, TF-IDF matchers, lead-scoring model |
| joblib | 1.6.0 | saving/loading each company's trained models |
| pandas | 3.0.6 | analytics frames, RFM, reorder intervals |
| numpy / scipy / threadpoolctl | 2.5.3 / 1.18.1 / 3.7.0 | (via pandas / scikit-learn) |
| tzdata | 2026.4 | time-zone data for quiet hours on Windows |

No LLM vendor SDK: one ~100-line client speaks the OpenAI-compatible protocol to every provider.
Standard library only: `csv`, `smtplib`/`email`, `hmac`/`hashlib` (webhook signatures), `zoneinfo`, `argparse`,
`webbrowser`, `.env` loading.

### Dev tools
| Tool | Version | Used for |
|---|---|---|
| uv | any recent | Python 3.12 install, dependency lock, running commands |
| make | any | the one-command workflow |
| pytest | 9.1.1 | 23 tests (fake LLM, no key needed) |
| ruff | latest (fetched on demand by `make lint`) | lint |

### Frontend
Plain HTML + CSS + JavaScript in one file: no libraries, no CDN, no build. Light/dark theme, mobile layout.

### External services (only when enabled)
LLM provider (Gemini API, or none with Ollama) · Meta WhatsApp Cloud API (`channel: whatsapp`) · any SMTP server (`channel: email`).

### Planned, not installed yet (see roadmap)
PostgreSQL + psycopg + Alembic · pgvector + multilingual embeddings (when catalogs pass ~50k items) · Temporal ·
Typst · DocuSeal · Razorpay · LiveKit / Pipecat · Chatwoot.

## 10. Model decision & research (30 Sep 2026)

**Decision: Gemini + Sarvam.** Local scikit-learn models answer routine messages first (free, ~2 ms); the LLM
handles the rest.

| Role | Model | How |
|---|---|---|
| Default LLM (no GPU) | Gemini `gemini-3.8-flash` (main) + `gemini-3.5-flash-lite` (background jobs) | `LLM_PROVIDER=gemini`, `GEMINI_API_KEY` |
| Indian-language-first LLM | [`sarvamai/sarvam-30b`](https://huggingface.co/sarvamai/sarvam-30b): 30B MoE, ~2.4B active, 22 Indian languages + English, tool calling, Apache 2.0, 65K context | Self-host with SGLang; `LLM_PROVIDER=custom`, `LLM_BASE_URL`, `LLM_MODEL=sarvamai/sarvam-30b` |

Pick per company: Gemini for mostly-English customers or no GPU; Sarvam when customers write mostly in
Tamil/Hindi/other Indian languages. Validate with 50–100 of the company's real chats before switching.

**Free option for pilots: OpenRouter free tier** (checked 30 Sep 2026 from OpenRouter's live model list:
464 models, 20 free, 17 free with tool calling). Ranked by Artificial Analysis agentic index:

| Free model | Agentic | Intelligence | Notes |
|---|---|---|---|
| **`qwen/qwen3.8-27b:free`** (main) | **45.8** | 33.7 | Beats paid Gemini 3.8 Flash on agentic (40.2); reasoning defaults to xhigh, so we send `effort: low` |
| `thinkingmachines/inkling:free` | 22.5 | 25.0 | ❌ unusable: 403 "only available on agentic harnesses" |
| `google/gemma-4-26b-a4b-it:free` (lite + last fallback) | n/a | n/a | 3.8B active, fast, reasoning off by default, 140+ languages |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 20.1 | 22.9 | last fallback, slower (55B active) |
| `nvidia/nemotron-3-super-120b-a12b:free` | 1.7 | 12.8 | fallback; answered live when Qwen/Gemma were rate-limited and wrote good campaign copy |
| `openrouter/free`, `stealth/space-bunny-alpha` | — | — | random model / anonymous: not for production |

Use `LLM_PROVIDER=openrouter` + `OPENROUTER_API_KEY`. Free pools are often congested (checked live: Qwen and both Gemmas
returned 429 while Nemotron answered), so the client falls back model by model: Qwen3.8 27B → Gemma 4 26B → Gemma 4 31B →
Nemotron Super → Nemotron Ultra. Every model must end in `:free` and every request carries `max_price: 0`.
Limits: 20 requests/min and 50/day (1,000/day once $10 of credits are bought); one LLM-handled message can take
2–3 requests (tool calls), the local fast path takes none. **Free providers may train on prompts**: fine for demo
data and pilots; use paid Gemini (or self-hosted Sarvam) for real customer data (DPDP). Sarvam is not on
OpenRouter; Gemini there is paid (Flash-Lite $0.30 / $2.50 per 1M tokens in/out).

**Evaluated, not chosen for now**
- [`Qwen/Qwen3.6-35B-A3B`](https://huggingface.co/Qwen/Qwen3.6-35B-A3B): Apache 2.0, 3B active, 262K context,
  strong tool calling (SWE-bench Verified 73.4), multimodal. Covers the text agent fully, but it's not proven
  on Indian languages and thinking is on by default (set `LLM_EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}`).
  Serve: `vllm serve Qwen/Qwen3.6-35B-A3B --enable-auto-tool-choice --tool-call-parser qwen3_coder`.
- [`google/gemma-4-26B-A4B`](https://huggingface.co/google/gemma-4-26B-A4B): ~3.8B active, tool calling, Apache 2.0.
- Lite/self-hosted small: [`Qwen/Qwen3.5-9B`](https://huggingface.co/Qwen/Qwen3.5-9B), `google/gemma-4-E4B-it`.
- Qwen3.8-27B (dense, ~9× compute per token of 35B-A3B) and Qwen3.8-Flash-Next (non-Apache licence) don't fit.

**Other parts of the scenario (later phases)**

| Need | Model |
|---|---|
| Speech-to-text, 22 Indian languages (voice agent) | [`ai4bharat/indic-conformer-600m-multilingual`](https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual) (MIT) |
| Text-to-speech, 20 Indian languages + English | [`ai4bharat/indic-parler-tts`](https://huggingface.co/ai4bharat/indic-parler-tts-pretrained), [`ai4bharat/IndicF5`](https://huggingface.co/ai4bharat/IndicF5) |
| Translation | [`sarvamai/sarvam-translate`](https://huggingface.co/sarvamai/sarvam-translate) |
| Embeddings (catalogs past ~50k items) | Qwen3-Embedding 0.6B / 8B, BGE-M3 |
| Classic ML | scikit-learn (in use); LightGBM (lead scoring at scale); statsforecast (demand forecasting) |
| Marketing image/video generation | not yet researched |

Hardware figures are estimates (Qwen3.6 ~20–24 GB VRAM at 4-bit); none of these open models have been run live yet.
