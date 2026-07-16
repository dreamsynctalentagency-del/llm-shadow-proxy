# LLM Shadow Proxy — Demo Flow & Talking Track

> Companion to [`Architecture_Doc.md`](./Architecture_Doc.md). This is the talking script
> for a 5–7 minute walkthrough plus a Q&A prep sheet.

- Repo root: `/workspaces/llm-shadow-proxy`
- Local URL: `http://localhost:8000` (UI mounted at `/`, redirects to `/ui/`)
- Prod: DO App Platform, spec at `.do/app.yaml`

---

## 0. One-sentence pitch

> "It's a FastAPI proxy that serves customer traffic through a primary LLM in real time,
> mirrors every request to a candidate LLM off the hot path, evaluates the two responses
> with deterministic heuristics, and gives ops a live knob to dial the mirroring
> percentage from 100% down to 0% without redeploying."

---

## 1. What we built (30-second overview)

| Concern | Chosen |
| --- | --- |
| Language / framework | Python 3.13 + FastAPI (async-native) |
| Primary model | `openai-gpt-oss-120b` on DO Serverless Inference |
| Candidate model | `openai-gpt-oss-20b` on DO Serverless Inference |
| Comparison store | SQLAlchemy 2.0 async → SQLite (dev) / Postgres (prod) |
| Raw payload store | DO Spaces (`llm-proxy-prod`, `nyc3`), gzipped JSON |
| Debug archive | Dedicated `mismatches.sqlite`, async streaming writer |
| Dispatcher | In-process `asyncio.Queue` + worker pool (bounded) |
| Live knobs | `PUT /v1/config` for `shadow_sample_rate` (100% → 0%) |
| Real-time metrics | `GET /v1/metrics` (business counters, in-process) |
| Prometheus scrape | `GET /metrics` |
| Test console | Static UI at `/ui/` (chat + metrics + slider + API explorer) |
| Deploy | Root `Dockerfile` + `.do/app.yaml` for DO App Platform |

---

## 2. Demo run order (script this)

### Step 1 — Start the app (or point at the deployed one)

```sh
# From /workspaces/llm-shadow-proxy
make up           # docker-compose, immutable image
# or, for hot-reload dev:
make up-dev
# open http://localhost:8000/
```

Verify boot from the terminal:

```sh
curl -sS http://localhost:8000/healthz
curl -sS http://localhost:8000/readyz
curl -sS http://localhost:8000/v1/config | jq .
```

**Talking point**: "Startup validates the DO Inference key strictly — non-empty,
minimum length, no dummy markers. If someone commits `your-key-here`, the process
refuses to boot. There's exactly one escape hatch, `SHADOW_PROXY_ALLOW_DUMMY_KEY=1`,
which is only for UI development without live keys."

### Step 2 — Open the test console

Navigate to `http://localhost:8000/`. Point out four panels:

1. **Chat window** — send a prompt, see the primary reply.
2. **Live metrics** — refreshes from `GET /v1/metrics`.
3. **Shadow sample rate slider** — writes `PUT /v1/config` on Apply.
4. **API Explorer** — cards for every endpoint, run inline.

### Step 3 — Send a chat that produces JSON

Prompt in the UI:

```
System: You are a router. Reply as a JSON object like {"action": "..."}.
User:   cancel my subscription
```

What to point out on-screen:

- **Primary block** shows the visible answer (`{"action": "cancel"}`).
- **Primary raw JSON** disclosure shows the full OpenAI-shape response, including
  `reasoning_content` — this is why we use 1024 completion tokens by default:
  the `openai-gpt-oss-*` models are reasoning models and burn tokens internally
  before emitting `content`.
- Under **Candidate**, once the shadow completes, the same view for the candidate model.
- The candidate block's left border is **green (match)**, **red (mismatch)**, or
  **amber (invalid_json / error)** — verdict at a glance.

**Talking point on latency**:
> "The candidate call runs in an `asyncio` worker task fed by a bounded queue.
> The primary response is returned to the client *before* the candidate is even
> dispatched. Candidate latency, timeouts, or errors do not affect the customer path."

### Step 4 — Force a mismatch to demo the tape

Send a prompt with an ambiguous intent, e.g.:

```
User: I want to cancel or maybe pause? decide for me.
```

The two models frequently disagree here. When the candidate finishes:

- UI shows the two `action` values side by side.
- Verdict shows `mismatch` with reason `action_mismatch:<p>!=<c>`.

Now demonstrate the tape:

