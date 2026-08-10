"""Idempotent schema migrations.

SQLite does not support ``ALTER TABLE IF NOT EXISTS``, so each column
addition is wrapped in try/except.  Safe to call on every connection.
"""

from __future__ import annotations

import logging
import sqlite3

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


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply all pending migrations idempotently."""
    for col_name, col_type, index_sql in _MIGRATIONS:
        try:
            conn.execute(
                f"ALTER TABLE events ADD COLUMN {col_name} {col_type}"
            )
            logger.info("migration: added column %s", col_name)
        except sqlite3.OperationalError:
            pass  # column already exists

        if index_sql:
            conn.execute(index_sql)

    conn.commit()
    logger.info("migrations complete")
