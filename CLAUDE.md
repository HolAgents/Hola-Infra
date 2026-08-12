# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

Hola-Infra is a **webhook → dispatch pipeline** for AI coding agents in the HolAgents GitHub org.

```
GitHub webhook → FC (FastAPI + SQLite on Alibaba Cloud) ← Dispatcher polls → launches Coding Agents
```

Two deployable units in one monorepo:
- **`fc/`** — FastAPI webhook receiver deployed to Alibaba Cloud FC Hangzhou (`python3.10` runtime)
- **`dispatcher/`** — local poll loop that claims events from FC and dispatches to Claude Code / Hermes agents

The `fc/` directory becomes the root on FC (`codeUri: ./`). All internal imports use `from app.xxx`, never `from fc.app.xxx`. Tests run from repo root with `fc/` added to `sys.path` in conftest.

## Commands

```bash
# Run FC locally
cd fc
GITHUB_WEBHOOK_SECRET=test API_KEY=test DB_PATH=/tmp/hola.db \
  python -m uvicorn main:app --host 0.0.0.0 --port 9000 --reload

# Run tests
python -m pytest tests/ -v --tb=short

# Run single test file
python -m pytest tests/test_fc_claim.py -v

# Deploy to FC (via GitHub Actions)
# Go to Actions → CI/CD → Run workflow → production

# Init Kanban (one-time)
cd dispatcher && python -m dispatcher --init-kanban

# Run dispatcher
cd dispatcher && python -m dispatcher
```

## Key design decisions

- **No shared code between `fc/` and `dispatcher/`** — they communicate only via the HTTP API (claim/ack)
- **Claim is atomic** — `UPDATE ... RETURNING` in a single SQLite transaction prevents two pullers from claiming the same event
- **Same-issue dedup** — claim query excludes events whose issue/PR number already has a `processing` event
- **TTL requeue** — `processing` events not acked within 15 minutes auto-reset to `received` (max 3 retries)
- **`runtime: python3.10`** — not `custom` (which uses Python 3.7, incompatible with current deps)
- **Imports use `from app.xxx`** — no `fc.` prefix, because FC extracts `fc/` contents as the code root
- **Webhook signature verification** — reads raw body bytes before JSON parse; uses `hmac.compare_digest`

## Database

Single-table `events` in SQLite (WAL mode, NAS-mounted at `/mnt/nas/events.db`):

| status | meaning |
|--------|---------|
| `received` | new, waiting for claim |
| `processing` | claimed by dispatcher, agent working |
| `completed` | done |
| `failed` | failed or max retries exceeded |

`delivery_id` is UNIQUE for idempotent webhook redelivery. `claim_token` prevents cross-puller ack confusion.

## Event routing

`dispatcher/router.py`: event_type + action + sender → agent_type decision. Bot senders (configurable) are skipped to prevent feedback loops. `workflow_run` failures route to `ci-debug` agent.

`dispatcher/identity.py`: agent_type → AgentIdentity lookup (currently local registry, will switch to Hola-Switch API). Identity includes GitHub username, repo scope, and target adapter type.

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`):
- `test` — pytest (24 tests)
- `fc-smoke` — starts uvicorn, full ingest→claim→ack flow
- `fc-package-check` — pip install `-t .` + import check (catches missing deps before deploy)
- Deploy jobs — OIDC → STS → `s deploy`, manual trigger only

`.github/workflows/ocr-review.yml`:
- `code-review` — OpenCodeReview (`alibaba/open-code-review`) posts AI inline review comments + a sticky summary on PRs; re-triggerable with a `/open-code-review` comment. Requires `OCR_LLM_URL` / `OCR_LLM_AUTH_TOKEN` secrets and `OCR_LLM_MODEL` / `OCR_LLM_USE_ANTHROPIC` vars.

Deploy uses OIDC (no long-lived AccessKeys). RAM role `fc-github-action` trusts IdP `action`.
