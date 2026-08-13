"""Poll loop — claim events from FC, dispatch to agents, ack results."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from dispatcher.config import get_settings
from dispatcher.fc_client import FCClient
from dispatcher.kanban import KanbanClient
from dispatcher.identity import resolve
from dispatcher.router import ResumeContext, RouteDecision, route
from dispatcher import hola_switch, workspace

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


def _extract_item_number(event: dict[str, Any]) -> int | None:
    """Get the issue/PR number from the event payload (workspace key)."""
    payload = event.get("payload", {})
    for key in ("issue", "pull_request"):
        n = (payload.get(key) or {}).get("number")
        if n is not None:
            return n
    return None


def _run_agent(
    event: dict[str, Any],
    agent_type: str,
    identity: object | None,
    custom_prompt: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
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
        f"Use gh CLI to interact with GitHub as needed.\n"
        f"Follow the hola-task-run skill.\n\n"
        f"IMPORTANT: When you are done (code committed and pushed), "
        f"output this exact tag as your last message:\n"
        f"<CommitSha>REPLACE_WITH_FULL_40_CHAR_SHA</CommitSha>"
    )

    ident_name = identity.identity_name if identity else None
    ident_target = identity.target_id if identity else None

    try:
        settings = get_settings()
        result = subprocess.run(
            [settings.claude_bin, "--session-id", session_id,
             "--name", f"task-{event['id']}",
             "-p", prompt],
            capture_output=True, text=True, timeout=600,
            cwd=cwd, env=env,
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


def _sync_task_state(event: dict[str, Any], decision: RouteDecision, fc: FCClient) -> None:
    """Patch the ORIGINAL task event with state derived from an
    agent-produced signal (plan comment / agent PR) — M2 closed loop."""
    sync = decision.sync_task
    repo = event.get("repo_full_name", "")
    item_number = sync.item_number or _extract_item_number(event)
    if not repo or not item_number:
        logger.warning("sync_task without repo/item: %s", decision.reason)
        return

    # Find the original task: the work event for this (repo, item) that
    # carries a session (i.e. an agent was dispatched for it).
    target = None
    try:
        events = fc.query_events(repo=repo, limit=200)
    except Exception as exc:
        logger.error("sync_task query failed: %s", exc)
        return
    for e in events:
        if e.get("event_type") != "issues":
            continue
        p = e.get("payload") or {}
        n = ((p.get("issue") or {}).get("number")
             or (p.get("pull_request") or {}).get("number"))
        if n == item_number and e.get("session_id"):
            target = e
            break

    if target is None:
        logger.warning("sync_task: no original task for %s item %s", repo, item_number)
        return

    patch = {
        "claim_token": target.get("claim_token"),
        "task_status": sync.task_status,
    }
    if sync.commit_sha:
        patch["commit_sha"] = sync.commit_sha
    try:
        fc.patch_event(target["id"], patch)
        logger.info("synced task %s → %s", target["id"], sync.task_status)
    except Exception as exc:
        logger.error("sync_task patch failed: %s", exc)


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
    fc: FCClient | None,
) -> dict[str, Any]:
    """Execute a dispatch decision.  Returns an ack-compatible result dict.

    A heartbeat thread keeps the claim lease alive while the agent runs
    (M3 — long tasks must not hit the 15-min TTL requeue).
    """
    stop = threading.Event()
    if fc is not None:
        threading.Thread(
            target=_heartbeat_loop, args=(fc, event, stop), daemon=True,
        ).start()
    try:
        return _dispatch_inner(event, decision, fc)
    finally:
        stop.set()


def _ci_dedup_key(event: dict[str, Any], decision: RouteDecision) -> str:
    """Unique key per (failing commit, CI run) — coalesces the
    workflow_run + check_run burst of a single failure (M3)."""
    ctx = decision.resume_context
    ci_obj = (
        (event.get("payload") or {}).get("workflow_run")
        or (event.get("payload") or {}).get("check_run")
        or {}
    )
    ci_id = ci_obj.get("id") or event["id"]
    return f"{ctx.commit_sha}:{ci_id}"


def _heartbeat_loop(fc: FCClient, event: dict[str, Any], stop: threading.Event) -> None:
    """Refresh the claim lease every 60s until stopped."""
    while not stop.wait(60):
        try:
            fc.heartbeat(event["id"], event["claim_token"])
        except Exception as exc:  # noqa: BLE001 — keep the lease thread alive
            logger.warning("heartbeat failed: %s", exc)


def _dispatch_inner(
    event: dict[str, Any],
    decision: RouteDecision,
    fc: FCClient | None,
) -> dict[str, Any]:
    repo = event.get("repo_full_name", "")

    # ---- OCR review trigger: resolve PR head sha → resume original task ----
    if decision.ocr_trigger:
        return _resume_from_ocr(decision.ocr_trigger, event, fc)

    # ---- ordinary dispatch ----
    if decision.agent_type != "resume":
        identity = resolve(decision.agent_type, repo)
        if identity is None:
            return {
                "status": "failed",
                "message": f"no identity for {decision.agent_type}",
            }
        # Human comment on an item with an active workspace → resume the
        # original task instead of spawning a fresh response agent.
        if decision.agent_type == "response":
            item_number = _extract_item_number(event)
            if item_number and workspace.is_active(repo, item_number):
                return _resume_from_comment(event, fc)
        item_number = _extract_item_number(event)
        cwd = env = None
        if item_number:
            cwd = str(workspace.allocate(
                repo, item_number, event["id"], identity.identity_name))
            env = {**os.environ, **hola_switch.resolve_identity_env(identity.identity_name)}
        return _run_agent(event, decision.agent_type, identity, cwd=cwd, env=env)

    # ---- resume dispatch ----
    ctx = decision.resume_context
    if ctx is None:
        return {
            "status": "failed",
            "message": "resume agent_type without resume_context",
        }
    return _execute_resume(ctx, event, fc)


def _execute_resume(
    ctx: ResumeContext,
    event: dict[str, Any],
    fc: FCClient | None,
    failure_status: str | None = "ci_failed",
) -> dict[str, Any]:
    """Resume the original task in its workspace: attempt hot resume,
    fall back to cold start on failure (#39 decision 4)."""
    repo = event.get("repo_full_name", "")

    # Workspace + identity env: derived from the original task (do NOT
    # rewrite the task metadata on resume — the original values win).
    cwd = env = None
    if ctx.item_number and repo:
        cwd = str(workspace.ensure(repo, ctx.item_number))
        env = {**os.environ, **hola_switch.resolve_identity_env(ctx.identity_name)}

    if fc is not None and failure_status:
        fc.patch_event(ctx.original_event_id, {
            "claim_token": ctx.claim_token,
            "task_status": failure_status,
        })

    result = _resume_agent(ctx, event, cwd=cwd, env=env)
    if result.get("status") != "completed":
        logger.info(
            "hot resume failed (%s) — cold start fallback",
            str(result.get("message", ""))[:80],
        )
        identity = resolve("triage", repo)
        result = _cold_start_agent(ctx, identity, event, cwd=cwd, env=env)

    if result.get("status") == "completed" and fc is not None:
        new_sha = result.get("commit_sha", "")
        fc.patch_event(ctx.original_event_id, {
            "claim_token": ctx.claim_token,
            "task_status": "pushed",
            "commit_sha": new_sha or ctx.commit_sha,
        })

    return result


def _find_original_task(fc: FCClient | None, repo: str, item_number: int) -> dict | None:
    """Locate the original work event for (repo, item) with a session."""
    if fc is None:
        return None
    try:
        events = fc.query_events(repo=repo, limit=200)
    except Exception as exc:  # noqa: BLE001
        logger.error("find original task failed: %s", exc)
        return None
    for e in events:
        if e.get("event_type") != "issues":
            continue
        p = e.get("payload") or {}
        n = ((p.get("issue") or {}).get("number")
             or (p.get("pull_request") or {}).get("number"))
        if n == item_number and e.get("session_id"):
            return e
    return None


def _resume_from_ocr(pr_number: int, event: dict[str, Any], fc: FCClient | None) -> dict[str, Any]:
    """OpenCodeReview feedback on a PR → resume the task that opened it."""
    try:
        out = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "headRefOid"],
            capture_output=True, text=True, timeout=30,
        )
        head_sha = json.loads(out.stdout).get("headRefOid", "")
    except Exception as exc:  # noqa: BLE001
        logger.error("ocr resume: gh pr view failed: %s", exc)
        return {"status": "failed", "message": f"ocr pr lookup failed: {exc}"}

    if not head_sha or fc is None:
        return {"status": "failed", "message": "ocr resume: no head sha"}

    task = fc.query_by_commit(head_sha)
    if not task or not task.get("session_id"):
        return {"status": "failed", "message": "ocr resume: no task for pr head"}

    task_payload = task.get("payload") or {}
    item_number = (
        (task_payload.get("issue") or {}).get("number")
        or (task_payload.get("pull_request") or {}).get("number")
    )
    comment_body = (event.get("payload") or {}).get("comment", {}).get("body", "")[:500]
    ctx = ResumeContext(
        identity_name=task.get("identity_name") or "",
        target_id=task.get("target_id") or "claude-code",
        session_id=task["session_id"],
        commit_sha=head_sha,
        error_message=f"OpenCodeReview feedback:\n{comment_body}",
        original_event_id=task["id"],
        item_number=item_number,
        claim_token=task.get("claim_token") or "",
    )
    return _execute_resume(ctx, event, fc, failure_status=None)


