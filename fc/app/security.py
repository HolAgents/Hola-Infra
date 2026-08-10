"""Security utilities — GitHub webhook signature verification & API key auth."""

import hashlib
import hmac
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from fc.app.config import get_settings


# ---------------------------------------------------------------------------
# GitHub HMAC-SHA256
# ---------------------------------------------------------------------------

def verify_github_signature(body: bytes, signature_header: Optional[str]) -> bool:
    """Constant-time comparison against the shared webhook secret.

    Returns ``True`` when the *signature_header* (``X-Hub-Signature-256``)
    matches ``sha256=HMAC(secret, body)``.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    secret = get_settings().github_webhook_secret
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def validate_webhook_headers(
    event: Optional[str] = Header(None, alias="X-GitHub-Event"),
    delivery_id: Optional[str] = Header(None, alias="X-GitHub-Delivery"),
    signature: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
) -> dict[str, str]:
    """Return ``{event, delivery_id, signature}`` or raise 400."""
    if not event or not delivery_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required headers: X-GitHub-Event, X-GitHub-Delivery",
        )
    return {"event": event, "delivery_id": delivery_id, "signature": signature}


# ---------------------------------------------------------------------------
# API Key (dispatcher ↔ fc)
# ---------------------------------------------------------------------------

async def require_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> None:
    """FastAPI dependency — constant-time comparison of ``X-API-Key`` header."""
    expected = get_settings().api_key
    if not x_api_key or not hmac.compare_digest(expected, x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