```sh
sqlite3 /workspaces/llm-shadow-proxy/data/mismatches.sqlite \
  "SELECT request_id, verdict, primary_content, candidate_content
   FROM mismatches ORDER BY inserted_at DESC LIMIT 5;"
```

**Talking point**:
> "Mismatches stream into a dedicated SQLite file via an `asyncio.Queue` and a
> background writer. Two reasons for the separation: (1) the OLTP table stays
> lean and fast for aggregation queries, and (2) the tape has drop-oldest overflow
> so a debug flood never backpressures the customer path."

### Step 5 — Dial the mirroring rate live

In the UI: move the slider to `0.5`, click **Apply**. Or from a terminal:

```sh
curl -sS -X PUT http://localhost:8000/v1/config \
  -H 'content-type: application/json' \
  -d '{"shadow_sample_rate": 0.5}' | jq .
```

Now send several chats in a row. Half will show `X-Shadow-Sampled: true` in the
response headers; half will show `false` and increment `shadow_sampled_out` in
`/v1/metrics`.

**Talking point**:
> "This is the rollout knob. Day one you run at 1.0 — highest signal. Once
> `exact_match_rate_pct` is stable above your threshold, you tune down to save
> candidate LLM cost. No redeploy, no restart, no code change. The sampling gate
> reads `app.state.shadow_sample_rate` fresh on every request."

### Step 6 — Inspect the raw store (DO Spaces)

Open the **API Explorer** in the UI, or:

```sh
# Get a recent request_id from the chat UI, then:
curl -sS http://localhost:8000/v1/evaluations/<REQUEST_ID>/raw | jq .
```

**Talking point**:
> "The raw payload — including reasoning_content, prompt, and usage — lives in
> DO Spaces, keyed by `raw/YYYY/MM/DD/<request_id>.json.gz`. The SQL row stores
> only the fields we aggregate on. This means our hot table stays small even
> when responses are 20–50 KB, and retention on Spaces is decoupled from the DB."

### Step 7 — Business metrics

Refresh **Live metrics** in the UI, or:

```sh
curl -sS http://localhost:8000/v1/metrics | jq .
```

Point out:

- `requests_total`, `requests_success` — customer path.
- `shadow_enqueued` vs `shadow_sampled_out` — how the sampling gate is behaving.
- `shadow_errors`, `shadow_timeouts` — candidate failure modes.
- `exact_match_rate_pct` — computed over finalized comparisons (matches vs. matches+mismatches+invalid_json).

**Talking point**:
> "This endpoint is intentionally process-local — O(1) counters in `app.state`.
> For cluster aggregates use the Prometheus `/metrics` scrape. `GET /v1/metrics`
> is the fast operator screen and demo screen."

---

## 3. End-to-end request lifecycle (say this while pointing at the diagram)

1. `POST /v1/chat` — request lands, ULID `request_id` assigned, prompt hashed.
2. **Primary call** via `httpx.AsyncClient` → DO Serverless Inference (`openai-gpt-oss-120b`).
3. Primary response validated as a Pydantic model.
4. **Concurrently**:
   - Row inserted into SQL store with `candidate_status=pending`.
   - Raw payload gzipped and PUT into DO Spaces at `raw/YYYY/MM/DD/<id>.json.gz`.
5. **Sampling gate** — `random.random() < app.state.shadow_sample_rate` decides
   whether to enqueue the candidate work.
6. Response returned to client (200 OK) with headers `X-Request-ID`,
   `X-Primary-Model`, `X-Shadow-Sampled`, `X-Shadow-Sample-Rate`, `X-Shadow-Enqueued`.
7. Later, a worker dequeues → calls candidate (`openai-gpt-oss-20b`) → evaluator
   runs on both `content` strings.
8. Evaluator computes `verdict ∈ {match, mismatch, invalid_json, candidate_error}`,
   returns reasons. Row updated. If not `match`, `MismatchTape.offer(row)` fires
   asynchronously.
9. A **sweeper task** runs every 5 minutes to reconcile rows still stuck in
   `pending`/`in_progress` beyond the stale threshold.
10. Business counters increment throughout — surfaced via `GET /v1/metrics`.

---

## 4. Why these design choices (Q&A prep)

### "Why not just log both responses and diff offline?"

- Two reasons: **latency isolation** and **operational speed**. Offline diff means
  you can't see the match rate in real time and you can't turn shadow mirroring
  off if the candidate provider has an incident. This proxy gives you both.

### "Why Postgres + Spaces instead of Mongo?"

