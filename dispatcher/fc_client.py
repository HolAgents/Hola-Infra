"""HTTP client for the FC Webhook Service API."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from dispatcher.config import get_settings

logger = logging.getLogger(__name__)


class FCClient:
    """Thin wrapper around the FC API endpoints used by the dispatcher."""

    def __init__(self) -> None:
        settings = get_settings()
        self._base = settings.fc_base_url.rstrip("/")
        self._api_key = settings.api_key
        self._client = httpx.Client(
            headers={"X-API-Key": self._api_key},
            timeout=30,
        )

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------

    def claim(
        self,
        limit: int = 20,
        event_types: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """POST /api/events/claim — atomically claim received events."""
        body: dict[str, Any] = {"limit": limit}
        if event_types:
            body["event_types"] = event_types

        resp = self._client.post(f"{self._base}/api/events/claim", json=body)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Ack
    # ------------------------------------------------------------------

    def ack(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /api/events/ack — report results for claimed events."""
        resp = self._client.post(
            f"{self._base}/api/events/ack",
            json={"results": results},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("rejected"):
            logger.warning("ack rejected: %s", data["rejected"])
        return data

    # ------------------------------------------------------------------
    # Query (for dedup checks)
    # ------------------------------------------------------------------

    def query_events(
        self,
        status: str = "",
        repo: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        """GET /api/events?status=... — list events (status optional)."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if repo:
            params["repo"] = repo
        resp = self._client.get(f"{self._base}/api/events", params=params)
        resp.raise_for_status()
        return resp.json().get("events", [])

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> bool:
        """GET /healthz — return True when FC is reachable."""
        try:
            resp = self._client.get(f"{self._base}/healthz", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Query by commit SHA (CI resume)
    # ------------------------------------------------------------------

    def query_by_commit(self, commit_sha: str) -> dict[str, Any] | None:
        """GET /api/events/by-commit/{sha} — find task by commit SHA."""
        try:
            resp = self._client.get(
                f"{self._base}/api/events/by-commit/{commit_sha}"
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Partial update (CI resume)
    # ------------------------------------------------------------------

    def patch_event(self, event_id: int, patch: dict[str, Any]) -> bool:
        """PATCH /api/events/{id} — partially update an event."""
        try:
            resp = self._client.patch(
                f"{self._base}/api/events/{event_id}", json=patch
            )
            return resp.status_code == 200
        except Exception:
            return False

    def close(self) -> None:
        self._client.close()
