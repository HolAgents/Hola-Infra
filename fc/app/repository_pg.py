"""PostgreSQL backend for the events ledger (PolarDB Serverless).

Same function signatures as ``repository_sqlite``. Claim uses
``FOR UPDATE SKIP LOCKED`` — the native queue-claim primitive — so
concurrent claims across FC instances are correct by construction
(Hola-Infra#36).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EVENT_COLS = (
    "id, delivery_id, event_type, repo_full_name, payload, signature, status, "
    "retry_count, claim_token, agent_id, error_message, received_at, claimed_at, "
    "completed_at, identity_name, target_id, session_id, commit_sha, task_status"
)


def _to_dict(row, cur) -> dict[str, Any]:
    cols = [d[0] for d in cur.description]
    d = dict(zip(cols, row))
    payload = d.get("payload")
    if isinstance(payload, str):
        d["payload"] = json.loads(payload)
    return d


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def insert_event(
    conn,
    delivery_id: str,
    event_type: str,
    repo_full_name: str,
    payload: dict,
    signature: str,
) -> Optional[int]:
    """Insert an event; return its id, or None when the delivery_id already
    exists (idempotent webhook redelivery)."""
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO events (delivery_id, event_type, repo_full_name, payload, signature)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (delivery_id) DO NOTHING
           RETURNING id""",
        (delivery_id, event_type, repo_full_name, psycopg2.extras.Json(payload), signature),
    )
    row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

def _requeue_expired(conn) -> None:
    """Reset stale ``processing`` events back to ``received`` (TTL requeue).

    Events that exceed ``max_retries`` are marked ``failed`` instead.
    """
    from app.config import get_settings

    settings = get_settings()
    cur = conn.cursor()
    cur.execute(
        """UPDATE events
           SET status = 'received', retry_count = retry_count + 1,
               claim_token = NULL, claimed_at = NULL
           WHERE status = 'processing'
             AND claimed_at < now() - make_interval(mins => %s)
             AND retry_count < %s""",
        (settings.claim_ttl_minutes, settings.max_retries),
    )
    cur.execute(
        """UPDATE events
           SET status = 'failed', error_message = 'max retries exceeded'
           WHERE status = 'processing'
             AND claimed_at < now() - make_interval(mins => %s)
             AND retry_count >= %s""",
        (settings.claim_ttl_minutes, settings.max_retries),
    )
    conn.commit()


