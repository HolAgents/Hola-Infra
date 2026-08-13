"""Connection factory — backend-aware (PostgreSQL or SQLite).

With ``DATABASE_URL`` set, connects to PostgreSQL (PolarDB Serverless);
otherwise uses the legacy SQLite file (local dev / NAS fallback).
Per-request connections, schema init idempotent on every connection.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from app.config import get_settings

_SCHEMA: str | None = None

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    delivery_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    repo_full_name TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    signature TEXT,
    status TEXT NOT NULL DEFAULT 'received'
        CHECK (status IN ('received', 'processing', 'completed', 'failed')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT,
    agent_id TEXT,
    error_message TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    identity_name TEXT,
    target_id TEXT,
    session_id TEXT,
    commit_sha TEXT,
    task_status TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_status_received
    ON events (id) WHERE status = 'received';
CREATE INDEX IF NOT EXISTS ix_events_status_processing
    ON events (claimed_at) WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS ix_events_commit_sha
    ON events (commit_sha) WHERE commit_sha IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_events_identity
    ON events (identity_name) WHERE identity_name IS NOT NULL;
"""


def _load_schema() -> str:
    global _SCHEMA
    if _SCHEMA is not None:
        return _SCHEMA
    schema_path = Path(__file__).parent / "schema.sql"
    _SCHEMA = schema_path.read_text(encoding="utf-8")
    return _SCHEMA


def _ensure_db_dir(db_path: str) -> None:
    """Create the parent directory for the database file if it doesn't exist."""
    parent = Path(db_path).parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def _is_transient(exc: Exception) -> bool:
    """True for lock contention, NAS-induced I/O failures, and PG
    connection blips — all transient and retryable (Hola-Infra#29/#32)."""
    msg = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError):
        return any(k in msg for k in ("locked", "busy", "disk i/o", "i/o error"))
    return any(k in msg for k in ("connection", "closed", "timeout", "unavailable"))


def _execute_with_lock_retry(conn: sqlite3.Connection, sql: str, retries: int = 5) -> None:
    """Execute a statement, retrying on transient lock contention.

    Multiple FC instances share the NAS-backed DB; concurrent cold starts
    can briefly contend on the write lock (see Hola-Infra#29).
    """
    for attempt in range(retries):
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if not _is_transient(exc):
                raise
            if attempt == retries - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def run_with_db_retry(fn, attempts: int = 5):
    """Run ``fn(conn)`` with a fresh connection per attempt, retrying
    transient database errors (lock contention / NAS disk I/O / PG
    connection blips).

    The wrapped operations must be idempotent — claim/ack verify
    claim_tokens and ingest dedups by delivery_id, so retries are safe.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        conn = get_connection()
        try:
            return fn(conn)
        except Exception as exc:  # noqa: BLE001 — both backends raise their own types
            last_exc = exc
            if not _is_transient(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
        finally:
            conn.close()
    raise last_exc  # pragma: no cover — loop always raises or returns


def get_connection():
    """Return a new, initialised database connection for the current request.

    Backend is chosen by settings: PostgreSQL when ``database_url`` is
    set (production), SQLite otherwise (local dev / legacy NAS).
    """
    settings = get_settings()
    if settings.database_url:
        return _get_pg_connection(settings)
    return _get_sqlite_connection(settings)


def _get_sqlite_connection(settings) -> sqlite3.Connection:
    """SQLite connection (rollback journal — the NFS-recommended mode).

    Fresh databases use SQLite's default rollback journal automatically;
    no journal-mode PRAGMA runs here — converting a live database's
    journal mode under concurrent traffic corrupted the NAS file once
    (Hola-Infra#34).
    """
    _ensure_db_dir(settings.db_path)

    conn = sqlite3.connect(
        settings.db_path,
        timeout=5,
        check_same_thread=False,    # each request gets its own connection
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")

    # Idempotent schema init — execute one statement at a time
    for stmt in _load_schema().split(";"):
        stmt = stmt.strip()
        if stmt and not stmt.startswith("--"):
            _execute_with_lock_retry(conn, stmt)
    conn.commit()

    # Apply incremental migrations
    from app.migrate import run_migrations
    run_migrations(conn)

    return conn


def _get_pg_connection(settings):
    """PostgreSQL connection (PolarDB Serverless)."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(settings.database_url, connect_timeout=5)
    psycopg2.extras.register_default_jsonb(conn, loads=json.loads)

    with conn:
        conn.cursor().execute(_PG_SCHEMA)

    return conn
