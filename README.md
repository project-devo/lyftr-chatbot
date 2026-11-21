# LLM Chatbot with Key Failover and Response Caching

React (Vite) frontend · FastAPI backend · SQLite storage · LiteLLM gateway (Google Gemini) with an
ordered API-key pool, automatic failover, a LiteLLM response cache and per-request token tracking.

```
/backend    FastAPI app (app/main.py endpoints, app/llm.py key pool + cache, app/db.py SQLite), pytest suite
/frontend   React chat UI (src/App.jsx, src/api.js)
docker-compose.yml · Makefile · .env.example
```

## 1. Live URLs

| | URL |
|---|---|
| Frontend (Vercel) | https://lyftr-chatbot-phi.vercel.app |
| Backend (Render) | https://lyftr-chatbot-api-kli9.onrender.com (try `/health`) |
| Repository | https://github.com/project-devo/lyftr-chatbot |

> **Render free-tier caveats.** The backend runs on Render's free plan:
> - It **sleeps after ~15 min idle**; the first request can take ~30-60 s. Mitigations: the frontend calls
>   `GET /health` with retries on page load and shows a "Waking up the server…" banner, POST `/chat` is retried on
>   network errors / 502 / 504, and an external uptime monitor pings `/health` every 5 minutes.
> - The free filesystem is **not persistent across redeploys/restarts**, so on Render the SQLite history and the
>   disk cache reset when the service is redeployed. The persistence requirement is met locally by the Docker
>   **named volume** (`chat-data` mounted at `/data`), which survives `docker compose restart` / `down` / `up`.

## 2. Run locally: with Docker and without

**Prerequisites:** a `.env` in the repo root. Copy `.env.example` and put at least two Gemini keys in
`LLM_API_KEYS` (`key_1`, `key_2`, ... in that order). Every setting is an environment variable:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_API_KEYS` | (required) | Comma-separated key pool, at least 2 |
| `LLM_MODEL` | `gemini/gemini-3.5-flash-lite` | LiteLLM model name |
| `DATABASE_URL` | `sqlite:////data/app.db` (Docker) | SQLite file |
| `CACHE_DIR` | `/data/litellm-cache` (Docker) | LiteLLM disk-cache directory |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated CORS origins (no trailing slash) |
| `ENABLE_FAILURE_SIMULATION` | `false` | `true` honours the `X-Simulate-Failure` header |
| `DEFAULT_SYSTEM_PROMPT` | `You are a helpful, concise assistant.` | Used when a session has no system prompt |
| `VITE_API_URL` | `http://localhost:8000` | Frontend build-time backend URL |

**Model choice:** the spec's default `gemini/gemini-2.0-flash` (and `gemini-2.5-flash`) now return
`404 ... no longer available`. The default is `gemini/gemini-3.5-flash-lite`: it answers in about 1 s, while
`gemini-3.5-flash` took about 50 s per reply because of thinking. Set `LLM_MODEL` to change it.

### With Docker

```bash
make up      # docker compose up --build -d   -> backend http://localhost:8000, frontend http://localhost:3000
make logs    # follow logs
make test    # backend pytest inside a Docker test stage (no local Python needed)
make down    # stop (the chat-data volume, and so the history, is kept)
```

The SQLite DB and the LiteLLM cache live on the named volume `chat-data` at `/data`, so
`docker compose restart backend` keeps every session. `VITE_API_URL` is passed to the frontend build as a build arg.

### Without Docker

Backend (Python 3.11+):

```bash
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt          # Windows: .venv\Scripts\pip ...
.venv/bin/python -m pytest -q                          # 30 tests, LLM mocked
# run (uvicorn reads ../.env; the two vars below override the Docker paths)
DATABASE_URL=sqlite:///./app.db CACHE_DIR=.cache/litellm LITELLM_LOCAL_MODEL_COST_MAP=True \
  .venv/bin/uvicorn app.main:app --env-file ../.env --port 8000
```

Frontend (Node 20+):

```bash
cd frontend
npm ci
npm run dev      # http://localhost:3000, talks to VITE_API_URL (default http://localhost:8000)
npm run build    # production bundle in dist/
```

## 3. Design decisions: key fallback, caching, SQLite session storage

### Key fallback (`backend/app/llm.py`)

- `LLM_API_KEYS` is split into an ordered pool; key ids are `key_1`, `key_2`, ... The key value is never logged
  or returned.