def _resume_from_comment(event: dict[str, Any], fc: FCClient | None) -> dict[str, Any]:
    """Human comment on an item with an active workspace → resume its task."""
    repo = event.get("repo_full_name", "")
    item_number = _extract_item_number(event)
    if not item_number:
        return {"status": "failed", "message": "comment resume: no item number"}

    task = _find_original_task(fc, repo, item_number)
    if task is None:
        return {"status": "failed", "message": "comment resume: no original task"}

    comment_body = (event.get("payload") or {}).get("comment", {}).get("body", "")[:500]
    ctx = ResumeContext(
        identity_name=task.get("identity_name") or "",
        target_id=task.get("target_id") or "claude-code",
        session_id=task["session_id"],
        commit_sha=task.get("commit_sha") or "",
        error_message=f"Human comment on the issue:\n{comment_body}",
        original_event_id=task["id"],
        item_number=item_number,
        claim_token=task.get("claim_token") or "",
    )
    return _execute_resume(ctx, event, fc, failure_status=None)


def _resume_agent(
    ctx: ResumeContext,
    event: dict[str, Any],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Hot resume: ``claude --resume <session_id> -p '…'`` in the task workspace."""
    prompt = (
        f"CI workflow failed for commit {ctx.commit_sha[:7]}.\n"
        f"Error: {ctx.error_message}\n\n"
        f"Fix the issue, commit your changes, and push.\n"
        f"Follow the hola-task-resume skill.\n\n"
        f"IMPORTANT: When done, output this exact tag as your last message:\n"
        f"<CommitSha>REPLACE_WITH_FULL_40_CHAR_SHA</CommitSha>"
    )
    try:
        settings = get_settings()
        result = subprocess.run(
            [settings.claude_bin, "--resume", ctx.session_id, "-p", prompt],
            capture_output=True, text=True, timeout=600,
            cwd=cwd, env=env,
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
    cwd: str | None = None,
    env: dict[str, str] | None = None,
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
        f"Follow the hola-task-run skill.\n\n"
        f"IMPORTANT: When done, output this exact tag as your last message:\n"
        f"<CommitSha>REPLACE_WITH_FULL_40_CHAR_SHA</CommitSha>"
    )
    return _run_agent(event, "triage", identity, custom_prompt=prompt, cwd=cwd, env=env)


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
    seen_ci_keys: set[str] = set()  # M3: CI-failure coalescing

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

                if decision.sync_task:
                    # M2/M3: agent-produced signal (plan comment / agent
                    # PR / CI success) → patch the original task, no
                    # agent dispatch.
                    _sync_task_state(event, decision, fc)
                    ack_results.append({
                        "event_id": event["id"],
                        "claim_token": event["claim_token"],
                        "status": "completed",
                        "message": decision.reason,
                    })
                    continue

                if decision.ocr_trigger:
                    # M3: OpenCodeReview feedback → resume the original task.
                    result = _dispatch(event, decision, fc)
                    ack_results.append({
                        "event_id": event["id"],
                        "claim_token": event["claim_token"],
                        "status": result["status"],
                        "agent_id": "resume",
                        "message": result.get("message", ""),
                    })
                    continue

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

                if decision.agent_type == "resume" and decision.resume_context:
                    # M3: one CI failure fires workflow_run + several
                    # check_run events — resume only once per (commit, run).
                    key = _ci_dedup_key(event, decision)
                    if key in seen_ci_keys:
                        logger.info("duplicate CI trigger skipped: %s", key)
                        ack_results.append({
                            "event_id": event["id"],
                            "claim_token": event["claim_token"],
                            "status": "completed",
                            "message": f"duplicate CI trigger {key}",
                        })
                        continue
                    seen_ci_keys.add(key)

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
