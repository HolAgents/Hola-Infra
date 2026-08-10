"""Webhook ingest pipeline — verify → filter → store."""

from __future__ import annotations

import logging
from typing import Optional

from fc.app.config import get_settings
from fc.app.repository import insert_event

logger = logging.getLogger(__name__)


def _is_allowed(repo: str, event_type: str) -> bool:
    """Check repo and event_type against the allow-list config."""
    settings = get_settings()
    if settings.allowed_repos:
        allowed_repos = {r.strip() for r in settings.allowed_repos.split(",") if r.strip()}
        if repo not in allowed_repos:
            logger.info("repo %s not in allowed_repos list", repo)
            return False
    if settings.allowed_events:
        allowed_events = {e.strip() for e in settings.allowed_events.split(",") if e.strip()}
        if event_type not in allowed_events:
            logger.info("event_type %s not in allowed_events list", event_type)
            return False
    return True


def handle_webhook(
    conn,
    delivery_id: str,
    event_type: str,
    repo_full_name: str,
    payload: dict,
    signature: str,
) -> dict:
    """Run the full ingest pipeline and return a result dict.

    Return shape::

        {"status": "accepted"|"duplicate"|"filtered",
         "delivery_id": ..., "event_id": ... | None}
    """
    # 1. Filter
    if not _is_allowed(repo_full_name, event_type):
        return {"status": "filtered", "delivery_id": delivery_id, "event_id": None}

    # 2. Store (idempotent via delivery_id unique index)
    row = insert_event(conn, delivery_id, event_type, repo_full_name, payload, signature)

    if row is None:
        logger.debug("duplicate delivery_id=%s", delivery_id)
        return {"status": "duplicate", "delivery_id": delivery_id, "event_id": None}

    logger.info(
        "ingested event_id=%d type=%s repo=%s delivery=%s",
        row["id"], event_type, repo_full_name, delivery_id,
    )
    return {
        "status": "accepted",
        "delivery_id": delivery_id,
        "event_id": row["id"],
    }
