"""Test schema migration — idempotent column additions."""

import sqlite3


def test_migration_adds_all_columns():
    """All 5 new columns exist after migration."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # Create base table
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id     TEXT    NOT NULL,
            event_type      TEXT    NOT NULL,
            repo_full_name  TEXT    NOT NULL,
            payload         TEXT    NOT NULL,
            signature       TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'received'
                            CHECK (status IN ('received','processing','completed','failed')),
            retry_count     INTEGER NOT NULL DEFAULT 0,
            claim_token     TEXT,
            agent_id        TEXT,
            error_message   TEXT,
            received_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            claimed_at      TEXT,
            completed_at    TEXT
        );
    """)

    from app.migrate import run_migrations
    run_migrations(conn)

    # Verify columns
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info('events')").fetchall()
    }
    for col in ("identity_name", "target_id", "session_id",
                "commit_sha", "task_status"):
        assert col in cols, f"column {col} not found"

    conn.close()


def test_migration_idempotent():
    """Running migration twice does not raise an error."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            repo_full_name TEXT NOT NULL,
            payload TEXT NOT NULL,
            signature TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'received',
            retry_count INTEGER NOT NULL DEFAULT 0,
            claim_token TEXT,
            agent_id TEXT,
            error_message TEXT,
            received_at TEXT NOT NULL DEFAULT (datetime('now')),
            claimed_at TEXT,
            completed_at TEXT
        );
    """)

    from app.migrate import run_migrations
    run_migrations(conn)  # first
    run_migrations(conn)  # second — must not raise

    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info('events')").fetchall()
    }
    assert "identity_name" in cols

    conn.close()


def test_new_indexes_created(client, api_key_headers):
    """Both commit_sha and identity indexes exist after migration."""
    # Trigger DB init via any request that calls get_connection()
    client.get("/api/events?limit=1", headers=api_key_headers)

    import sqlite3
    from app.config import get_settings
    conn = sqlite3.connect(get_settings().db_path)
    conn.row_factory = sqlite3.Row

    indexes = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "ix_events_commit_sha" in indexes
    assert "ix_events_identity" in indexes
    conn.close()
