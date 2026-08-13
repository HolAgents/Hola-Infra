"""Workspace path derivation, allocation, and release."""

import json

from dispatcher import workspace
from dispatcher.config import get_settings


def _set_root(tmp_path):
    settings = get_settings()
    settings.workspace_root = str(tmp_path)


def test_derive_path_is_deterministic(tmp_path):
    _set_root(tmp_path)
    a = workspace.derive_path("HolAgents/Foo-Bar", 42)
    b = workspace.derive_path("HolAgents/Foo-Bar", 42)
    assert a == b
    assert a.name == "item-42"
    assert a.parent.name == "HolAgents__Foo-Bar"
    assert a != workspace.derive_path("HolAgents/Other", 42)
    assert a != workspace.derive_path("HolAgents/Foo-Bar", 7)


def test_allocate_writes_task_meta(tmp_path):
    _set_root(tmp_path)
    path = workspace.allocate("HolAgents/demo", 1, event_id=100, identity_name="holagent001")
    meta = workspace.read_task_meta(path)
    assert meta["repo"] == "HolAgents/demo"
    assert meta["item_number"] == 1
    assert meta["event_id"] == 100
    assert meta["identity_name"] == "holagent001"
    assert workspace.is_active("HolAgents/demo", 1)


def test_ensure_does_not_rewrite_meta(tmp_path):
    _set_root(tmp_path)
    path = workspace.allocate("HolAgents/demo", 1, event_id=100, identity_name="holagent001")
    # resume path: ensure must not clobber original metadata
    path2 = workspace.ensure("HolAgents/demo", 1)
    assert path2 == path
    meta = workspace.read_task_meta(path)
    assert meta["event_id"] == 100  # original wins


def test_release_archives(tmp_path):
    _set_root(tmp_path)
    workspace.allocate("HolAgents/demo", 1, event_id=100, identity_name="x")
    target = workspace.release("HolAgents/demo", 1)
    assert target is not None
    assert not workspace.is_active("HolAgents/demo", 1)
    assert target.exists()
    assert ".archive" in str(target)


def test_read_meta_missing_returns_empty(tmp_path):
    _set_root(tmp_path)
    assert workspace.read_task_meta(workspace.derive_path("HolAgents/none", 9)) == {}
