"""Read identity credentials from the local Hola-Switch data store.

Hola-Switch keeps identities in ``~/.cc-switch/cc-switch.db`` (SQLite)
with secrets in ``~/.cc-switch/credentials/{identity_id}/github-account.env``.
The dispatcher reads both READ-ONLY to build the per-agent environment —
no Hola-Switch API exists yet, so this is the integration surface
(Hola-Infra#27, #39).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from dispatcher.config import get_settings

logger = logging.getLogger(__name__)


def _db_path() -> Path:
    settings = get_settings()
    return Path(settings.hola_switch_db_path).expanduser()


def _credentials_dir() -> Path:
    return _db_path().parent / "credentials"


def resolve_identity_env(identity_name: str | None) -> dict[str, str]:
    """Build the identity env block for *identity_name*.

    Returns {} when the identity is unknown or data is missing — the
    caller then launches the agent without identity injection and the
    agent must treat the missing keys as a blocked condition.
    """
    if not identity_name:
        return {}

    db = _db_path()
    if not db.exists():
        logger.warning("Hola-Switch db not found at %s", db)
        return {}

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                """SELECT i.id, c.data
                   FROM identities i
                   JOIN identity_credentials c ON c.identity_id = i.id
                   WHERE i.name = ? AND c.credential_type = 'github-account'""",
                (identity_name,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.error("Hola-Switch db read failed: %s", exc)
        return {}

    if row is None:
        logger.warning("identity %r not found in Hola-Switch", identity_name)
        return {}

    identity_id, data_json = row
    try:
        data: dict[str, Any] = json.loads(data_json)
    except json.JSONDecodeError:
        logger.error("identity %r has malformed credential data", identity_name)
        return {}

    git_name = data.get("git_name") or identity_name
    git_email = data.get("git_email") or ""

    # Token lives in a managed env file next to the DB (DB stores only a
    # sha256 fingerprint). Resolve the LIVE value at every launch so
    # token rotation takes effect without touching task state.
    token = ""
    cred_file = _credentials_dir() / identity_id / "github-account.env"
    if cred_file.exists():
        try:
            for line in cred_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
        except OSError as exc:
            logger.error("credential file read failed for %r: %s", identity_name, exc)
    if not token:
        logger.warning("identity %r has no token file at %s", identity_name, cred_file)

    return {
        "GH_TOKEN": token,
        "GITHUB_TOKEN": token,
        "GIT_AUTHOR_NAME": git_name,
        "GIT_COMMITTER_NAME": git_name,
        "GIT_AUTHOR_EMAIL": git_email,
        "GIT_COMMITTER_EMAIL": git_email,
        "HOLA_AGENT_ID": identity_name,
    }
