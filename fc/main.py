"""Hola Infra — FC Webhook Service.

FastAPI application entry point for Alibaba Cloud Function Compute.
"""

import logging

from fastapi import FastAPI

from app.api import webhooks, events, health
from app.config import get_settings

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

app = FastAPI(
    title="Hola Webhook Receiver",
    description="GitHub webhook receiver + event ledger for the Hola agent dispatch pipeline.",
    version="0.1.0",
)

# Mount routers
app.include_router(webhooks.router)
app.include_router(events.router)
app.include_router(health.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=9000, reload=True)
