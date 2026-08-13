"""Poll loop — claim events from FC, dispatch to agents, ack results."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from dispatcher.config import get_settings
from dispatcher.fc_client import FCClient
from dispatcher.kanban import KanbanClient
from dispatcher.identity import resolve, get_current_target
from dispatcher.router import ResumeContext, RouteDecision, route

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


def _run_agent(
    event: dict[str, Any],
    agent_type: str,
    identity: object | None,
    custom_prompt: str | None = None,
) -> dict[str, Any]:
    """Launch Claude Code as a subprocess to handle the event.

    A UUID session ID is pre-generated so the dispatcher knows it
    upfront — no need to parse it from Claude's output.  The agent is
    instructed to emit a ``##HOLA_RESULT`` marker when it commits; the
    dispatcher parses that for the commit SHA.

    Returns a result dict with *status*, *message*, *session_id*,
    *commit_sha*, *identity_name*, and *target_id*.
    """
    payload = event.get("payload", {})
    repo = event.get("repo_full_name", "")
    event_type = event.get("event_type", "")

    # ---- pre-generate session ID ----
    session_id = str(uuid.uuid4())

    # ---- build prompt ----
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

    identity_info = ""
    if identity:
        identity_info = (
            f"Your agent identity:\n"
            f"  - Agent ID: {identity.agent_id}\n"
            f"  - Name: {identity.display_name}\n"
            f"  - GitHub user: {identity.gh_user}\n"
            f"  - Agent type: {identity.agent_type}\n\n"
        )

    prompt = custom_prompt or (
        f"Handle this GitHub event in repo {repo}.\n"
        f"Event: {event_type}\n"
        f"Title: {title}\n"
        f"Body: {body[:2000]}\n\n"
        f"{identity_info}"
        f"Use gh CLI to interact with GitHub as needed.\n\n"
        f"IMPORTANT: When you are done (code committed and pushed), "
        f"output this exact tag as your last message:\n"
        f"<CommitSha>REPLACE_WITH_FULL_40_CHAR_SHA</CommitSha>"
    )

    ident_name = identity.identity_name if identity else None
    ident_target = identity.target_id if identity else None

    try:
        result = subprocess.run(
            ["claude", "--session-id", session_id,
             "--name", f"task-{event['id']}",
             "-p", prompt],
            capture_output=True, text=True, timeout=600,
        )
        commit_sha = _parse_commit_marker(result.stdout)

        if result.returncode == 0:
            return {
                "status": "completed",
                "message": result.stdout[:500],
                "session_id": session_id,
                "commit_sha": commit_sha,
                "identity_name": ident_name,
                "target_id": ident_target,
            }
        else:
            return {
                "status": "failed",
                "message": result.stderr[:500],
                "session_id": session_id,
                "commit_sha": commit_sha,
                "identity_name": ident_name,
                "target_id": ident_target,
            }
    except subprocess.TimeoutExpired:
        return {"status": "failed", "message": "agent timeout (10 min)",
                "session_id": session_id}
    except FileNotFoundError:
        return {"status": "failed", "message": "claude CLI not found",
                "session_id": session_id}
    except Exception as exc:
        return {"status": "failed", "message": str(exc),
                "session_id": session_id}


def _parse_commit_marker(stdout: str) -> str | None:
    """Parse the commit SHA from agent output.

    Accepts both the current ``<CommitSha>...</CommitSha>`` tag (the
    format the runbook skills instruct — Hola-Skill#1) and the legacy
    ``##HOLA_RESULT:{"commit_sha":"..."}##`` marker, which is removed
    after a transition release (Hola-Infra#31).
    """
    # New tag format (preferred)
    m = re.search(r"<CommitSha>\s*([0-9a-fA-F]{40})\s*</CommitSha>", stdout)
    if m:
        return m.group(1)
    # Legacy format
    m = re.search(r"##HOLA_RESULT:\s*(\{[^}]+\})\s*##", stdout)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return data.get("commit_sha")
    except json.JSONDecodeError:
        return None


def _ack_skip(event: dict[str, Any], decision: RouteDecision, fc: FCClient) -> None:
    """Ack as completed for events we intentionally skip."""
    fc.ack([{
        "event_id": event["id"],
        "claim_token": event["claim_token"],
        "status": "completed",
        "message": f"skipped: {decision.reason}",
    }])


# ---------------------------------------------------------------------------
# Resume dispatch (CI failure → agent resume)
# ---------------------------------------------------------------------------


def _dispatch(
    event: dict[str, Any],
    decision: RouteDecision,
    fc: FCClient,
) -> dict[str, Any]:
    """Execute a dispatch decision.  Returns an ack-compatible result dict."""
    repo = event.get("repo_full_name", "")

    # ---- ordinary dispatch ----
    if decision.agent_type != "resume":
        identity = resolve(decision.agent_type, repo)
        if identity is None:
            return {
                "status": "failed",
                "message": f"no identity for {decision.agent_type}",
            }
        return _run_agent(event, decision.agent_type, identity)

    # ---- resume dispatch ----
    ctx = decision.resume_context
    if ctx is None:
        return {
            "status": "failed",
            "message": "resume agent_type without resume_context",
        }

    # 1. detect re-bind
    current_target = get_current_target(ctx.identity_name)
    target_changed = (current_target != ctx.target_id)
    session_available = _check_session_exists(ctx.target_id, ctx.session_id)

    # 2. choose strategy
    if not target_changed and session_available:
        logger.info("Hot resume: target=%s session=%s", ctx.target_id, ctx.session_id[:12])
        result = _resume_agent(ctx, event)
    else:
        reason = "target changed" if target_changed else "session not found"
        logger.info(
            "Cold start: %s (was %s, now %s)",
            reason, ctx.target_id, current_target,
        )
        identity = resolve("triage", repo)
        result = _cold_start_agent(ctx, identity, event)

    # 3. update original task event
    if result.get("status") == "completed":
        new_sha = result.get("commit_sha", "")
        fc.patch_event(ctx.original_event_id, {
            "claim_token": event["claim_token"],
            "task_status": "pushed",
            "commit_sha": new_sha or ctx.commit_sha,
        })

    return result


def _resume_agent(ctx: ResumeContext, event: dict[str, Any]) -> dict[str, Any]:
    """Hot resume: ``claude --resume <session_id> -p '…'``."""
    prompt = (
        f"CI workflow failed for commit {ctx.commit_sha[:7]}.\n"
        f"Error: {ctx.error_message}\n\n"
        f"Fix the issue, commit your changes, and push.\n"
        f"IMPORTANT: When done, output this exact tag as your last message:\n"
        f"<CommitSha>REPLACE_WITH_FULL_40_CHAR_SHA</CommitSha>"
    )
    try:
        result = subprocess.run(
            ["claude", "--resume", ctx.session_id, "-p", prompt],
            capture_output=True, text=True, timeout=600,
        )
        commit_sha = _parse_commit_marker(result.stdout)
        if result.returncode == 0:
            return {
                "status": "completed",
                "message": "fixed",
                "commit_sha": commit_sha,
                "session_id": ctx.session_id,
            }
        return {"status": "failed", "message": result.stderr[:500]}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "message": "resume agent timeout"}
    except FileNotFoundError:
        return {"status": "failed", "message": "claude CLI not found"}


def _cold_start_agent(
    ctx: ResumeContext,
    identity: object | None,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Cold start: full context via prompt for a fresh agent."""
    identity_name = (
        identity.identity_name if identity else ctx.identity_name
    )
    prompt = (
        f"You are acting as identity '{identity_name}'.\n"
        f"Previously, this identity committed {ctx.commit_sha}, "
        f"which caused a CI failure.\n"
        f"CI Error: {ctx.error_message}\n\n"
        f"Fix the issue, commit, and push. "
        f"The commit author should remain '{identity_name}'.\n"
        f"IMPORTANT: When done, output this exact tag as your last message:\n"
        f"<CommitSha>REPLACE_WITH_FULL_40_CHAR_SHA</CommitSha>"
    )
    return _run_agent(event, "triage", identity, custom_prompt=prompt)


def _check_session_exists(target_id: str, session_id: str) -> bool:
    """Return True when the Claude Code session directory exists locally."""
    if target_id != "claude-code":
        return False  # other targets don't support session resume yet
    session_dir = Path.home() / ".claude" / "projects" / "-"
    if not session_dir.exists():
        return False
    # Check if any JSONL file contains this session_id
    try:
        for f in session_dir.glob("*.jsonl"):
            if f.name.startswith(session_id):
                return True
    except OSError:
        pass
    return False


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
                decision: RouteDecision = route(event, fc_client=fc)
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

                # 2. Dispatch (resume or ordinary)
                result = _dispatch(event, decision, fc)

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
                    # CI Resume fields — passed through to FC
                    "session_id": result.get("session_id"),
                    "commit_sha": result.get("commit_sha"),
                    "identity_name": result.get("identity_name"),
                    "target_id": result.get("target_id"),
                    "task_status": "pushed" if result.get("commit_sha") else None,
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