- `complete()` loops over the keys **itself** and calls `litellm.completion(model, messages, api_key=key)` once per
  attempt, so `key_id` and `fallback_attempts` are exact (LiteLLM's Router would hide which key answered).
- **Try the next key** on: `RateLimitError` (429), quota / `RESOURCE_EXHAUSTED` errors, `ServiceUnavailableError`,
  `BadGatewayError`, `InternalServerError`, any other 5xx, `APIConnectionError` and `Timeout` (30 s per attempt).
  Invalid or revoked keys (401/403) are also treated as per-key failures, so one bad key cannot take the service down.
- **Don't retry** non-retryable errors (for example `BadRequestError`). They return a clean `400 {"detail": ...}`
  and nothing is stored.
- If every key fails, the response is `503 {"detail": "all providers unavailable"}`.
- The user message and the reply are written **only after** the LLM call succeeds, in **one transaction**. A
  422 or 503 never adds anything to the history.
- Every error is JSON with `detail`: FastAPI 422s, `HTTPException`s, and a catch-all handler that returns
  `500 {"detail": "internal error"}`, so no stack trace ever reaches the client.

**Failover test hook:** `X-Simulate-Failure: key_1=429` / `key_1=429,key_2=500` / `*=429` (statuses 429, 500, 502,
503). It is honoured only when `ENABLE_FAILURE_SIMULATION=true`. A malformed header returns 422. The simulated
failure raises the matching LiteLLM exception (`RateLimitError`, `InternalServerError`, `BadGatewayError`,
`ServiceUnavailableError`), so it goes through exactly the same failover code as a real provider error.

**Exact per-attempt ordering** (for each key in order):

1. **Simulated failure check.** If `X-Simulate-Failure` lists this key (or `*`), raise the simulated error and
   go to the next key. This happens *before* the cache lookup, so `*=429` returns 503 even for a cached prompt.
2. **LiteLLM cache lookup.** If a hit, return it (`LLM_CACHE`, `key_id: null`, `fallback_attempts: 0`). Once an
   earlier key has failed in the same request, later attempts **skip the cache read**
   (`cache={"no-cache": True}`). Failover is therefore never masked by the cache, and `key_2` with
   `fallback_attempts: 1` is reported even for a prompt that was cached before. For real provider errors this
   changes nothing, because the first attempt has already missed the cache.
3. **Provider call** with this key. On success LiteLLM stores the response in the cache.

### Caching (LiteLLM built-in)

- `litellm.cache = litellm.Cache(type="disk", disk_cache_dir=CACHE_DIR)`, a `diskcache` store on the `/data`
  volume, so it also survives restarts.
- **Cache key = model + the full message list** (system prompt + history + new message), which is LiteLLM's
  default key built from the OpenAI-style request params. `api_key` is a LiteLLM-owned kwarg and is **not** part
  of the key, so an answer fetched via `key_2` is a hit when retried via `key_1`. This is verified by the test
  `test_cache_key_ignores_api_key` and against the real API.
- A hit is detected from `response._hidden_params["cache_hit"]` and reported as `LLM_CACHE`. A hit returns the
  identical reply text and the usage stored with the cached response, with `key_id: null` and
  `fallback_attempts: 0`. A different message, or the same message later in a conversation (different history),
  is a different key, so it never comes from the cache.
- Measured locally: live call about 900-1100 ms, cache hit about 3-6 ms.
- `litellm==1.100.0` is pinned: 1.106/1.108 crash on cache hits (they import proxy-only modules such as
  `soundfile`). `orjson` is required on the cache-hit path.

### SQLite session storage (`backend/app/db.py`)

- Uses stdlib `sqlite3` with one short-lived connection per operation. The path comes from `DATABASE_URL`
  (`sqlite:////data/app.db` maps to `/data/app.db`), and the directory and schema are created on startup.
- `messages(id, session_id, role, content, metadata JSON, created_at ISO-8601)` with an index on
  `(session_id, id)`, plus `sessions(session_id PK, system_prompt, created_at)`, so a session's system prompt
  persists across turns. A request with a non-empty `system_prompt` sets or overrides it.
- The client sends only the new message. The backend rebuilds `[system] + history + new message` from SQLite
  for every request.
- `GET /sessions/{id}/messages` returns history oldest first, without the system prompt. Assistant messages
  carry their full metadata.

## 4. Verification: how to see failover, cache hits and per-message token usage

```bash
B=http://localhost:8000          # or the live backend URL
# Windows PowerShell: use curl.exe and the same arguments

# Health
curl -s $B/health
# -> {"status":"ok","keys_configured":2,"cache_enabled":true}

# Live answer (note resolution_source, key_id, latency_ms, input/output tokens)
curl -s -X POST $B/chat -H "Content-Type: application/json" \
  -d '{"session_id":"s-demo-1","message":"What is the capital of France?"}'

# Cache hit: same first message in a NEW session -> LLM_CACHE, identical reply, key_id null, a few ms
curl -s -X POST $B/chat -H "Content-Type: application/json" \
  -d '{"session_id":"s-demo-2","message":"What is the capital of France?"}'

# Failover: key_1 "returns" 429 -> answered by key_2, fallback_attempts 1 (also try key_1=500)
curl -s -X POST $B/chat -H "Content-Type: application/json" -H "X-Simulate-Failure: key_1=429" \
  -d '{"session_id":"s-demo-3","message":"Name three primary colors."}'

# All keys fail -> 503 {"detail":"all providers unavailable"}, nothing written
curl -s -i -X POST $B/chat -H "Content-Type: application/json" -H "X-Simulate-Failure: *=429" \
  -d '{"session_id":"s-demo-3","message":"This will fail"}'

# Multi-turn memory + history (oldest first, per-message metadata incl. tokens)
curl -s -X POST $B/chat -H "Content-Type: application/json" -d '{"session_id":"s-demo-4","message":"My name is Priyank"}'
curl -s -X POST $B/chat -H "Content-Type: application/json" -d '{"session_id":"s-demo-4","message":"What is my name?"}'
curl -s $B/sessions/s-demo-4/messages

# Validation / not found
curl -s -i -X POST $B/chat -H "Content-Type: application/json" -d '{"session_id":"s-x","message":"   "}'   # 422
curl -s -i $B/sessions/unknown/messages                                                                  # 404
```

The token counts are **per request**: `input_tokens` is the prompt of that call (system + history + new message),
so it grows as a conversation gets longer, and `output_tokens` is that reply only.

**In the UI** (http://localhost:3000), every assistant reply shows `meta-source` (green `LIVE_INFERENCE` /
blue `LLM_CACHE`), `meta-model`, `meta-latency`, `meta-input-tokens`, `meta-output-tokens`, plus key id and
fallback count. The **Failure simulation** dropdown sends `X-Simulate-Failure`, so failover (`key_1 → 429`) and
a readable 503 (`all keys → 429`) can be shown without curl. To see a cache hit, click **New chat** and resend
the same first message.

Automated: `make test` runs the 30 backend tests with the LLM mocked through LiteLLM's `mock_response`, so the
real LiteLLM cache and the failover loop are exercised. They cover the 422 cases, 404, health, cache-hit
labelling, cache-key independence from the API key, failover on 429/500/502/503 (simulated and "real" provider
errors), `*=429` returning 503 with no history written, non-retryable errors, history order and separation
between sessions, per-request tokens, system-prompt persistence, and the JSON 500 handler.

## 5. Setup Used

- **AI tools:** Claude Code (Claude Opus 5.5) for implementation, testing, Docker verification and deployment
  through the Render and Vercel MCP connectors.
- **Editor:** VS Code / Antigravity.
- **Local:** Windows 11, Docker Desktop, GNU make (ezwinports), Python 3.11, Node 20+.

## 6. Bonus features (if any)

- **Failure-simulation dropdown in the UI.** It demos failover and the readable 503 path in the browser,
  without curl.
- **Key id and fallback count shown on each reply.** Failover is visible next to the answer.
- **Cold-start handling.** `/health` warm-up with a "Waking up the server…" banner, and POST retries only on
  network errors / 502 / 504 (never 422/503).
- **Per-key failover on invalid/revoked keys (401/403).** A broken key degrades to the next one instead of
  failing requests.
- **The cache survives restarts.** The LiteLLM disk cache lives on the same `/data` volume as SQLite.
- **`make test` runs in a Docker test stage.** The suite runs anywhere Docker runs.
- **Non-root runtime container**, plus a light/dark theme that follows the OS setting.
