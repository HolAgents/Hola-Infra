"""PostgreSQL backend tests — skipped unless DATABASE_URL is set.

Run in CI against a postgres service container; locally, run with:

    DATABASE_URL=postgresql://user:pass@host:5432/db python -m pytest tests/test_repository_pg.py -v
"""

import json
import os

import pytest

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")

from app.db import get_connection  # noqa: E402
from app.repository import (  # noqa: E402
    ack_batch,
    claim_batch,
    get_event,
    insert_event,
    patch_event,
    query_by_commit,
    query_events,
)

PAYLOAD = {
    "action": "opened",
    "issue": {"number": 1, "node_id": "I_1", "title": "t"},
    "repository": {"full_name": "HolAgents/test"},
    "sender": {"login": "test-user"},
}


@pytest.fixture()
def conn():
    conn = get_connection()
    with conn:
        conn.cursor().execute("TRUNCATE events RESTART IDENTITY")
    yield conn
    conn.close()


def _insert(conn, delivery, event_type="issues", payload=None, repo="HolAgents/test"):
    return insert_event(conn, delivery, event_type, repo, payload or PAYLOAD, "sig")


def test_insert_and_dedup(conn):
    first = _insert(conn, "d-1")
    assert first is not None
    assert _insert(conn, "d-1") is None  # duplicate delivery ignored
    second = _insert(conn, "d-2")
    # ids are opaque: a conflicted insert may still consume a sequence
    # value, so assert distinct/increasing rather than contiguous ids
    assert second is not None and second > first


def test_claim_order_and_ack(conn):
    id1 = _insert(conn, "d-1")
    id2 = _insert(conn, "d-2")
    claimed, remaining = claim_batch(conn, limit=10)
    assert [c["id"] for c in claimed] == [id1, id2]
    assert remaining == 0

    res = ack_batch(conn, [{
        "event_id": claimed[0]["id"],
        "claim_token": claimed[0]["claim_token"],
        "status": "completed",
    }])
    assert res["acked"] == 1
    ev = get_event(conn, id1)
    assert ev["status"] == "completed"


def test_ack_token_mismatch_rejected(conn):
    _insert(conn, "d-1")
    claimed, _ = claim_batch(conn, limit=1)
    res = ack_batch(conn, [{
        "event_id": claimed[0]["id"],
        "claim_token": "wrong-token",
        "status": "completed",
    }])
    assert res["acked"] == 0
    assert res["rejected"][0]["reason"] == "token mismatch"


def test_same_issue_dedup_skips_busy(conn):
    payload2 = dict(PAYLOAD)
    id1 = _insert(conn, "d-1", payload=PAYLOAD)
    _insert(conn, "d-2", payload=payload2)
    first, _ = claim_batch(conn, limit=1)
    assert [c["id"] for c in first] == [id1]  # claims only one
    second, _ = claim_batch(conn, limit=1)
    assert second == []  # same issue number is processing → skip


def test_commit_query_and_patch(conn):
    _insert(conn, "d-1")
    claimed, _ = claim_batch(conn, limit=1)
    token = claimed[0]["claim_token"]

    ev = patch_event(conn, claimed[0]["id"], {
        "claim_token": token,
        "commit_sha": "a" * 40,
        "session_id": "sid-1",
        "task_status": "pushed",
    })
    assert ev["commit_sha"] == "a" * 40

    found = query_by_commit(conn, "a" * 40)
    assert found is not None
    assert found["session_id"] == "sid-1"


def test_query_events_filter(conn):
    _insert(conn, "d-1")
    _insert(conn, "d-2", event_type="push")
    events = query_events(conn, status_filter="received", limit=50)
    assert len(events) == 2
    pushes = query_events(conn, repo="HolAgents/test", limit=50)
    assert len(pushes) == 2
