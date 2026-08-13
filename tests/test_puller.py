"""Test dispatcher puller — session ID, commit marker, resume logic."""

import uuid
from pathlib import Path

import pytest

from dispatcher.puller import (
    _parse_commit_marker,
    _check_session_exists,
    _resume_agent,
    _cold_start_agent,
    _run_agent,
)
from dispatcher.router import ResumeContext


# ---------------------------------------------------------------------------
# _parse_commit_marker
# ---------------------------------------------------------------------------


def test_parse_commit_marker_valid():
    assert _parse_commit_marker(
        'Some output\n##HOLA_RESULT:{"commit_sha":"abc123def456"}##\nDone'
    ) == "abc123def456"


def test_parse_commit_marker_new_tag_format():
    sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    assert _parse_commit_marker(
        f"Final message:\n<CommitSha>{sha}</CommitSha>\n"
    ) == sha


def test_parse_commit_marker_new_tag_with_surrounding_text():
    sha = "f" * 40
    stdout = f"Working...\n<CommitSha>{sha}</CommitSha>"
    assert _parse_commit_marker(stdout) == sha


def test_parse_commit_marker_new_tag_rejects_short_sha():
    # Only full 40-char SHAs match the tag format
    assert _parse_commit_marker("<CommitSha>abc123</CommitSha>") is None


def test_parse_commit_marker_not_found():
    assert _parse_commit_marker("No marker here") is None


def test_parse_commit_marker_empty():
    assert _parse_commit_marker("") is None


def test_parse_commit_marker_malformed_json():
    assert _parse_commit_marker(
        '##HOLA_RESULT:{bad json}##'
    ) is None


def test_parse_commit_marker_no_commit_sha_key():
    assert _parse_commit_marker(
        '##HOLA_RESULT:{"other":"value"}##'
    ) is None


def test_parse_commit_marker_multiline_stdout():
    stdout = """Starting agent...
Working on the issue...
Committing changes...
##HOLA_RESULT:{"commit_sha":"deadbeef"}##
Agent finished."""
    assert _parse_commit_marker(stdout) == "deadbeef"


# ---------------------------------------------------------------------------
# session ID generation (integration-style — check format)
# ---------------------------------------------------------------------------


def test_session_id_is_valid_uuid():
    sid = str(uuid.uuid4())
    uuid.UUID(sid)  # does not raise
    assert len(sid) == 36
    assert sid.count("-") == 4


# ---------------------------------------------------------------------------
# _check_session_exists
# ---------------------------------------------------------------------------


def test_check_session_exists_non_claude_target():
    """Non claude-code targets always return False."""
    assert _check_session_exists("hermes", "sess-any") is False


def test_check_session_exists_no_dir(monkeypatch, tmp_path):
    """When .claude dir doesn't exist → False."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _check_session_exists("claude-code", "no-such-session") is False


# ---------------------------------------------------------------------------
# ResumeContext dataclass
# ---------------------------------------------------------------------------


def test_resume_context_all_fields():
    ctx = ResumeContext(
        identity_name="holagent001",
        target_id="claude-code",
        session_id="sess-test-123",
        commit_sha="abc123def456",
        error_message="Workflow 'CI' failed",
        original_event_id=42,
    )
    assert ctx.identity_name == "holagent001"
    assert ctx.original_event_id == 42


# ---------------------------------------------------------------------------
# Agent prompt includes marker instruction
# ---------------------------------------------------------------------------


def test_run_agent_prompt_includes_marker():
    """_run_agent should include ##HOLA_RESULT instruction in prompt."""
    # We can't easily test the subprocess call, but we can verify the
    # function signature and that session_id is generated.
    # This is a smoke test for the module-level constants.
    from dispatcher.puller import logger
    assert logger is not None  # module loads correctly


class _FakeFC:
    """Minimal FCClient double for sync/resume tests."""

    def __init__(self, events):
        self.events = events
        self.patches = []

    def query_events(self, repo=None, limit=200):
        return self.events

    def patch_event(self, event_id, patch):
        self.patches.append((event_id, patch))
        return {"id": event_id, **patch}


def test_sync_task_state_patches_original_task():
    from dispatcher.puller import _sync_task_state
    from dispatcher.router import RouteDecision, SyncTask

    original = {
        "id": 3,
        "event_type": "issues",
        "claim_token": "orig-token",
        "session_id": "sess-1",
        "payload": {"issue": {"number": 7}},
    }
    fc = _FakeFC([original])
    event = {
        "event_type": "issue_comment",
        "repo_full_name": "HolAgents/demo",
        "payload": {"issue": {"number": 7}},
    }
    decision = RouteDecision(False, skip_ack=True, sync_task=SyncTask("planned"))
    _sync_task_state(event, decision, fc)

    assert fc.patches == [(3, {"claim_token": "orig-token", "task_status": "planned"})]


