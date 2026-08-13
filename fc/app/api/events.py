"""POST /api/events/claim, POST /api/events/ack, GET /api/events."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import get_connection, run_with_db_retry
from app.models import (
    AckRequest,
    AckResponse,
    ClaimRequest,
    ClaimResponse,
    EventPatchRequest,
    HeartbeatRequest,
)
from app.security import require_api_key
from app.services.claiming import do_ack, do_claim
from app.repository import (
    get_event,
    heartbeat as heartbeat_repo,
    patch_event,
    query_by_commit,
    query_events,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/events",
    tags=["events"],
    dependencies=[Depends(require_api_key)],
)


@router.post("/claim", response_model=ClaimResponse, summary="Atomically claim received events")
def claim(body: ClaimRequest):
    """Claim up to *limit* ``received`` events in a single atomic transaction.

    Events whose repo + issue/PR number already has a ``processing`` event are
    automatically skipped.
    """
    result = run_with_db_retry(
        lambda conn: do_claim(conn, limit=body.limit, event_types=body.event_types)
    )
    return ClaimResponse(**result)


@router.post("/ack", response_model=AckResponse, summary="Acknowledge processed events")
def ack(body: AckRequest):
    """Report results for claimed events. Requires matching ``claim_token``."""
    result = run_with_db_retry(
        lambda conn: do_ack(conn, [r.model_dump() for r in body.results])
    )
    return AckResponse(**result)


@router.get("", summary="Query events (read-only)")
def list_events(
    status: str | None = Query(None, description="Filter by status"),
    repo: str | None = Query(None, description="Filter by repo_full_name"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List events for debugging / inspection."""
    conn = get_connection()
    try:
        events = query_events(conn, status_filter=status, repo=repo, limit=limit, offset=offset)
    finally:
        conn.close()
    return {"events": events, "count": len(events)}


@router.get("/by-commit/{commit_sha}", summary="Query task by commit SHA")
def get_by_commit(commit_sha: str):
    """Return the work event associated with *commit_sha*.

    Searches only work-type events (issues, pull_request, push,
    issue_comment).  Returns 404 when no task is linked to the commit.
    """
    conn = get_connection()
    try:
        ev = query_by_commit(conn, commit_sha)
    finally:
        conn.close()
    if ev is None:
        raise HTTPException(status_code=404, detail="no task found for commit")
    return ev


@router.patch("/{event_id}", summary="Partially update an event")
def patch_one_event(event_id: int, body: EventPatchRequest):
    """Update specific fields of an event.  Requires *claim_token* for
    ownership verification.  Only allowed fields are written; others are
    silently ignored.
    """
    conn = get_connection()
    try:
        result = patch_event(conn, event_id, body.model_dump())
    finally:
        conn.close()
    if result is None:
        raise HTTPException(status_code=404, detail="event not found or token mismatch")
    return result


@router.get("/{event_id}", summary="Get single event")
def get_one_event(event_id: int):
    """Return a single event by id."""
    conn = get_connection()
    try:
        ev = get_event(conn, event_id)
    finally:
        conn.close()
    if ev is None:
        raise HTTPException(status_code=404, detail="event not found")
    return ev


@router.post("/{event_id}/heartbeat", summary="Refresh the claim lease of a processing event")
def heartbeat_event(event_id: int, body: HeartbeatRequest):
    """Refresh ``claimed_at`` so a long-running agent does not hit the
    TTL requeue.  404 when the event is not found, not processing, or
    the claim_token does not match (Hola-Infra#43)."""
    ok = run_with_db_retry(
        lambda conn: heartbeat_repo(conn, event_id, body.claim_token)
    )
    if not ok:
        raise HTTPException(status_code=404, detail="event not found, not processing, or token mismatch")
    return {"status": "ok"}
