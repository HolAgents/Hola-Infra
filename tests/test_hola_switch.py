"""Hola-Switch identity env resolution against a fixture data store."""

import json
import sqlite3

from dispatcher import hola_switch
from dispatcher.config import get_settings


def _make_fixture(tmp_path, identity_name="holagent001", git_name="holagent001",
                  git_email="hola001@agent.qq.com", token="ghp_testtoken123"):
    db = tmp_path / "cc-switch.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE identities (id TEXT PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE identity_credentials (
            identity_id TEXT, credential_type TEXT,
            data TEXT, created_at TEXT
        );
    """)
    conn.execute(
        "INSERT INTO identities VALUES ('id-1', ?, 'test')", (identity_name,),
    )
    conn.execute(
        "INSERT INTO identity_credentials VALUES ('id-1', 'github-account', ?, '2026-01-01')",
        (json.dumps({
            "token": "sha256:fingerprint",
            "git_name": git_name,
            "git_email": git_email,
            "ssh_key_path": "",
        }),),
    )
    conn.commit()
    conn.close()

    cred_dir = tmp_path / "credentials" / "id-1"
    cred_dir.mkdir(parents=True)
    (cred_dir / "github-account.env").write_text(
        f"# idswitch: id-1 -- managed\nGITHUB_TOKEN={token}\n# /idswitch\n",
        encoding="utf-8",
    )

    settings = get_settings()
    settings.hola_switch_db_path = str(db)
    return db


def test_resolve_identity_env_full(tmp_path):
    _make_fixture(tmp_path)
    env = hola_switch.resolve_identity_env("holagent001")
    assert env["GH_TOKEN"] == "ghp_testtoken123"
    assert env["GITHUB_TOKEN"] == "ghp_testtoken123"
    assert env["GIT_AUTHOR_NAME"] == "holagent001"
    assert env["GIT_COMMITTER_NAME"] == "holagent001"
    assert env["GIT_AUTHOR_EMAIL"] == "hola001@agent.qq.com"
    assert env["GIT_COMMITTER_EMAIL"] == "hola001@agent.qq.com"
    assert env["HOLA_AGENT_ID"] == "holagent001"


def test_resolve_unknown_identity_returns_empty(tmp_path):
    _make_fixture(tmp_path)
    assert hola_switch.resolve_identity_env("nobody") == {}


def test_resolve_missing_db_returns_empty(tmp_path):
    settings = get_settings()
    settings.hola_switch_db_path = str(tmp_path / "does-not-exist.db")
    assert hola_switch.resolve_identity_env("holagent001") == {}


def test_resolve_missing_token_returns_env_with_empty_token(tmp_path):
    db = _make_fixture(tmp_path)
    # remove the credential file → token empty, names still resolved
    (db.parent / "credentials" / "id-1" / "github-account.env").unlink()
    env = hola_switch.resolve_identity_env("holagent001")
    assert env["GH_TOKEN"] == ""
    assert env["GIT_AUTHOR_NAME"] == "holagent001"


def test_resolve_none_identity(tmp_path):
    assert hola_switch.resolve_identity_env(None) == {}
