# Hola Infra — Webhook + Task Dispatch

GitHub webhook receiver (FC) + local dispatcher for [HolAgents](https://github.com/HolAgents) organization.

## Architecture

```
GitHub Webhook → FC (阿里云杭州, FastAPI + SQLite/NAS)
                    ↕ API (claim/ack)
              Dispatcher (本地) → Kanban + Claude Code Agent
```

See [design doc](https://github.com/HolAgents/Hola-Infra/issues/4) for full architecture.

## Monorepo

| Directory | Where | Purpose |
|-----------|-------|---------|
| `fc/` | 阿里云 FC 杭州 | Webhook receiver + event ledger |
| `dispatcher/` | 本地 | Poll FC, dispatch to agents, update Kanban |
| `tests/` | both | pytest suite |

## Quick Start

### 1. FC (cloud)

```bash
cd fc
cp .env.example .env   # fill in GITHUB_WEBHOOK_SECRET + API_KEY
# local dev:
uvicorn fc.main:app --host 0.0.0.0 --port 9000 --reload
# deploy:
s nas init
s deploy
```

### 2. Dispatcher (local)

```bash
cd dispatcher
cp ../fc/.env.example .env  # fill in fc_base_url + api_key + github_token
python -m dispatcher
```

## API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/webhooks/github` | HMAC-SHA256 | GitHub webhook ingest |
| POST | `/api/events/claim` | API Key | Atomically claim received events |
| POST | `/api/events/ack` | API Key | Report claim results |
| GET | `/api/events?status=...` | API Key | Query events |
| GET | `/healthz` | none | Health check |

## Event Lifecycle

```
received → (claim) → processing → (ack:completed) → completed
                  \            \→ (ack:failed) → failed
                   \→ (TTL 15min, retry<3) → requeue back to received
```

## Kanban Mapping

| FC Status | Kanban Column |
|-----------|---------------|
| received (new) | Backlog |
| received (claimed) | Ready |
| processing (agent working) | In progress |
| processing (agent done) | In review |
| completed | Done |
