"""Shared pytest fixtures — in-memory SQLite + FastAPI TestClient."""

import json
import os
import tempfile
from pathlib import Path

import pytest

# Override config before any imports that use it
os.environ["GITHUB_WEBHOOK_SECRET"] = "test_secret"
os.environ["API_KEY"] = "test_api_key"
os.environ["DB_PATH"] = ":memory:"  # Will be overridden per test

from fastapi.testclient import TestClient


@pytest.fixture
def app():
    """FastAPI app with a temp SQLite database."""
    from app.config import get_settings, Settings

    # Force fresh settings with temp db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    os.environ["DB_PATH"] = tmp_path

    # Clear settings singleton so it re-reads
    import app.config as _cfg
    _cfg._settings = None

    from main import app

    yield app

    # Cleanup
    try:
        os.unlink(tmp_path)
        os.unlink(tmp_path + "-wal")  # WAL file
        os.unlink(tmp_path + "-shm")  # shared memory file
    except FileNotFoundError:
        pass


@pytest.fixture
def client(app):
    """TestClient for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
def api_key_headers():
    """Headers with valid API key."""
    return {"X-API-Key": "test_api_key"}


# ---------------------------------------------------------------------------
# Sample GitHub payloads
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_issue_opened() -> dict:
    return {
        "action": "opened",
        "issue": {
            "number": 42,
            "node_id": "I_kw_xxx",
            "title": "Test issue",
            "body": "This is a test issue body.",
            "html_url": "https://github.com/HolAgents/Hola-Infra/issues/42",
        },
        "repository": {"full_name": "HolAgents/Hola-Infra"},
        "sender": {"login": "test-user"},
    }


@pytest.fixture
def sample_push() -> dict:
    return {
        "ref": "refs/heads/main",
        "repository": {"full_name": "HolAgents/Hola-Infra"},
        "sender": {"login": "test-user"},
        "commits": [{"id": "abc123", "message": "fix: test"}],
    }
