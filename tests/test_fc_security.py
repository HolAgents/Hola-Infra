"""Test HMAC signature verification and API key auth."""

import hashlib
import hmac
import json


def _sign(body: bytes) -> str:
    secret = b"test_secret"
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _make_headers(body: bytes, event: str = "issues", delivery: str = "aaa-111"):
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": _sign(body),
        "Content-Type": "application/json",
    }


# ---- webhook endpoint ----------------------------------------------------


def test_valid_webhook(client, sample_issue_opened):
    body = json.dumps(sample_issue_opened).encode()
    resp = client.post("/api/webhooks/github", content=body, headers=_make_headers(body))
    assert resp.status_code == 201
    assert resp.json()["status"] == "accepted"


def test_invalid_signature(client, sample_issue_opened):
    body = json.dumps(sample_issue_opened).encode()
    headers = _make_headers(body)
    headers["X-Hub-Signature-256"] = "sha256=deadbeef"
    resp = client.post("/api/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 401


def test_missing_signature(client, sample_issue_opened):
    body = json.dumps(sample_issue_opened).encode()
    resp = client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "aaa-111",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_missing_headers(client, sample_issue_opened):
    body = json.dumps(sample_issue_opened).encode()
    resp = client.post("/api/webhooks/github", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_invalid_json(client):
    body = b"not json"
    headers = _make_headers(body)
    resp = client.post("/api/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 400


# ---- API key auth --------------------------------------------------------


def test_claim_without_api_key(client):
    resp = client.post("/api/events/claim", json={"limit": 5})
    assert resp.status_code == 401


def test_claim_with_valid_api_key(client, api_key_headers):
    resp = client.post("/api/events/claim", json={"limit": 5}, headers=api_key_headers)
    assert resp.status_code == 200


def test_claim_with_invalid_api_key(client):
    resp = client.post("/api/events/claim", json={"limit": 5}, headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401
