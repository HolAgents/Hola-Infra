"""SQLite connection factory — rollback journal, per-request connections."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.config import get_settings

_SCHEMA: str | None = None


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


def _is_transient(exc: sqlite3.OperationalError) -> bool:
    """True for lock contention and NAS-induced transient I/O failures.

    Concurrent writers across FC instances hit random ``database is
    locked`` and ``disk I/O error`` on the NAS-backed DB (Hola-Infra#29,
    #32); both are transient and retryable.
    """
    msg = str(exc).lower()
    return any(k in msg for k in ("locked", "busy", "disk i/o", "i/o error"))


def run_with_db_retry(fn, attempts: int = 5):
    """Run ``fn(conn)`` with a fresh connection per attempt, retrying
    transient SQLite errors (lock contention / NAS disk I/O).

    The wrapped operations must be idempotent — claim/ack verify
    claim_tokens and ingest dedups by delivery_id, so retries are safe.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        conn = get_connection()
        try:
            return fn(conn)
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if not _is_transient(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
        finally:
            conn.close()
    raise last_exc  # pragma: no cover — loop always raises or returns


def get_connection() -> sqlite3.Connection:
    """Return a new, initialised SQLite connection for the current request.

    Per-connection PRAGMAs:
        - journal_mode=DELETE (rollback journal) — WAL's fcntl locking is
          unreliable over NAS/NFS and concurrent writers across instances
          hit random "database is locked" errors (Hola-Infra#29)
        - synchronous=NORMAL (acceptable crash window; webhooks are redeliverable)
        - 5-second busy_timeout (wait out transient lock contention)
        - row_factory = sqlite3.Row (dict-like access)
    """
    settings = get_settings()
    _ensure_db_dir(settings.db_path)

    conn = sqlite3.connect(
        settings.db_path,
        timeout=5,
        check_same_thread=False,    # each request gets its own connection
    )
    conn.row_factory = sqlite3.Row
    # No journal_mode PRAGMA here: fresh databases use SQLite's default
    # (rollback journal) automatically, and converting a live database's
    # journal mode under concurrent traffic corrupted the NAS-backed file
    # (Hola-Infra#34).
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