def claim_batch(
    conn,
    limit: int = 20,
    event_types: Optional[list[str]] = None,
) -> tuple[list[dict], int]:
    """Atomically claim up to *limit* ``received`` events.

    Events whose repo + issue/PR number already has a ``processing`` event
    are skipped to prevent duplicate dispatch of the same work item.
    ``FOR UPDATE SKIP LOCKED`` makes concurrent claimers safe by
    construction.

    Returns ``(claimed_events, remaining_count)``.
    """
    _requeue_expired(conn)

    claim_token = uuid.uuid4().hex
    cur = conn.cursor()

    type_clause = "AND e.event_type = ANY(%s)" if event_types else ""
    params: list[Any] = []
    if event_types:
        params.append(event_types)
    params.extend([limit, claim_token])

    sql = f"""
        WITH candidates AS (
            SELECT e.id FROM events e
            WHERE e.status = 'received' {type_clause}
              AND NOT EXISTS (
                  SELECT 1 FROM events p
                  WHERE p.status = 'processing'
                    AND p.repo_full_name = e.repo_full_name
                    AND COALESCE(p.payload->'issue'->>'number',
                                 p.payload->'pull_request'->>'number') IS NOT NULL
                    AND COALESCE(p.payload->'issue'->>'number',
                                 p.payload->'pull_request'->>'number')
                        = COALESCE(e.payload->'issue'->>'number',
                                   e.payload->'pull_request'->>'number')
              )
            ORDER BY e.id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        ),
        updated AS (
            UPDATE events e SET
                status = 'processing',
                claim_token = %s,
                claimed_at = now()
            FROM candidates c
            WHERE e.id = c.id
            RETURNING e.id, e.delivery_id, e.event_type, e.repo_full_name,
                      e.payload, e.claim_token, e.received_at
        )
        SELECT * FROM updated ORDER BY id
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.commit()

    claimed = []
    for r in rows:
        d = dict(zip([c[0] for c in cur.description], r))
        d["payload"] = d["payload"] if isinstance(d["payload"], dict) else json.loads(d["payload"])
        claimed.append(d)

    cur.execute("SELECT COUNT(*) FROM events WHERE status = 'received'")
    remaining = cur.fetchone()[0]
    return claimed, remaining


# ---------------------------------------------------------------------------
# Ack
# ---------------------------------------------------------------------------

def ack_batch(conn, results: list[dict]) -> dict:
    """Apply a batch of ack results.

    Each result dict must contain ``event_id``, ``claim_token``, and ``status``.
    Optional: ``agent_id``, ``message``, and resume fields.

    Returns ``{"acked": N, "rejected": [{"event_id": N, "reason": "..."}]}``.
    """
    acked = 0
    rejected: list[dict] = []

    with conn:  # single transaction — rollback on any error
        cur = conn.cursor()
        for r in results:
            event_id = r["event_id"]
            claim_token = r["claim_token"]
            new_status = r["status"]
            agent_id = r.get("agent_id")
            message = r.get("message")

            cur.execute(
                "SELECT claim_token, status FROM events WHERE id = %s",
                (event_id,),
            )
            row = cur.fetchone()
            if row is None:
                rejected.append({"event_id": event_id, "reason": "not found"})
                continue
            if row[0] != claim_token:
                rejected.append({"event_id": event_id, "reason": "token mismatch"})
                continue
            if row[1] != "processing":
                rejected.append({"event_id": event_id, "reason": f"status is {row[1]}"})
                continue

            if new_status == "completed":
                cur.execute(
                    """UPDATE events
                       SET status = 'completed', completed_at = now(),
                           agent_id = %s, error_message = %s,
                           task_status = COALESCE(%s, task_status),
                           commit_sha = COALESCE(%s, commit_sha),
                           session_id = COALESCE(%s, session_id),
                           identity_name = COALESCE(%s, identity_name),
                           target_id = COALESCE(%s, target_id)
                       WHERE id = %s""",
                    (agent_id, message,
                     r.get("task_status"), r.get("commit_sha"),
                     r.get("session_id"), r.get("identity_name"),
                     r.get("target_id"), event_id),
                )
            elif new_status == "failed":
                cur.execute(
                    """UPDATE events
                       SET status = 'failed', error_message = %s, agent_id = %s,
                           task_status = COALESCE(%s, task_status),
                           commit_sha = COALESCE(%s, commit_sha)
                       WHERE id = %s""",
                    (message, agent_id, r.get("task_status"), r.get("commit_sha"), event_id),
                )
            else:
                rejected.append({"event_id": event_id, "reason": f"invalid status {new_status}"})
                continue

            acked += 1

    return {"acked": acked, "rejected": rejected}


# ---------------------------------------------------------------------------
# Query (for dispatcher)
# ---------------------------------------------------------------------------

def get_event(conn, event_id: int) -> Optional[dict]:
    """Return a single event by id (or None)."""
    cur = conn.cursor()
    cur.execute(f"SELECT {_EVENT_COLS} FROM events WHERE id = %s", (event_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return _to_dict(row, cur)


def query_events(
    conn,
    status_filter: Optional[str] = None,
    repo: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Read-only event listing (for debugging / inspection)."""
    where = []
    params: list[Any] = []
    if status_filter:
        where.append("status = %s")
        params.append(status_filter)
    if repo:
        where.append("repo_full_name = %s")
        params.append(repo)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.extend([limit, offset])

    cur = conn.cursor()
    cur.execute(
        f"SELECT {_EVENT_COLS} FROM events {clause} ORDER BY id DESC LIMIT %s OFFSET %s",
        params,
    )
    return [_to_dict(r, cur) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Query by commit SHA
# ---------------------------------------------------------------------------

def query_by_commit(conn, commit_sha: str) -> Optional[dict]:
    """Return the work event associated with *commit_sha*.

    Only searches work-type events (issues, pull_request, push,
    issue_comment).  CI trigger events (workflow_run, check_run) are
    ignored — they hold ``commit_sha`` for linking but are not tasks.
    """
    cur = conn.cursor()
    cur.execute(
        """SELECT id, delivery_id, event_type, repo_full_name, payload,
                  identity_name, target_id, session_id, commit_sha,
                  task_status, claim_token, received_at
           FROM events
           WHERE commit_sha = %s
             AND event_type IN ('issues', 'pull_request', 'push', 'issue_comment')
           ORDER BY id DESC LIMIT 1""",
        (commit_sha,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return _to_dict(row, cur)


# ---------------------------------------------------------------------------
# Partial update (PATCH)
# ---------------------------------------------------------------------------

_ALLOWED_PATCH_FIELDS = {
    "task_status", "session_id", "commit_sha",
    "identity_name", "target_id",
}


def patch_event(conn, event_id: int, patch: dict) -> Optional[dict]:
    """Partially update an event.  ``claim_token`` is required for ownership
    verification.  Only whitelisted fields are written.

    Returns the updated row as a dict, or ``None`` when the event is not
    found or the claim_token doesn't match.
    """
    claim_token = patch.pop("claim_token", None)
    cur = conn.cursor()
    cur.execute("SELECT claim_token, status FROM events WHERE id = %s", (event_id,))
    row = cur.fetchone()
    if row is None or row[0] != claim_token:
        return None

    sets: list[str] = []
    params: list[Any] = []
    for k, v in patch.items():
        if k in _ALLOWED_PATCH_FIELDS and v is not None:
            sets.append(f"{k} = %s")
            params.append(v)

    if not sets:
        return None

    params.append(event_id)
    cur.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = %s", params)
    conn.commit()
    return get_event(conn, event_id)
