# LLM Shadow Proxy

Production-ready API proxy that serves customer traffic through a **Primary LLM** on DigitalOcean Serverless Inference, while asynchronously shadowing every request to a **Candidate LLM** and comparing the two responses using deterministic heuristics.

- Zero impact on primary latency, errors, or availability
- Primary responses are **durably persisted before ack** — never lost even if candidate is slow or the proxy crashes
- Postgres (structured metadata + verdicts) + DO Spaces (raw payloads)
- Plug-and-play interfaces for LLM clients, dispatchers, evaluators, and stores

See **[docs/Architecture_Doc.md](docs/Architecture_Doc.md)** for the full design.

---

## Quick start

### 1. Get a DigitalOcean Model Access Key

1. https://cloud.digitalocean.com → **INFERENCE** → **Manage** → **Model Access Keys** → **Create model access key**
2. Give it a name (`llm-shadow-proxy-dev`), choose "All models" (or scope to specific ones), no VPC restriction for local dev.
3. Copy the secret key immediately — it's shown once.
4. Ensure your account has a positive prepaid balance in **Inference → Manage Prepayment**.

### 2. Install

```bash
cd /workspaces/llm-shadow-proxy
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env: paste your DO_INFERENCE_API_KEY
```

Optionally review `config/models.yaml` and set your Primary / Candidate model IDs. Discover model IDs with:

```bash
curl -H "Authorization: Bearer $DO_INFERENCE_API_KEY" https://inference.do-ai.run/v1/models
```

### 4. Run tests

```bash
pytest
```

### 5. Run the server

```bash
uvicorn shadow_proxy.main:app --reload --host 0.0.0.0 --port 8000
```

- Test Console (chat UI): http://localhost:8000/ui/  (root `/` redirects here)
- Swagger UI: http://localhost:8000/docs
- Metrics: http://localhost:8000/metrics

### 6. Hit the proxy

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "system", "content": "Reply only as JSON {\"action\": \"...\"}."},
      {"role": "user", "content": "book a flight to Paris"}
    ],
    "temperature": 0.0,
    "max_completion_tokens": 32
  }'
```

You'll get the primary model's response immediately. Once the candidate finishes in the background, its comparison shows up in:

```bash
curl http://localhost:8000/v1/evaluations/summary
curl http://localhost:8000/v1/evaluations
```

---

## Running via Docker

The image runs as an unprivileged user, uses a multi-stage build so the final image only contains a Python venv + source, and is fully driven by env vars — no code changes needed to switch between SQLite and Postgres.

### Option A — full stack (Postgres + proxy)

```bash
cp .env.example .env
# Put your DO_INFERENCE_API_KEY into .env

make up            # or: docker compose up -d --build
make logs          # tail proxy logs
make health        # curl /healthz
make smoke         # send a demo /v1/chat request
```

Then:
- Proxy: http://localhost:8000
- Swagger UI: http://localhost:8000/docs
- Postgres: `localhost:5432` (user `shadow`, password `shadow`, db `shadow`)

Data survives restarts via named volumes (`pgdata`, `proxydata`). To wipe:
```bash
docker compose down -v
```

### Option B — SQLite-only (no Postgres, fastest to try)

```bash
make up-dev
# equivalent to:
#   docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build proxy
```

### Option C — just the image (no compose)

```bash
docker build -f docker/Dockerfile -t llm-shadow-proxy:local .

docker run --rm -it \
  -p 8000:8000 \
  -e DO_INFERENCE_API_KEY=$YOUR_KEY \
  -v $(pwd)/data:/app/data \
  llm-shadow-proxy:local
```

### Required / accepted environment variables

| Var | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DO_INFERENCE_API_KEY` | **yes** | — | DO Serverless Inference model access key. Server refuses to boot if empty, <20 chars, or contains placeholder markers (fake/test/dummy/changeme/etc.) |
| `SHADOW_PROXY_ALLOW_DUMMY_KEY` | no | `0` | Set to `1` to bypass key validation for local UI/dev work. Real DO calls will 401. **Never** set in prod. |
| `DO_INFERENCE_BASE_URL` | no | `https://inference.do-ai.run/v1` | Override for testing |
| `DATABASE_URL` | no | SQLite in `/app/data` (docker default), Postgres via compose | `sqlite+aiosqlite://...` or `postgresql+asyncpg://...` |
| `PROXY_API_KEYS` | no | empty (auth disabled) | Comma-separated bearer tokens accepted by `/v1/chat` |
| `RAW_STORE_TYPE` | no | `filesystem` | `filesystem` or `spaces` |
| `RAW_STORE_FILESYSTEM_PATH` | no | `/app/data/raw` | Where gzipped raw payloads land |
| `DO_SPACES_*` | only if `RAW_STORE_TYPE=spaces` | — | Bucket, region, endpoint, key, secret |
| `LOG_LEVEL` | no | `INFO` | Standard levels |
| `LOG_FORMAT` | no | `json` | `json` or `console` |
| `CONFIG_FILE` | no | `/app/config/models.yaml` | Route/dispatcher/evaluator config |

Compose reads `.env` from the repo root automatically (`env_file: .env` with `required: false`). You can also override any env at the shell:

```bash
DO_INFERENCE_API_KEY=xxx LOG_FORMAT=console make up
```

### Editing `config/models.yaml` without rebuilding

The compose file mounts `./config` read-only into the container. Edit the YAML, then `docker compose restart proxy` to pick it up.

### Deploying to DigitalOcean

The same image works on DO App Platform (Docker source), DOKS, or any container runtime. In production:

- Set `DATABASE_URL` to your DO Managed Postgres connection string
- Set `RAW_STORE_TYPE=spaces` + `DO_SPACES_*`
- Set `PROXY_API_KEYS` to enable bearer-token auth
- Scope the DO model access key to the App Platform / DOKS VPC

---

## Layout

```
src/shadow_proxy/    # application code
config/models.yaml   # routes, models, dispatcher, evaluator, store config
docs/                # Architecture doc
tests/               # unit + integration tests
docker/              # Dockerfile
docker-compose.yml   # local Postgres + proxy
```

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/chat` | Customer-facing primary chat proxy |
| GET | `/v1/evaluations` | List comparison records |
| GET | `/v1/evaluations/{request_id}` | Single record |
| GET | `/v1/evaluations/summary` | Aggregate stats |
| GET | `/v1/config` | Runtime config (non-secret) |
| GET | `/healthz` | Liveness |
| GET | `/readyz` | Readiness |
| GET | `/metrics` | Prometheus |
| GET | `/docs` | Swagger UI |
| GET | `/ui/` | Test Console (chat + live evaluation status) |
| GET | `/` | Redirects to `/ui/` |
