"""Test atomic claim, ack, and TTL requeue."""

import hashlib
import hmac
import json
import time


def _sign(body: bytes) -> str:
    secret = b"test_secret"
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _ingest(client, payload: dict, delivery: str, event: str = "issues"):
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


def _make_issue_payload(issue_number: int) -> dict:
    return {
        "action": "opened",
        "issue": {"number": issue_number, "node_id": f"I_{issue_number}"},
        "repository": {"full_name": "HolAgents/test"},
        "sender": {"login": "user"},
    }


def test_claim_empty(api_key_headers, client):
    resp = client.post("/api/events/claim", json={"limit": 10}, headers=api_key_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0


def test_claim_and_ack(api_key_headers, client):
    # Ingest 2 events
    _ingest(client, _make_issue_payload(1), delivery="claim-001")
    _ingest(client, _make_issue_payload(2), delivery="claim-002")

    # Claim
    resp = client.post("/api/events/claim", json={"limit": 10}, headers=api_key_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert data["remaining"] == 0

    ev1, ev2 = data["events"][0], data["events"][1]
    assert "claim_token" in ev1
    assert "claim_token" in ev2

    # Ack completed
    ack_resp = client.post(
        "/api/events/ack",
        json={
            "results": [
                {"event_id": ev1["id"], "claim_token": ev1["claim_token"], "status": "completed"},
                {"event_id": ev2["id"], "claim_token": ev2["claim_token"], "status": "failed", "message": "test fail"},
            ]
        },
        headers=api_key_headers,
    )
    assert ack_resp.status_code == 200
    ack_data = ack_resp.json()
    assert ack_data["acked"] == 2
    assert len(ack_data["rejected"]) == 0

    # Verify status via query
    resp = client.get("/api/events/1", headers=api_key_headers)
    assert resp.json()["status"] == "completed"
    resp = client.get("/api/events/2", headers=api_key_headers)
    assert resp.json()["status"] == "failed"
    assert resp.json()["error_message"] == "test fail"


def test_ack_token_mismatch(api_key_headers, client):
    _ingest(client, _make_issue_payload(3), delivery="claim-003")
    resp = client.post("/api/events/claim", json={"limit": 1}, headers=api_key_headers)
    data = resp.json()

    # Try to ack with wrong token
    ack_resp = client.post(
        "/api/events/ack",
        json={"results": [{"event_id": data["events"][0]["id"], "claim_token": "wrong_token", "status": "completed"}]},
        headers=api_key_headers,
    )
    ack_data = ack_resp.json()
    assert ack_data["acked"] == 0
    assert len(ack_data["rejected"]) == 1
    assert ack_data["rejected"][0]["reason"] == "token mismatch"


def test_same_issue_not_claimed_twice(api_key_headers, client):
    """Two events for the same issue — only the first should be claimable."""
    # Ingest two events for issue #5
    _ingest(client, _make_issue_payload(5), delivery="same-001")
    _ingest(client, _make_issue_payload(5), delivery="same-002")

    # First claim picks up the first event
    resp = client.post("/api/events/claim", json={"limit": 10}, headers=api_key_headers)
    data = resp.json()
    assert data["count"] == 1  # Only one claimed; the other is blocked by same issue

    # Second claim returns 0 (the duplicate issue is still received but blocked)
    resp2 = client.post("/api/events/claim", json={"limit": 10}, headers=api_key_headers)
    data2 = resp2.json()
    assert data2["count"] == 0  # Issue #5 still has processing event

    # Ack the first event
    ev = data["events"][0]
    client.post(
        "/api/events/ack",
        json={"results": [{"event_id": ev["id"], "claim_token": ev["claim_token"], "status": "completed"}]},
        headers=api_key_headers,
    )

    # Now the second event for issue #5 can be claimed
    resp3 = client.post("/api/events/claim", json={"limit": 10}, headers=api_key_headers)
    data3 = resp3.json()
    assert data3["count"] == 1


def test_push_and_pr_exclusion_different_keys(api_key_headers, client):
    """Push events don't have issue number — they should all be claimable."""
    push_payload = {
        "ref": "refs/heads/main",
        "repository": {"full_name": "HolAgents/test"},
        "sender": {"login": "user"},
        "commits": [],
    }
    body = json.dumps(push_payload).encode()
    client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "push-001",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    resp = client.post("/api/events/claim", json={"limit": 10}, headers=api_key_headers)
    assert resp.json()["count"] == 1  # Push always claimable (no issue number to block)
