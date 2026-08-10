"""GET /healthz — liveness + DB reachability check."""

from fastapi import APIRouter

from app.db import get_connection
from app.models import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse, summary="Health check")
def healthz():
    """Return service health including database connectivity."""
    try:
        conn = get_connection()
        conn.execute("SELECT 1")
        conn.close()
        db_status = "connected"
    except Exception as exc:
        db_status = f"error: {exc}"

    return HealthResponse(status="ok", db=db_status)
