"""Hola Infra — FC Webhook Service.

FastAPI application entry point for Alibaba Cloud Function Compute.
"""

import logging
import sqlite3
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import admin, webhooks, events, health
from app.config import get_settings

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("hola.main")

app = FastAPI(
    title="Hola Webhook Receiver",
    description="GitHub webhook receiver + event ledger for the Hola agent dispatch pipeline.",
    version="0.1.0",
)

# Mount routers
app.include_router(webhooks.router)
app.include_router(events.router)
app.include_router(health.router)
app.include_router(admin.router)


# ---------------------------------------------------------------------------
# Observability: access log + traceback logging
# ---------------------------------------------------------------------------


@app.middleware("http")
async def access_log(request: Request, call_next):
    """Log every request: method, path, status, duration, GitHub delivery/event."""
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s status=%d dur=%.1fms delivery=%s event=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        request.headers.get("x-github-delivery", "-"),
        request.headers.get("x-github-event", "-"),
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Log the full traceback before returning 500 — the FC console otherwise
    hides it behind a bare 'Internal Server Error'."""
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


# Startup diagnostics — the runtime's SQLite lib version matters (e.g. the
# RETURNING clause requires >= 3.35; the FC image ships an older one).
logger.info(
    "startup: sqlite_lib_version=%s db_path=%s log_level=%s",
    sqlite3.sqlite_version,
    settings.db_path,
    settings.log_level,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=9000, reload=True)
