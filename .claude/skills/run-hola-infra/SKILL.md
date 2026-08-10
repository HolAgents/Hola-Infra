---
name: run-hola-infra
description: Run and debug the Hola-Infra project locally. Start the FC webhook server, test endpoints, run the dispatcher, and verify the full webhook-to-dispatch pipeline.
---

# run-hola-infra

Run and debug the Hola-Infra project locally.

## Quick start

```bash
# Start FC (webhook receiver) on port 9000
cd fc
GITHUB_WEBHOOK_SECRET=test_secret API_KEY=test_key DB_PATH=/tmp/hola.db \
  python -m uvicorn main:app --host 0.0.0.0 --port 9000 --reload

# In another terminal: test endpoints
curl http://localhost:9000/healthz

# Simulate a GitHub webhook
PAYLOAD='{"action":"opened","issue":{"number":42,"node_id":"I_42","title":"test"},"repository":{"full_name":"HolAgents/test"},"sender":{"login":"test-user"}}'
SIG=$(python -c "import hashlib,hmac;print('sha256='+hmac.new(b'test_secret','$PAYLOAD'.encode(),hashlib.sha256).hexdigest())")
curl -s -X POST http://localhost:9000/api/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -H "X-GitHub-Delivery: test-001" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$PAYLOAD"

# Claim events (simulate dispatcher)
curl -s -X POST http://localhost:9000/api/events/claim \
  -H "X-API-Key: test_key" \
  -d '{"limit":10}'

# Ack
curl -s -X POST http://localhost:9000/api/events/ack \
  -H "X-API-Key: test_key" \
  -d '{"results":[{"event_id":1,"claim_token":"TOKEN","status":"completed"}]}'
```

## API reference

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/healthz` | none | Liveness + DB check |
| POST | `/api/webhooks/github` | HMAC-SHA256 | GitHub webhook ingest |
| POST | `/api/events/claim` | `X-API-Key` | Atomically claim received events |
| POST | `/api/events/ack` | `X-API-Key` | Report results for claimed events |
| GET | `/api/events` | `X-API-Key` | Query events (`?status=received&limit=50`) |
| GET | `/api/events/{id}` | `X-API-Key` | Get single event |

## Webhook signature verification

GitHub signs with `HMAC-SHA256(secret, raw_body_bytes)`. FC reads raw body bytes, recomputes HMAC, and does `hmac.compare_digest()`.

Test vector:
```python
import hashlib, hmac
secret = b"test_secret"
body = b'{"key":"value"}'
sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
```

## Event lifecycle

```
received → claim → processing → ack(completed) → completed
                              → ack(failed) → failed
             ↑ TTL 15min + retry<3 → requeue back to received
```

## Database

SQLite with WAL mode at `DB_PATH`. Schema: `fc/app/schema.sql`.

Debug directly:
```bash
sqlite3 /tmp/hola.db "SELECT id, event_type, status, received_at FROM events ORDER BY id DESC LIMIT 10;"
```

## Test suite

```bash
# All tests
python -m pytest tests/ -v --tb=short

# Specific test file
python -m pytest tests/test_fc_claim.py -v

# Run dispatcher tests only
python -m pytest tests/test_dispatcher.py -v
```

## Directory structure

```
fc/           → FC webhook service (deploy to Alibaba Cloud)
dispatcher/   → Local task dispatcher (runs locally, polls FC)
tests/        → pytest for both
```

## Running dispatcher

```bash
cd dispatcher
cp fc/.env.example .env   # configure fc_base_url, api_key, github_token
python -m dispatcher       # starts poll loop
```

`python -m dispatcher --init-kanban` creates an org-level ProjectV2 Kanban.
