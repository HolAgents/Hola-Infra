"""Task workspace management — path-derived registry.

Workspace paths are DERIVED from (repo, item number), so no registry
storage is needed: resumes and dispatcher restarts re-derive the same
path. The dispatcher releases a workspace only when the PR merges (M4);
agents must never clean up their own workspace.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from dispatcher.config import get_settings


def workspace_root() -> Path:
    return Path(get_settings().workspace_root).resolve()


def _sanitize(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)


def derive_path(repo_full_name: str, item_number: int) -> Path:
    """Deterministic workspace path for a GitHub work item."""
    owner, _, repo = repo_full_name.partition("/")
    return workspace_root() / f"{_sanitize(owner)}__{_sanitize(repo)}" / f"item-{item_number}"


def allocate(
    repo_full_name: str,
    item_number: int,
    event_id: int,
    identity_name: str | None,
) -> Path:
    """Create the workspace and write task metadata (idempotent)."""
    path = derive_path(repo_full_name, item_number)
    path.mkdir(parents=True, exist_ok=True)
    meta = path / ".hola-task.json"
    meta.write_text(
        json.dumps(
            {
                "repo": repo_full_name,
                "item_number": item_number,
                "event_id": event_id,
                "identity_name": identity_name,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def ensure(repo_full_name: str, item_number: int) -> Path:
    """Return the workspace path, creating the directory without touching
    the task metadata (used by resume — the original metadata wins)."""
    path = derive_path(repo_full_name, item_number)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_task_meta(path: Path) -> dict:
    """Read .hola-task.json from a workspace ({} when absent)."""
    meta = path / ".hola-task.json"
    if not meta.exists():
        return {}
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def is_active(repo_full_name: str, item_number: int) -> bool:
    """True when a workspace currently exists for the item."""
    return derive_path(repo_full_name, item_number).exists()


def release(repo_full_name: str, item_number: int) -> Path | None:
    """Archive the workspace (rename to .archive/<ts>-<name>)."""
    path = derive_path(repo_full_name, item_number)
    if not path.exists():
        return None
    archive_dir = workspace_root() / ".archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{int(time.time())}-{path.parent.name}-{path.name}"
    path.rename(target)
    return target
