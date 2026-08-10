"""Event router — decide whether and how to dispatch an event."""

from __future__ import annotations

import logging
from typing import Any, Optional

from dispatcher.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Route decision result
# ---------------------------------------------------------------------------


class RouteDecision:
    __slots__ = ("should_dispatch", "agent_type", "reason", "skip_ack")

    def __init__(
        self,
        should_dispatch: bool,
        agent_type: str = "",
        reason: str = "",
        skip_ack: bool = False,
    ) -> None:
        self.should_dispatch = should_dispatch
        self.agent_type = agent_type
        self.reason = reason
        self.skip_ack = skip_ack  # True → ack as "completed" immediately


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

def route(event: dict[str, Any]) -> RouteDecision:
    """Determine dispatch action for a single webhook event.

    Decision rules:
        - Bot sender → skip, ack completed
        - Issues opened → triage agent
        - PR opened → review agent
        - Push → quality agent
        - Issue comment → response agent (if mention)
        - Everything else → skip
    """
    settings = get_settings()
    skip_senders = {s.strip() for s in settings.agent_skip_senders.split(",") if s.strip()}
    sender = _extract_sender(event)

    event_type = event.get("event_type", "")
    payload = event.get("payload", {})

    # ---- bot filter ----
    if sender and sender in skip_senders:
        return RouteDecision(
            should_dispatch=False,
            reason=f"sender {sender} is bot",
            skip_ack=True,  # ack immediately as completed
        )

    # ---- issues ----
    if event_type == "issues":
        action = payload.get("action", "")
        if action == "opened":
            return RouteDecision(True, agent_type="triage", reason="issue opened")
        if action == "closed":
            return RouteDecision(False, reason="issue closed — no action needed", skip_ack=True)
        return RouteDecision(False, reason=f"issue {action} — ignored", skip_ack=True)

    # ---- pull_request ----
    if event_type == "pull_request":
        action = payload.get("action", "")
        if action == "opened":
            return RouteDecision(True, agent_type="review", reason="PR opened")
        if action == "closed":
            return RouteDecision(False, reason="PR closed — no action needed", skip_ack=True)
        return RouteDecision(False, reason=f"PR {action} — ignored", skip_ack=True)

    # ---- push ----
    if event_type == "push":
        if sender and sender not in skip_senders:
            return RouteDecision(True, agent_type="quality", reason="push by human")
        return RouteDecision(False, reason="push by bot", skip_ack=True)

    # ---- issue_comment ----
    if event_type == "issue_comment":
        action = payload.get("action", "")
        comment_body = payload.get("comment", {}).get("body", "")
        if action == "created" and _is_mention_or_command(comment_body):
            return RouteDecision(True, agent_type="response", reason="mention/command")
        return RouteDecision(False, reason="comment — no mention", skip_ack=True)

    # ---- fallback ----
    return RouteDecision(False, reason=f"unhandled event type: {event_type}", skip_ack=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_sender(event: dict) -> Optional[str]:
    payload = event.get("payload", {})
    return payload.get("sender", {}).get("login")


def _is_mention_or_command(body: str) -> bool:
    """Check if the comment contains @hola-bot or a slash command."""
    if not body:
        return False
    body_lower = body.lower()
    return "@hola-bot" in body_lower or body_lower.startswith("/")