- We aggregate — match rate, p95 latency, verdict breakdown by tenant. Those are
  SQL's strength, weakness in NoSQL. Meanwhile the *unstructured* thing here is
  the raw LLM payload, which is large and doesn't need to be queryable — Spaces
  is perfect for it. So: SQL for what we query, object storage for what we archive.
  Same "hybrid" pattern used by every mature audit-log system.

### "Why in-process queue instead of Redis/Kafka?"

- MVP scoping. The `CandidateDispatcher` is a protocol, `InProcessDispatcher` is
  one implementation; `RedisStreamsDispatcher` is a config flip away. For hundreds
  of RPS per replica, in-process is right — it's I/O-bound, bounded, drop-metric'd,
  and requires zero extra infra. When we need multi-replica candidate work-sharing,
  we swap the implementation, not the pipeline.

### "How do you make sure the primary response is never lost?"

- Primary is returned **before** anything about the candidate happens. The row is
  persisted with `candidate_status=pending` *before* the client sees the response.
  If the candidate side crashes, times out, or the whole worker pool dies, the
  primary response is already durable in the SQL row and the raw payload is
  already in Spaces. The sweeper catches stuck pendings later.

### "What if the candidate LLM is slow?"

- The candidate lives in its own `httpx.AsyncClient` with its own timeout (20s).
  There's no shared connection pool, no shared timeout budget. If the candidate
  saturates, the queue fills, `overflow_policy: drop_new` kicks in, and a metric
  fires — but customer requests still return with only primary latency.

### "Reasoning models — what's the gotcha?"

- `openai-gpt-oss-*` emit `choices[0].message.reasoning_content` before `content`.
  Reasoning tokens count against `max_completion_tokens`. At 256 tokens we saw
  `finish_reason=length` with null `content`. The UI now uses 1024 and the
  evaluator handles missing content as `invalid_json` with a specific reason,
  not a hard failure.

### "How is the API key handled?"

- It's a `SecretStr` in `pydantic-settings`. Boot calls `assert_runtime_ready()`
  which checks: non-empty, ≥ 20 chars, and no dummy markers (`example`, `dummy`,
  `your-...-here`, `change-me`, etc.). Defense-in-depth: `DOInferenceClient`
  re-validates strictly on init. Only `SHADOW_PROXY_ALLOW_DUMMY_KEY=1` disables
  it, and it's clearly named as a dev-only escape hatch.

### "How do you avoid CWD-related config bugs?"

- `_find_env_file()` finds `.env` regardless of where the process was launched
  (override → CWD walk-up → repo root). Then all *relative* paths in the config
  (`config_file`, `raw_store_filesystem_path`, sqlite `DATABASE_URL`) are anchored
  to that `.env`'s directory. Running from `/tmp` behaves identically to running
  from the repo root.

### "How is the sample rate propagated across replicas?"

- It isn't yet — it's a per-replica knob today, on purpose. Making it cluster-wide
  would push it to Redis or to DO App Platform's env-update flow, which is a
  different tradeoff (durability + slower propagation). The current design is
  the right one for the deploy scale we're targeting; the escalation path is
  documented in `Architecture_Doc.md §11.5.3`.

---

## 5. Deployment cheat sheet

### Local (Docker)

```sh
make up          # immutable image, prod-like
make up-dev      # source-mounted, hot reload
make logs        # tail
make sh          # exec into container
```

### DO App Platform

1. Push to GitHub.
2. In DO Console → Apps → Create App → point at the repo. App Platform reads
   `.do/app.yaml` and picks up `docker/Dockerfile`.
3. **Set secrets in the console** (they are intentionally blank in the spec):
   - `DO_INFERENCE_API_KEY`
   - `DO_SPACES_KEY`
   - `DO_SPACES_SECRET`
4. Deploy. Health check hits `/healthz`. Once green, `curl /v1/config` on the
   assigned URL to confirm the resolved env file and model configuration.

---

## 6. Things that would surprise a code-reviewer

1. `settings.py` does path anchoring — this is why config/DB/raw paths behave
   identically from any CWD.
2. `chat.py` extracts a `_shadow_gate()` helper. It's small on purpose so it can
   be unit-tested and to keep the `chat` handler under lint's PLR0915 threshold.
3. `MismatchTape` uses drop-oldest overflow. If the writer can't keep up, we lose
   *debug* data, never *customer* data.
4. `lifespan` in `main.py` has `noqa: PLR0915`. It's the wiring layer; keeping
   the wiring flat is a legibility choice.
5. There are **very few tests** — the user explicitly deprioritized tests to focus
   on functionality. The evaluator, hashing, IDs, and dispatcher have unit tests;
   larger integration coverage is deferred.
