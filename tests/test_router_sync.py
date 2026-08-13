"""M2 routing — plan comment / agent PR → SyncTask decisions."""

from dispatcher.router import route

PLAN_COMMENT = {
    "id": 10,
    "event_type": "issue_comment",
    "repo_full_name": "HolAgents/demo",
    "payload": {
        "action": "created",
        "issue": {"number": 7},
        "comment": {"body": "<!-- hola-plan -->\n## Plan: ..."},
        "sender": {"login": "holagent001"},
    },
    "claim_token": "t",
}

AGENT_PR = {
    "id": 11,
    "event_type": "pull_request",
    "repo_full_name": "HolAgents/demo",
    "payload": {
        "action": "opened",
        "pull_request": {
            "number": 12,
            "head": {"ref": "hola/issue-7-fix", "sha": "b" * 40},
        },
        "sender": {"login": "holagent001"},
    },
    "claim_token": "t",
}


def test_plan_comment_syncs_planned():
    d = route(PLAN_COMMENT)
    assert d.should_dispatch is False
    assert d.skip_ack is True
    assert d.sync_task is not None
    assert d.sync_task.task_status == "planned"


def test_agent_pr_syncs_pr_opened_with_branch_item_number():
    d = route(AGENT_PR)
    assert d.should_dispatch is False
    assert d.sync_task is not None
    assert d.sync_task.task_status == "pr_opened"
    assert d.sync_task.commit_sha == "b" * 40
    assert d.sync_task.item_number == 7  # from branch name, not PR number


def test_human_pr_still_dispatches_review():
    import copy
    ev = copy.deepcopy(AGENT_PR)
    ev["payload"]["pull_request"]["head"]["ref"] = "feature/other"
    d = route(ev)
    assert d.should_dispatch is True
    assert d.agent_type == "review"
    assert d.sync_task is None


def test_mention_comment_still_dispatches_response():
    ev = dict(PLAN_COMMENT)
    ev["payload"]["comment"]["body"] = "/do something"
    d = route(ev)
    assert d.should_dispatch is True
    assert d.agent_type == "response"


# ---------------------------------------------------------------------------
# M4: PR close release, blocked comment, iteration cap
# ---------------------------------------------------------------------------

def _closed_pr(merged):
    import copy
    ev = copy.deepcopy(AGENT_PR)
    ev["payload"]["action"] = "closed"
    ev["payload"]["pull_request"]["merged"] = merged
    return ev


def test_agent_pr_merged_syncs_done():
    d = route(_closed_pr(True))
    assert d.should_dispatch is False
    assert d.sync_task is not None
    assert d.sync_task.task_status == "done"
    assert d.sync_task.item_number == 7


def test_agent_pr_closed_unmerged_syncs_released():
    d = route(_closed_pr(False))
    assert d.sync_task is not None
    assert d.sync_task.task_status == "released"


def test_blocked_comment_syncs_blocked():
    ev = dict(PLAN_COMMENT)
    ev["payload"]["comment"]["body"] = "<!-- hola-blocked -->\n**Status: blocked**..."
    d = route(ev)
    assert d.should_dispatch is False
    assert d.sync_task is not None
    assert d.sync_task.task_status == "blocked"
