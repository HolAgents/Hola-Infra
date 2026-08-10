"""POST /api/webhooks/github — receive GitHub webhook events."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from fc.app.db import get_connection
from fc.app.models import IngestResponse
from fc.app.security import validate_webhook_headers, verify_github_signature
from fc.app.services.ingest import handle_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post(
    "/github",
    response_model=IngestResponse,
    status_code=202,
    summary="Receive a GitHub webhook event",
)
async def github_webhook(request: Request):
    """Verify the HMAC-SHA256 signature, filter, and store the event."""
    # Read raw body before any JSON parsing (HMAC input)
    body = await request.body()

    # Validate required headers
    headers = validate_webhook_headers(
        request.headers.get("X-GitHub-Event"),
        request.headers.get("X-GitHub-Delivery"),
        request.headers.get("X-Hub-Signature-256"),
    )

    # Verify signature
    if not verify_github_signature(body, headers["signature"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature",
        )

    # Parse JSON body
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    # Extract repo
    repo = payload.get("repository", {}).get("full_name", "")

    conn = get_connection()
    try:
        result = handle_webhook(
            conn,
            delivery_id=headers["delivery_id"],
            event_type=headers["event"],
            repo_full_name=repo,
            payload=payload,
            signature=headers["signature"] or "",
        )
    finally:
        conn.close()

    if result["status"] == "filtered":
        return IngestResponse(status="filtered", delivery_id=result["delivery_id"])

    if result["status"] == "duplicate":
        return JSONResponse(
            status_code=200,
            content=IngestResponse(status="duplicate", delivery_id=result["delivery_id"]).model_dump(),
        )

    return JSONResponse(
        status_code=201,
        content=IngestResponse(
            status="accepted",
            delivery_id=result["delivery_id"],
            event_id=result["event_id"],
        ).model_dump(),
    )
