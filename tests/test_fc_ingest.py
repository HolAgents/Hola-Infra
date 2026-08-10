"""Test webhook ingest — store, deduplicate, filter."""

import hashlib
import hmac
import json


def _sign(body: bytes) -> str:
    secret = b"test_secret"
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _post(client, payload: dict, delivery: str = "aaa-111", event: str = "issues"):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )


def test_store_and_query(client, sample_issue_opened, api_key_headers):
    # Ingest
    resp = _post(client, sample_issue_opened, delivery="ingest-001")
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["event_id"] == 1

    # Query
    resp = client.get("/api/events/1", headers=api_key_headers)
    assert resp.status_code == 200
    ev = resp.json()
    assert ev["delivery_id"] == "ingest-001"
    assert ev["event_type"] == "issues"
    assert ev["repo_full_name"] == "HolAgents/Hola-Infra"
    assert ev["status"] == "received"


def test_duplicate_delivery(client, sample_issue_opened):
    # First ingest
    resp1 = _post(client, sample_issue_opened, delivery="dup-001")
    assert resp1.status_code == 201

    # Second ingest (same delivery_id) — should be idempotent
    resp2 = _post(client, sample_issue_opened, delivery="dup-001")
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"


# ---------------------------------------------------------------------------
# CI events
# ---------------------------------------------------------------------------


def test_ingest_workflow_run_event(client):
    """workflow_run failure is ingested successfully."""
    wf_payload = {
        "action": "completed",
        "workflow_run": {
            "name": "CI",
            "conclusion": "failure",
            "head_commit": {"id": "abc123"},
            "html_url": "https://github.com/test/actions/runs/1",
        },
        "repository": {"full_name": "HolAgents/test"},
        "sender": {"login": "github-actions"},
    }
    resp = _post(client, wf_payload, delivery="ci-wf-001", event="workflow_run")
    assert resp.status_code == 201
    assert resp.json()["status"] == "accepted"


def test_ingest_check_run_event(client):
    """check_run failure is ingested successfully."""
    cr_payload = {
        "action": "completed",
        "check_run": {
            "name": "test",
            "conclusion": "failure",
            "head_commit": {"id": "def456"},
            "html_url": "https://github.com/test/actions/runs/2",
        },
        "repository": {"full_name": "HolAgents/test"},
        "sender": {"login": "github-actions"},
    }
    resp = _post(client, cr_payload, delivery="ci-cr-001", event="check_run")
    assert resp.status_code == 201


def test_task_status_null_for_ci_events(client, api_key_headers):
    """CI events have task_status = NULL after ingest."""
    wf_payload = {
        "action": "completed",
        "workflow_run": {
            "name": "CI",
            "conclusion": "failure",
            "head_commit": {"id": "null-test-001"},
            "html_url": "https://github.com/test/actions/runs/3",
        },
        "repository": {"full_name": "HolAgents/test"},
        "sender": {"login": "github-actions"},
    }
    _post(client, wf_payload, delivery="ci-null-001", event="workflow_run")

    resp = client.get("/api/events/1", headers=api_key_headers)
    assert resp.json()["task_status"] is None
    assert resp.json()["event_type"] == "workflow_run"
