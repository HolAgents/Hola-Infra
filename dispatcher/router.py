"""Event router — decide whether and how to dispatch an event."""

from __future__ import annotations

import logging
import re
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
    item_number: int | None = None  # issue/PR number of the original task (workspace path)
    claim_token: str = ""     # the ORIGINAL event's claim token (for patch ownership)


@dataclass
class SyncTask:
    """State sync for an EXISTING task — no agent dispatch, the puller
    patches the original task event instead (M2/M3/M4)."""
    task_status: str                       # planned | pr_opened | ci_passed | done | released | blocked | failed | ...
    commit_sha: str | None = None
    item_number: int | None = None         # override when derived from branch name
    event_id: int | None = None            # direct target event (by-commit lookups)
    claim_token: str = ""                  # original event's claim token for direct targets


@dataclass
class RouteDecision:
    should_dispatch: bool
    agent_type: str = ""
    reason: str = ""
    skip_ack: bool = False    # True → ack as "completed" immediately
    resume_context: ResumeContext | None = None  # CI resume only
    sync_task: SyncTask | None = None             # state sync (no dispatch)
    ocr_trigger: int | None = None                # PR number of an OCR review comment (M3)


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
            pr = payload.get("pull_request") or {}
            branch = (pr.get("head") or {}).get("ref", "")
            m = re.match(r"hola/issue-(\d+)", branch)
            if m:
                # An agent's own PR for a known task — sync state instead
                # of dispatching another agent at it.
                head_sha = (pr.get("head") or {}).get("sha", "")
                return RouteDecision(
                    False,
                    reason=f"agent PR for issue {m.group(1)} — sync task state",
                    skip_ack=True,
                    sync_task=SyncTask(
                        "pr_opened",
                        commit_sha=head_sha or None,
                        item_number=int(m.group(1)),
                    ),
                )
            return RouteDecision(True, agent_type="review", reason="PR opened")
        if action == "closed":
            pr = payload.get("pull_request") or {}
            branch = (pr.get("head") or {}).get("ref", "")
            m = re.match(r"hola/issue-(\d+)", branch)
            merged = bool(pr.get("merged", False))
            if m:
                # M4: release the workspace on PR close; merged → done,
                # closed-unmerged → released (issue stays open).
                status = "done" if merged else "released"
                return RouteDecision(
                    False,
                    reason=f"agent PR closed (merged={merged}) — release workspace",
                    skip_ack=True,
                    sync_task=SyncTask(status, item_number=int(m.group(1))),
                )
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
        if action == "created":
            if comment_body.lstrip().startswith("<!-- hola-plan -->"):
                return RouteDecision(
                    False,
                    reason="plan comment — sync task state",
                    skip_ack=True,
                    sync_task=SyncTask("planned"),
                )
            if comment_body.lstrip().startswith("<!-- hola-blocked -->"):
                # M4: agent reported itself blocked — workspace is kept.
                return RouteDecision(
                    False,
                    reason="blocked comment — sync task state",
                    skip_ack=True,
                    sync_task=SyncTask("blocked"),
                )
            if sender == "github-actions[bot]" and "<!-- ocr-" in comment_body:
                # OpenCodeReview feedback — the puller resolves the PR's
                # head sha and resumes the original task (M3).
                pr_number = (payload.get("issue") or {}).get("number")
                if pr_number:
                    return RouteDecision(
                        False,
                        reason="OCR review comment — resume original task",
                        skip_ack=True,
                        ocr_trigger=pr_number,
                    )
            if _is_mention_or_command(comment_body):
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

    # commit SHA extraction: check_run carries a TOP-LEVEL head_sha;
    # workflow_run nests head_commit.id (the old code read only the
    # latter, so check_run failures always fell through to orphan).
    commit_sha = ci_obj.get("head_sha") or (ci_obj.get("head_commit") or {}).get("id", "")

    if conclusion == "success" and commit_sha and fc_client is not None:
        # CI green for a known commit → mark the original task ci_passed.
        task = fc_client.query_by_commit(commit_sha)
        if task and task.get("session_id"):
            return RouteDecision(
                False,
                reason=f"{event_type} success — sync ci_passed",
                skip_ack=True,
                sync_task=SyncTask("ci_passed", event_id=task["id"]),
            )

    if conclusion not in ("failure", "cancelled", "timed_out"):
        return RouteDecision(False, reason=f"{event_type} ok ({conclusion})", skip_ack=True)

    # ---- CI failure — try to find original task ----
    if commit_sha and fc_client is not None:
        task = fc_client.query_by_commit(commit_sha)
        if task and task.get("session_id"):
            task_payload = task.get("payload") or {}
            item_number = (
                (task_payload.get("issue") or {}).get("number")
                or (task_payload.get("pull_request") or {}).get("number")
            )
            # M4: iteration cap — repeated CI failures on this trigger
            # escalate to human instead of resuming forever.
            settings = get_settings()
            if event.get("retry_count", 0) >= settings.max_ci_resumes:
                return RouteDecision(
                    False,
                    reason=f"CI retries exhausted ({event.get('retry_count')}) — escalation",
                    skip_ack=True,
                    sync_task=SyncTask(
                        "failed",
                        event_id=task["id"],
                        claim_token=task.get("claim_token") or "",
                    ),
                )
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
                    item_number=item_number,
                    claim_token=task.get("claim_token") or "",
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
