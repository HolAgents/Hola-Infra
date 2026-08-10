"""Test CI failure → resume routing in router.py."""

import pytest

from dispatcher.router import (
    ResumeContext,
    RouteDecision,
    route,
    _extract_ci_error,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wf_event(conclusion="failure", commit_sha="abc123", action="completed",
              name="CI", html_url="https://github.com/test/actions/1"):
    return {
        "event_type": "workflow_run",
        "repo_full_name": "HolAgents/test",
        "payload": {
            "action": action,
            "workflow_run": {
                "name": name,
                "conclusion": conclusion,
                "head_commit": {"id": commit_sha} if commit_sha else {},
                "html_url": html_url,
            },
            "repository": {"full_name": "HolAgents/test"},
            "sender": {"login": "github-actions"},
        },
    }


def _cr_event(conclusion="failure", commit_sha="abc123", action="completed"):
    return {
        "event_type": "check_run",
        "repo_full_name": "HolAgents/test",
        "payload": {
            "action": action,
            "check_run": {
                "name": "test",
                "conclusion": conclusion,
                "head_commit": {"id": commit_sha} if commit_sha else {},
                "html_url": "https://github.com/test/actions/2",
            },
            "repository": {"full_name": "HolAgents/test"},
            "sender": {"login": "github-actions"},
        },
    }


class _FakeFC:
    """Fake FCClient for router tests."""

    def __init__(self, task=None):
        self._task = task

    def query_by_commit(self, sha):
        return self._task


# ---------------------------------------------------------------------------
# workflow_run tests
# ---------------------------------------------------------------------------


def test_wf_failure_with_known_commit():
    """Commit found → agent_type='resume' with ResumeContext."""
    fc = _FakeFC(task={
        "id": 42,
        "identity_name": "holagent001",
        "target_id": "claude-code",
        "session_id": "sess-abc-123",
    })
    decision = route(_wf_event(commit_sha="abc123"), fc_client=fc)
    assert decision.should_dispatch is True
    assert decision.agent_type == "resume"
    ctx = decision.resume_context
    assert ctx is not None
    assert ctx.identity_name == "holagent001"
    assert ctx.target_id == "claude-code"
    assert ctx.session_id == "sess-abc-123"
    assert ctx.commit_sha == "abc123"
    assert ctx.original_event_id == 42
    assert "CI" in ctx.error_message


def test_wf_failure_unknown_commit():
    """Commit not found → agent_type='triage', no resume_context."""
    fc = _FakeFC(task=None)
    decision = route(_wf_event(commit_sha="unknown-sha"), fc_client=fc)
    assert decision.should_dispatch is True
    assert decision.agent_type == "triage"
    assert decision.resume_context is None
    assert "orphan CI failure" in decision.reason


def test_wf_failure_no_head_commit():
    """No head_commit in payload → fallback triage."""
    fc = _FakeFC(task=None)
    decision = route(_wf_event(commit_sha=""), fc_client=fc)
    assert decision.agent_type == "triage"


def test_wf_failure_no_session_id_in_task():
    """Task found but lacks session_id → fallback triage (can't resume)."""
    fc = _FakeFC(task={
        "id": 1,
        "identity_name": "holagent001",
        "target_id": "claude-code",
        "session_id": None,
    })
    decision = route(_wf_event(commit_sha="abc123"), fc_client=fc)
    assert decision.agent_type == "triage"


def test_wf_success_skipped():
    """workflow_run success → not dispatched."""
    decision = route(_wf_event(conclusion="success"))
    assert decision.should_dispatch is False
    assert decision.skip_ack is True


def test_wf_in_progress_skipped():
    """workflow_run action='in_progress' → ignored."""
    decision = route(_wf_event(action="in_progress"))
    assert decision.should_dispatch is False
    assert decision.skip_ack is True


def test_wf_cancelled_routed():
    """workflow_run cancelled → also treated as failure."""
    fc = _FakeFC(task={
        "id": 1, "identity_name": "holagent001",
        "target_id": "claude-code", "session_id": "sess-1",
    })
    decision = route(_wf_event(conclusion="cancelled", commit_sha="abc"), fc_client=fc)
    assert decision.agent_type == "resume"


# ---------------------------------------------------------------------------
# check_run tests
# ---------------------------------------------------------------------------


def test_cr_failure_with_known_commit():
    """check_run failure + known commit → resume."""
    fc = _FakeFC(task={
        "id": 7, "identity_name": "holagent001",
        "target_id": "claude-code", "session_id": "sess-cr-1",
    })
    decision = route(_cr_event(commit_sha="cr-abc"), fc_client=fc)
    assert decision.agent_type == "resume"
    assert decision.resume_context is not None


def test_cr_failure_unknown_commit():
    """check_run failure + unknown commit → triage."""
    fc = _FakeFC(task=None)
    decision = route(_cr_event(commit_sha="cr-unknown"), fc_client=fc)
    assert decision.agent_type == "triage"


# ---------------------------------------------------------------------------
# Backward compat
# ---------------------------------------------------------------------------


def test_issue_opened_still_triage():
    """Existing routing unchanged."""
    event = {
        "event_type": "issues",
        "repo_full_name": "HolAgents/test",
        "payload": {
            "action": "opened",
            "issue": {"number": 1, "title": "test", "body": "x"},
            "repository": {"full_name": "HolAgents/test"},
            "sender": {"login": "user"},
        },
    }
    decision = route(event)
    assert decision.agent_type == "triage"


def test_pr_opened_still_review():
    """PR opened → review (unchanged)."""
    event = {
        "event_type": "pull_request",
        "repo_full_name": "HolAgents/test",
        "payload": {
            "action": "opened",
            "pull_request": {"number": 2, "title": "PR test"},
            "repository": {"full_name": "HolAgents/test"},
            "sender": {"login": "user"},
        },
    }
    decision = route(event)
    assert decision.agent_type == "review"


# ---------------------------------------------------------------------------
# CI error extraction
# ---------------------------------------------------------------------------


def test_ci_error_message():
    ci_obj = {
        "name": "test-suite",
        "conclusion": "failure",
        "html_url": "https://github.com/example/actions/runs/42",
    }
    msg = _extract_ci_error(ci_obj, "workflow_run")
    assert "test-suite" in msg
    assert "failure" in msg
    assert "https://github.com/example/actions/runs/42" in msg
