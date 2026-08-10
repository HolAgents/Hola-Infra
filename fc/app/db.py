"""SQLite connection factory — WAL mode, per-request connections."""

import sqlite3
import os
from pathlib import Path

from fc.app.config import get_settings

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


def get_connection() -> sqlite3.Connection:
    """Return a new, initialised SQLite connection for the current request.

    Per-connection PRAGMAs:
        - WAL journal mode (writers don't block readers on NAS)
        - synchronous=NORMAL (acceptable crash window; webhooks are redeliverable)
        - 5-second busy_timeout (wait out transient NAS lock contention)
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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")

    # idempotent schema init
    conn.executescript(_load_schema())
    conn.commit()

    return conn
