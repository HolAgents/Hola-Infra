"""Event router — decide whether and how to dispatch an event."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from dispatcher.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Route decision result
# ---------------------------------------------------------------------------


@dataclass
class ResumeContext:
    """Populated when a CI failure is linked to a previous task."""
    identity_name: str
    target_id: str
    session_id: str
    commit_sha: str           # CI-failing commit
    error_message: str        # human-readable CI error
    original_event_id: int    # event_id of the original task


@dataclass
class RouteDecision:
    should_dispatch: bool
    agent_type: str = ""
    reason: str = ""
    skip_ack: bool = False    # True → ack as "completed" immediately
    resume_context: ResumeContext | None = None  # CI resume only


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

def route(event: dict[str, Any], fc_client=None) -> RouteDecision:
    """Determine dispatch action for a single webhook event.

    Decision rules:
        - Bot sender → skip, ack completed
        - Issues opened → triage agent
        - PR opened → review agent
        - Push → quality agent
        - Issue comment → response agent (if mention)
        - workflow_run / check_run failure → resume (if commit linked) or triage
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
            skip_ack=True,
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

    # ---- workflow_run (CI/CD) ----
    if event_type == "workflow_run":
        return _route_ci_event(event, fc_client)

    # ---- check_run (CI/CD) ----
    if event_type == "check_run":
        return _route_ci_event(event, fc_client)

    # ---- fallback ----
    return RouteDecision(False, reason=f"unhandled event type: {event_type}", skip_ack=True)


# ---------------------------------------------------------------------------
# CI event routing
# ---------------------------------------------------------------------------

def _route_ci_event(event: dict[str, Any], fc_client=None) -> RouteDecision:
    """Route CI (workflow_run / check_run) events.

    On failure with a known commit, look up the original task and route
    to ``resume``.  Otherwise treat as an orphan and dispatch a fresh
    triage agent.
    """
    payload = event.get("payload", {})
    event_type = event.get("event_type", "")

    # CI payload: workflow_run or check_run
    ci_obj = payload.get("workflow_run") or payload.get("check_run") or {}
    action = payload.get("action") or ci_obj.get("action", "")
    conclusion = ci_obj.get("conclusion", "")
    name = ci_obj.get("name", "unknown")

    if action != "completed":
        return RouteDecision(False, reason=f"{event_type} {action} — ignored", skip_ack=True)

    if conclusion not in ("failure", "cancelled", "timed_out"):
        return RouteDecision(False, reason=f"{event_type} ok ({conclusion})", skip_ack=True)

    # ---- CI failure — try to find original task ----
    head_commit = ci_obj.get("head_commit", {})
    commit_sha = head_commit.get("id", "")

    if commit_sha and fc_client is not None:
        task = fc_client.query_by_commit(commit_sha)
        if task and task.get("session_id"):
            return RouteDecision(
                should_dispatch=True,
                agent_type="resume",
                reason=f"CI failed: {name}, resume {task['session_id'][:12]}",
                resume_context=ResumeContext(
                    identity_name=task["identity_name"],
                    target_id=task["target_id"],
                    session_id=task["session_id"],
                    commit_sha=commit_sha,
                    error_message=_extract_ci_error(ci_obj, event_type),
                    original_event_id=task["id"],
                ),
            )

    # Orphan CI failure — no linked task
    return RouteDecision(
        True, agent_type="triage",
        reason=f"orphan CI failure: {name}",
    )


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


def _extract_ci_error(ci_obj: dict, event_type: str) -> str:
    """Build a human-readable CI error message."""
    name = ci_obj.get("name", "unknown")
    conclusion = ci_obj.get("conclusion", "failure")
    url = ci_obj.get("html_url", "")
    return (
        f"{event_type} '{name}' {conclusion}"
        + (f" — {url}" if url else "")
    )
