"""GET /healthz — liveness + DB reachability check."""

import socket

from fastapi import APIRouter

from app.db import get_connection
from app.models import HealthResponse

router = APIRouter(tags=["health"])


def _vpc_context() -> str:
    """Best-effort view of the instance's VPC attachment: the source IP a
    connection to the DB host would use, plus the resolved address.  An
    unroutable source means the instance has no usable VPC path (the
    vpcConfig ENI never attached) — the classic silent-drop timeout.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.database_url:
        return "no-database-url"
    try:
        from urllib.parse import urlparse

        host = urlparse(settings.database_url).hostname or ""
        ip = socket.gethostbyname(host)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((ip, 5432))  # no traffic — route resolution only
            src = probe.getsockname()[0]
        except OSError as exc:
            src = f"unroutable({exc.errno})"
        finally:
            probe.close()
        return f"host={host} resolved={ip} src={src}"
    except Exception as exc:  # noqa: BLE001 — diagnostics must never hide db status
        return f"error: {exc}"


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

    return HealthResponse(status="ok", db=db_status, net=_vpc_context())
