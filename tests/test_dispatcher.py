"""Test dispatcher router decisions."""

import pytest

from dispatcher.router import RouteDecision, route


def _make_event(event_type: str, payload: dict) -> dict:
    return {
        "id": 1,
        "delivery_id": "test-001",
        "event_type": event_type,
        "repo_full_name": "HolAgents/test",
        "payload": payload,
        "claim_token": "token-xxx",
        "received_at": "2026-01-01T00:00:00Z",
    }


class TestBotFilter:
    def test_bot_sender_skipped(self):
        ev = _make_event("issues", {
            "action": "opened",
            "issue": {"number": 1, "node_id": "I_1"},
            "sender": {"login": "hola-bot"},
            "repository": {"full_name": "HolAgents/test"},
        })
        d = route(ev)
        assert d.should_dispatch is False
        assert d.skip_ack is True
        assert "bot" in d.reason


class TestIssueRouting:
    def test_issue_opened_dispatched(self):
        ev = _make_event("issues", {
            "action": "opened",
            "issue": {"number": 1, "node_id": "I_1"},
            "sender": {"login": "user"},
            "repository": {"full_name": "HolAgents/test"},
        })
        d = route(ev)
        assert d.should_dispatch is True
        assert d.agent_type == "triage"

    def test_issue_closed_skipped(self):
        ev = _make_event("issues", {
            "action": "closed",
            "issue": {"number": 1},
            "sender": {"login": "user"},
            "repository": {"full_name": "HolAgents/test"},
        })
        d = route(ev)
        assert d.should_dispatch is False
        assert d.skip_ack is True


class TestPRRouting:
    def test_pr_opened_dispatched(self):
        ev = _make_event("pull_request", {
            "action": "opened",
            "pull_request": {"number": 5, "node_id": "PR_5", "title": "fix bug"},
            "sender": {"login": "user"},
            "repository": {"full_name": "HolAgents/test"},
        })
        d = route(ev)
        assert d.should_dispatch is True
        assert d.agent_type == "review"

    def test_pr_closed_skipped(self):
        ev = _make_event("pull_request", {
            "action": "closed",
            "pull_request": {"number": 5},
            "sender": {"login": "user"},
            "repository": {"full_name": "HolAgents/test"},
        })
        d = route(ev)
        assert d.should_dispatch is False


class TestPushRouting:
    def test_human_push_dispatched(self):
        ev = _make_event("push", {
            "ref": "refs/heads/main",
            "sender": {"login": "user"},
            "repository": {"full_name": "HolAgents/test"},
            "commits": [],
        })
        d = route(ev)
        assert d.should_dispatch is True
        assert d.agent_type == "quality"

    def test_bot_push_skipped(self):
        ev = _make_event("push", {
            "ref": "refs/heads/main",
            "sender": {"login": "hola-bot"},
            "repository": {"full_name": "HolAgents/test"},
            "commits": [],
        })
        d = route(ev)
        assert d.should_dispatch is False
        assert d.skip_ack is True


class TestCommentRouting:
    def test_mention_dispatched(self):
        ev = _make_event("issue_comment", {
            "action": "created",
            "comment": {"body": "@hola-bot please review this"},
            "issue": {"number": 1, "node_id": "I_1"},
            "sender": {"login": "user"},
            "repository": {"full_name": "HolAgents/test"},
        })
        d = route(ev)
        assert d.should_dispatch is True
        assert d.agent_type == "response"

    def test_no_mention_skipped(self):
        ev = _make_event("issue_comment", {
            "action": "created",
            "comment": {"body": "Looks good!"},
            "issue": {"number": 1},
            "sender": {"login": "user"},
            "repository": {"full_name": "HolAgents/test"},
        })
        d = route(ev)
        assert d.should_dispatch is False
        assert d.skip_ack is True
