"""FastAPI dependencies."""

from fastapi import Depends, Request

from app.security import require_api_key


async def get_raw_body(request: Request) -> bytes:
    """Return the raw request body bytes (used for HMAC verification)."""
    return await request.body()
