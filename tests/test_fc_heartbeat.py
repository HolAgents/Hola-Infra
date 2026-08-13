"""Heartbeat endpoint — claim lease refresh (M3, Hola-Infra#43)."""


def _insert_and_claim(client, api_key_headers):
    payload = {
        "action": "opened",
        "issue": {"number": 1, "node_id": "I_1", "title": "t"},
        "repository": {"full_name": "HolAgents/test"},
        "sender": {"login": "test-user"},
    }
    resp = client.post("/api/events/claim", headers=api_key_headers, json={"limit": 10})
    # ensure there is at least one event by ingesting through the repo layer
    from app.db import get_connection
    from app.repository import insert_event
    conn = get_connection()
    insert_event(conn, "hb-1", "issues", "HolAgents/test", payload, "sig")
    conn.close()
    resp = client.post("/api/events/claim", headers=api_key_headers, json={"limit": 10})
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert events, "expected a claimable event"
    return events[0]


def test_heartbeat_refreshes_processing_event(client, api_key_headers):
    ev = _insert_and_claim(client, api_key_headers)
    resp = client.post(
        f"/api/events/{ev['id']}/heartbeat",
        headers=api_key_headers,
        json={"claim_token": ev["claim_token"]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_heartbeat_wrong_token_404(client, api_key_headers):
    ev = _insert_and_claim(client, api_key_headers)
    resp = client.post(
        f"/api/events/{ev['id']}/heartbeat",
        headers=api_key_headers,
        json={"claim_token": "wrong"},
    )
    assert resp.status_code == 404


def test_heartbeat_non_processing_404(client, api_key_headers):
    ev = _insert_and_claim(client, api_key_headers)
    # ack it → completed → heartbeat must 404
    resp = client.post(
        "/api/events/ack",
        headers=api_key_headers,
        json={"results": [{
            "event_id": ev["id"],
            "claim_token": ev["claim_token"],
            "status": "completed",
        }]},
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/api/events/{ev['id']}/heartbeat",
        headers=api_key_headers,
        json={"claim_token": ev["claim_token"]},
    )
    assert resp.status_code == 404