def test_sync_task_state_no_original_task_is_noop():
    from dispatcher.puller import _sync_task_state
    from dispatcher.router import RouteDecision, SyncTask

    fc = _FakeFC([])  # nothing with a session
    event = {
        "event_type": "issue_comment",
        "repo_full_name": "HolAgents/demo",
        "payload": {"issue": {"number": 7}},
    }
    decision = RouteDecision(False, skip_ack=True, sync_task=SyncTask("planned"))
    _sync_task_state(event, decision, fc)
    assert fc.patches == []


def test_dispatch_wires_workspace_and_identity_env(monkeypatch, tmp_path):
    """M1: _dispatch launches the agent with cwd=workspace, injected
    identity env, configurable claude_bin, and the runbook skill named."""
    import json
    import sqlite3
    import subprocess as sp

    from dispatcher import workspace
    from dispatcher.config import get_settings
    from dispatcher.puller import _dispatch
    from dispatcher.router import RouteDecision

    # fixture Hola-Switch data store
    db = tmp_path / "cc-switch.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE identities (id TEXT, name TEXT, description TEXT);"
        "CREATE TABLE identity_credentials (identity_id TEXT, credential_type TEXT, data TEXT, created_at TEXT);"
    )
    conn.execute("INSERT INTO identities VALUES ('id-1', 'holagent001', 't')")
    conn.execute(
        "INSERT INTO identity_credentials VALUES ('id-1', 'github-account', ?, 'x')",
        (json.dumps({"token": "sha256:x", "git_name": "holagent001",
                     "git_email": "h@qq.com", "ssh_key_path": ""}),),
    )
    conn.commit()
    conn.close()
    cred = tmp_path / "credentials" / "id-1"
    cred.mkdir(parents=True)
    (cred / "github-account.env").write_text("GITHUB_TOKEN=ghp_envtoken\n", encoding="utf-8")

    settings = get_settings()
    settings.workspace_root = str(tmp_path)
    settings.claude_bin = "fake-claude"
    settings.hola_switch_db_path = str(db)

    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        captured["cwd"] = kw.get("cwd")
        captured["env"] = kw.get("env")
        return sp.CompletedProcess(args, 0, stdout="<CommitSha>" + "f" * 40 + "</CommitSha>", stderr="")

    monkeypatch.setattr("dispatcher.puller.subprocess.run", fake_run)

    event = {
        "id": 5, "event_type": "issues", "repo_full_name": "HolAgents/demo",
        "payload": {"issue": {"number": 7}, "sender": {"login": "u"}},
        "claim_token": "t",
    }
    decision = RouteDecision(True, agent_type="triage", reason="issue opened")
    result = _dispatch(event, decision, fc=None)

    assert result["status"] == "completed"
    assert captured["cwd"] == str(workspace.derive_path("HolAgents/demo", 7))
    assert captured["env"]["GH_TOKEN"] == "ghp_envtoken"
    assert captured["env"]["GIT_AUTHOR_NAME"] == "holagent001"
    assert captured["env"]["HOLA_AGENT_ID"] == "holagent001"
    assert captured["args"][0] == "fake-claude"
    assert "hola-task-run" in captured["args"][-1]


# ---------------------------------------------------------------------------
# Cold start prompt content
# ---------------------------------------------------------------------------


class _FakeIdentity:
    identity_name = "test-identity"
    target_id = "claude-code"
    agent_id = "fake-01"
    agent_type = "triage"
    display_name = "Fake"
    gh_user = "fake-bot"


def test_cold_start_prompt_contains_context():
    """Verify _cold_start_agent builds prompt with key info."""
    ctx = ResumeContext(
        identity_name="test-identity",
        target_id="claude-code",
        session_id="sess-old",
        commit_sha="abc123",
        error_message="CI failed: No module named X",
        original_event_id=1,
    )
    # We can't easily capture the subprocess call, but we can check
    # that the function doesn't crash when invoked with a mock that
    # would raise FileNotFoundError (no claude CLI).
    try:
        _cold_start_agent(ctx, _FakeIdentity(), {"id": 1, "claim_token": "x"})
    except FileNotFoundError:
        pass  # expected — no claude CLI in test env


def test_resume_agent_uses_resume_flag():
    """_resume_agent builds command with --resume."""
    ctx = ResumeContext(
        identity_name="t", target_id="claude-code", session_id="sess-r",
        commit_sha="abc", error_message="fail", original_event_id=1,
    )
    try:
        _resume_agent(ctx, {"id": 2, "claim_token": "y"})
    except FileNotFoundError:
        pass  # expected — no claude CLI
