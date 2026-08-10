"""Claim / ack / requeue orchestration.

Thin service layer that calls *repository* functions and shapes HTTP responses.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.repository import claim_batch, ack_batch, query_events, get_event

logger = logging.getLogger(__name__)


def do_claim(conn, limit: int, event_types: Optional[list[str]] = None) -> dict:
    """Claim a batch of ``received`` events.

    Return shape::

        {"count": N, "events": [...], "remaining": M}
    """
    claimed, remaining = claim_batch(conn, limit=limit, event_types=event_types)
    logger.info("claimed %d events, %d remaining", len(claimed), remaining)
    return {
        "count": len(claimed),
        "events": claimed,
        "remaining": remaining,
    }


def do_ack(conn, results: list[dict]) -> dict:
    """Ack a batch of results.

    Return shape::

        {"acked": N, "rejected": [...]}
    """
    out = ack_batch(conn, results)
    logger.info("ack: %d acked, %d rejected", out["acked"], len(out["rejected"]))
    return out
