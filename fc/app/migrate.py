"""Idempotent schema migrations.

SQLite does not support ``ALTER TABLE IF NOT EXISTS``, so each column
addition distinguishes "column already exists" (skip) from transient
lock contention (retry).  Safe to call on every connection.
"""

from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

# Each entry: (column_name, column_type, optional_index_sql)
_MIGRATIONS: list[tuple[str, str, str | None]] = [
    ("identity_name", "TEXT", None),
    ("target_id",     "TEXT", None),
    ("session_id",    "TEXT", None),
    ("commit_sha",    "TEXT",
     "CREATE INDEX IF NOT EXISTS ix_events_commit_sha"
     " ON events(commit_sha) WHERE commit_sha IS NOT NULL"),
    ("task_status",   "TEXT",
     "CREATE INDEX IF NOT EXISTS ix_events_identity"
     " ON events(identity_name) WHERE identity_name IS NOT NULL"),
]


def _add_column(conn: sqlite3.Connection, col_name: str, col_type: str) -> None:
    """ALTER TABLE ADD COLUMN with lock-retry; skip when column exists."""
    for attempt in range(5):
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col_name} {col_type}")
            logger.info("migration: added column %s", col_name)
            return
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg:
                return  # column already exists
            if ("locked" in msg or "busy" in msg) and attempt < 4:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise


def _create_index(conn: sqlite3.Connection, index_sql: str) -> None:
    """CREATE INDEX IF NOT EXISTS with lock-retry."""
    for attempt in range(5):
        try:
            conn.execute(index_sql)
            return
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if ("locked" in msg or "busy" in msg) and attempt < 4:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply all pending migrations idempotently."""
    for col_name, col_type, index_sql in _MIGRATIONS:
        _add_column(conn, col_name, col_type)
        if index_sql:
            _create_index(conn, index_sql)

    conn.commit()
    logger.info("migrations complete")
