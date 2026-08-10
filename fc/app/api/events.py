"""POST /api/events/claim, POST /api/events/ack, GET /api/events."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from fc.app.db import get_connection
from fc.app.models import (
    AckRequest,
    AckResponse,
    ClaimRequest,
    ClaimResponse,
)
from fc.app.security import require_api_key
from fc.app.services.claiming import do_ack, do_claim
from fc.app.repository import query_events, get_event

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
    conn = get_connection()
    try:
        result = do_claim(conn, limit=body.limit, event_types=body.event_types)
    finally:
        conn.close()
    return ClaimResponse(**result)


@router.post("/ack", response_model=AckResponse, summary="Acknowledge processed events")
def ack(body: AckRequest):
    """Report results for claimed events. Requires matching ``claim_token``."""
    conn = get_connection()
    try:
        result = do_ack(conn, [r.model_dump() for r in body.results])
    finally:
        conn.close()
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
