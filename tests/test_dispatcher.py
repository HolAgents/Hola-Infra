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

    def test_merge_push_skipped(self, monkeypatch):
        monkeypatch.setattr(
            "dispatcher.router._looks_like_merge", lambda repo, sha: True
        )
        ev = _make_event("push", {
            "ref": "refs/heads/main",
            "head_commit": {"id": "a" * 40},
            "sender": {"login": "user"},
            "repository": {"full_name": "HolAgents/test"},
            "commits": [],
        })
        d = route(ev)
        assert d.should_dispatch is False
        assert d.skip_ack is True
        assert "merge" in d.reason

    def test_non_merge_push_still_dispatched(self, monkeypatch):
        monkeypatch.setattr(
            "dispatcher.router._looks_like_merge", lambda repo, sha: False
        )
        ev = _make_event("push", {
            "ref": "refs/heads/main",
            "head_commit": {"id": "a" * 40},
            "sender": {"login": "user"},
            "repository": {"full_name": "HolAgents/test"},
            "commits": [],
        })
        d = route(ev)
        assert d.should_dispatch is True
        assert d.agent_type == "quality"

    def test_merge_lookup_failure_falls_back_to_dispatch(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("gh api down")
        monkeypatch.setattr("dispatcher.router.subprocess.run", boom)
        ev = _make_event("push", {
            "ref": "refs/heads/main",
            "head_commit": {"id": "a" * 40},
            "sender": {"login": "user"},
            "repository": {"full_name": "HolAgents/test"},
            "commits": [],
        })
        d = route(ev)
        assert d.should_dispatch is True
        assert d.agent_type == "quality"

    def test_merge_lookup_result_parsing(self, monkeypatch):
        import dispatcher.router as router_mod
        calls = {}

        class FakeRun:
            returncode = 0
            stdout = '[2, "Merge pull request #1 from x/y"]'

        def fake_run(*args, **kwargs):
            calls["args"] = args[0]
            return FakeRun()

        monkeypatch.setattr(router_mod.subprocess, "run", fake_run)
        assert router_mod._looks_like_merge("HolAgents/test", "b" * 40) is True
        assert calls["args"][:3] == ["gh", "api", "repos/HolAgents/test/commits/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]

        FakeRun.stdout = '[1, "fix: add tests (#98)"]'
        assert router_mod._looks_like_merge("HolAgents/test", "b" * 40) is True

        FakeRun.stdout = '[1, "wip: normal commit"]'
        assert router_mod._looks_like_merge("HolAgents/test", "b" * 40) is False


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


# ---------------------------------------------------------------------------
# FC Client new methods (integration-style with monkeypatch)
# ---------------------------------------------------------------------------


class TestFCClientNewMethods:
    """Test query_by_commit and patch_event via mocked HTTP."""

    @staticmethod
    def _make_fc(methods):
        """Create an FCClient with mocked httpx.Client methods."""
        from dispatcher.fc_client import FCClient
        import httpx

        class _MockClient:
            def __init__(self):
                pass
            def close(self):
                pass

        # Add mocked methods
        for name, fn in methods.items():
            setattr(_MockClient, name, fn)

        fc = FCClient.__new__(FCClient)
        fc._base = "http://fake"
        fc._api_key = "fake-key"
        fc._client = _MockClient()
        return fc

    def test_query_by_commit_found(self):
        class _FakeResp:
            status_code = 200
            @staticmethod
            def json():
                return {"id": 1, "identity_name": "holagent001"}
            @staticmethod
            def raise_for_status():
                pass

        fc = self._make_fc({"get": lambda self, url: _FakeResp()})
        result = fc.query_by_commit("abc")
        assert result == {"id": 1, "identity_name": "holagent001"}

    def test_query_by_commit_not_found(self):
        class _Fake404:
            status_code = 404

        fc = self._make_fc({"get": lambda self, url: _Fake404()})
        result = fc.query_by_commit("nonexistent")
        assert result is None

    def test_query_by_commit_network_error(self):
        import httpx

        def _fake_get(self, url):
            raise httpx.ConnectError("timeout")

        fc = self._make_fc({"get": _fake_get})
        result = fc.query_by_commit("any")
        assert result is None

    def test_patch_event_success(self):
        class _FakeOK:
            status_code = 200

        fc = self._make_fc({"patch": lambda self, url, json=None: _FakeOK()})
        assert fc.patch_event(1, {"task_status": "pushed"}) is True

    def test_patch_event_failure(self):
        class _FakeErr:
            status_code = 500

        fc = self._make_fc({"patch": lambda self, url, json=None: _FakeErr()})
        assert fc.patch_event(1, {"task_status": "pushed"}) is False
