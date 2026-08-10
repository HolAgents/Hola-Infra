"""Repository — all SQLite operations for the events table.

Every public function receives a ``conn`` (``sqlite3.Connection``) as its first
argument so that callers control transaction boundaries.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Optional

from fc.app.config import get_settings


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def insert_event(
    conn,
    delivery_id: str,
    event_type: str,
    repo_full_name: str,
    payload: dict[str, Any],
    signature: str,
) -> dict | None:
    """Insert a new webhook event (idempotent via delivery_id).

    Returns the fresh row as a dict, or ``None`` when the delivery_id already
    exists (duplicate).  Checks the UNIQUE index explicitly, then falls back
    to IntegrityError for race protection.
    """
    # Fast path: check for existing delivery_id
    existing = conn.execute(
        "SELECT id FROM events WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    if existing:
        return None

    try:
        cur = conn.execute(
            """INSERT INTO events
               (delivery_id, event_type, repo_full_name, payload, signature)
               VALUES (?, ?, ?, ?, ?)""",
            (delivery_id, event_type, repo_full_name, json.dumps(payload), signature),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM events WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.IntegrityError:
        # Race: another concurrent request inserted same delivery_id
        return None


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

def _requeue_expired(conn) -> None:
    """Reset stale ``processing`` events back to ``received`` (TTL requeue).

    Events that exceed ``max_retries`` are marked ``failed`` instead.
    """
    settings = get_settings()
    conn.execute(
        """UPDATE events
           SET status = 'received',
               retry_count = retry_count + 1,
               claim_token = NULL,
               claimed_at = NULL
           WHERE status = 'processing'
             AND claimed_at < datetime('now', ?)
             AND retry_count < ?""",
        (f"-{settings.claim_ttl_minutes} minutes", settings.max_retries),
    )
    conn.execute(
        """UPDATE events
           SET status = 'failed',
               error_message = 'max retries exceeded'
           WHERE status = 'processing'
             AND claimed_at < datetime('now', ?)
             AND retry_count >= ?""",
        (f"-{settings.claim_ttl_minutes} minutes", settings.max_retries),
    )


def claim_batch(
    conn,
    limit: int = 20,
    event_types: Optional[list[str]] = None,
) -> tuple[list[dict], int]:
    """Atomically claim up to *limit* ``received`` events.

    Events whose repo + issue/PR number already has a ``processing`` event are
    skipped to prevent duplicate dispatch of the same work item.

    Returns ``(claimed_events, remaining_count)``.
    """
    _requeue_expired(conn)

    claim_token = uuid.uuid4().hex

    # Build optional event_type filter
    type_clause = ""
    params: list[Any] = []
    if event_types:
        placeholders = ",".join(["?" for _ in event_types])
        type_clause = f"AND event_type IN ({placeholders})"
        params.extend(event_types)

    params.append(limit)
    params.append(claim_token)

    sql = f"""\
        WITH candidates AS (
            SELECT * FROM events
            WHERE status = 'received' {type_clause}
            ORDER BY id
            LIMIT ?
        ),
        busy AS (
            SELECT repo_full_name,
                   json_extract(payload, '$.issue.number') AS item_number
            FROM events WHERE status = 'processing'
            UNION
            SELECT repo_full_name,
                   json_extract(payload, '$.pull_request.number') AS item_number
            FROM events WHERE status = 'processing'
        ),
        filtered AS (
            SELECT c.*
            FROM candidates c
            LEFT JOIN busy b
              ON b.repo_full_name = c.repo_full_name
             AND b.item_number IN (
                  json_extract(c.payload, '$.issue.number'),
                  json_extract(c.payload, '$.pull_request.number')
             )
            WHERE b.item_number IS NULL
        ),
        -- deduplicate within candidates: only take the first per (repo, item_number)
        dedup AS (
            SELECT f.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY f.repo_full_name,
                           COALESCE(
                               json_extract(f.payload, '$.issue.number'),
                               json_extract(f.payload, '$.pull_request.number')
                           )
                       ORDER BY f.id
                   ) AS rn
            FROM filtered f
        )
        UPDATE events SET
            status = 'processing',
            claim_token = ?,
            claimed_at = datetime('now')
        WHERE id IN (SELECT id FROM dedup WHERE rn = 1)
        RETURNING id, delivery_id, event_type, repo_full_name, payload,
                  claim_token, received_at
    """
    with conn:  # BEGIN IMMEDIATE
        rows = conn.execute(sql, params).fetchall()

    claimed = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        claimed.append(d)

    remaining = conn.execute(
        "SELECT COUNT(*) FROM events WHERE status = 'received'"
    ).fetchone()[0]

    return claimed, remaining


# ---------------------------------------------------------------------------
# Ack
# ---------------------------------------------------------------------------

def ack_batch(
    conn,
    results: list[dict],  # [{"event_id": N, "claim_token": "...", "status": "...", ...}]
) -> dict:
    """Apply a batch of ack results.

    Each result dict must contain ``event_id``, ``claim_token``, and ``status``.
    Optional: ``agent_id``, ``message``.

    Returns ``{"acked": N, "rejected": [{"event_id": N, "reason": "..."}]}``.
    """
    acked = 0
    rejected: list[dict] = []

    for r in results:
        event_id = r["event_id"]
        claim_token = r["claim_token"]
        new_status = r["status"]
        agent_id = r.get("agent_id")
        message = r.get("message")

        # Verify claim_token ownership
        row = conn.execute(
            "SELECT claim_token, status FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()

        if row is None:
            rejected.append({"event_id": event_id, "reason": "not found"})
            continue
        if row["claim_token"] != claim_token:
            rejected.append({"event_id": event_id, "reason": "token mismatch"})
            continue
        if row["status"] != "processing":
            rejected.append({"event_id": event_id, "reason": f"status is {row['status']}"})
            continue

        if new_status == "completed":
            conn.execute(
                """UPDATE events
                   SET status = 'completed', completed_at = datetime('now'),
                       agent_id = ?, error_message = ?
                   WHERE id = ?""",
                (agent_id, message, event_id),
            )
        elif new_status == "failed":
            conn.execute(
                """UPDATE events
                   SET status = 'failed', error_message = ?, agent_id = ?
                   WHERE id = ?""",
                (message, agent_id, event_id),
            )
        else:
            rejected.append({"event_id": event_id, "reason": f"invalid status {new_status}"})
            continue

        acked += 1

    conn.commit()
    return {"acked": acked, "rejected": rejected}


# ---------------------------------------------------------------------------
# Query (for dispatcher)
# ---------------------------------------------------------------------------

def get_event(conn, event_id: int) -> dict | None:
    """Return a single event by id (or None)."""
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


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
        where.append("status = ?")
        params.append(status_filter)
    if repo:
        where.append("repo_full_name = ?")
        params.append(repo)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.extend([limit, offset])

    rows = conn.execute(
        f"SELECT * FROM events {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()

    results: list[dict] = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        results.append(d)
    return results
