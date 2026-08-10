"""Poll loop — claim events from FC, dispatch to agents, ack results."""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

from dispatcher.config import get_settings
from dispatcher.fc_client import FCClient
from dispatcher.kanban import KanbanClient
from dispatcher.router import RouteDecision, route

logger = logging.getLogger(__name__)


def _extract_node_id(event: dict[str, Any]) -> str | None:
    """Try to get the GitHub GraphQL node_id for the issue/PR from the payload."""
    payload = event.get("payload", {})
    # issue
    node = payload.get("issue", {}).get("node_id")
    if node:
        return node
    # pull request
    node = payload.get("pull_request", {}).get("node_id")
    if node:
        return node
    return None


def _run_agent(event: dict[str, Any], agent_type: str) -> dict[str, Any]:
    """Launch Claude Code as a subprocess to handle the event.

    Returns a result dict with ``status`` and ``message``.
    """
    payload = event.get("payload", {})
    repo = event.get("repo_full_name", "")
    event_type = event.get("event_type", "")

    # Build a concise prompt for Claude Code
    title = (
        payload.get("issue", {}).get("title", "")
        or payload.get("pull_request", {}).get("title", "")
        or ""
    )
    body = (
        payload.get("issue", {}).get("body", "")
        or payload.get("pull_request", {}).get("body", "")
        or ""
    )

    prompt = (
        f"Handle this GitHub event in repo {repo}.\n"
        f"Event: {event_type}\n"
        f"Title: {title}\n"
        f"Body: {body[:2000]}\n\n"  # truncate for context
        f"Your agent type is: {agent_type}.\n"
        f"Use gh CLI to interact with GitHub as needed."
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=600,  # 10 min per task
            cwd=None,     # uses current directory
        )
        if result.returncode == 0:
            return {"status": "completed", "message": result.stdout[:500]}
        else:
            return {"status": "failed", "message": result.stderr[:500]}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "message": "agent timeout (10 min)"}
    except FileNotFoundError:
        return {"status": "failed", "message": "claude CLI not found"}
    except Exception as exc:
        return {"status": "failed", "message": str(exc)}


def _ack_skip(event: dict[str, Any], decision: RouteDecision, fc: FCClient) -> None:
    """Ack as completed for events we intentionally skip."""
    fc.ack([{
        "event_id": event["id"],
        "claim_token": event["claim_token"],
        "status": "completed",
        "message": f"skipped: {decision.reason}",
    }])


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_loop() -> None:
    """Run the dispatcher poll loop (blocking)."""
    settings = get_settings()
    fc = FCClient()
    kanban = KanbanClient()

    logger.info("dispatcher started — poll interval %ds, batch size %d",
                settings.poll_interval_seconds, settings.batch_size)

    try:
        while True:
            try:
                claimed = fc.claim(limit=settings.batch_size)
            except Exception as exc:
                logger.error("claim failed: %s", exc)
                time.sleep(settings.poll_interval_seconds)
                continue

            events = claimed.get("events", [])
            if not events:
                time.sleep(settings.poll_interval_seconds)
                continue

            logger.info("claimed %d events", len(events))

            ack_results: list[dict[str, Any]] = []

            for event in events:
                decision: RouteDecision = route(event)
                node_id = _extract_node_id(event)

                logger.debug(
                    "event %d, type=%s, dispatch=%s, reason=%s",
                    event["id"], event["event_type"], decision.should_dispatch, decision.reason,
                )

                if decision.skip_ack:
                    # Move to Done on Kanban
                    if node_id:
                        kanban.move_card(node_id, "Done")
                    ack_results.append({
                        "event_id": event["id"],
                        "claim_token": event["claim_token"],
                        "status": "completed",
                        "message": decision.reason,
                    })
                    continue

                if not decision.should_dispatch:
                    # Still move to Done — no action needed
                    if node_id:
                        kanban.move_card(node_id, "Done")
                    ack_results.append({
                        "event_id": event["id"],
                        "claim_token": event["claim_token"],
                        "status": "completed",
                        "message": decision.reason,
                    })
                    continue

                # ---- dispatch ----
                # 1. Kanban → In progress
                if node_id:
                    kanban.move_card(node_id, "In progress")

                # 2. Run agent
                result = _run_agent(event, decision.agent_type)

                # 3. Kanban → In review (done working) / Done (failed)
                if node_id:
                    if result["status"] == "completed":
                        kanban.move_card(node_id, "In review")
                    else:
                        kanban.move_card(node_id, "Done")

                ack_results.append({
                    "event_id": event["id"],
                    "claim_token": event["claim_token"],
                    "status": result["status"],
                    "agent_id": decision.agent_type,
                    "message": result.get("message", ""),
                })

            # Batch ack
            if ack_results:
                try:
                    fc.ack(ack_results)
                except Exception as exc:
                    logger.error("ack failed: %s", exc)

    except KeyboardInterrupt:
        logger.info("dispatcher shutting down")
    finally:
        fc.close()
        kanban.close()
