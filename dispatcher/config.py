"""Dispatcher configuration — reads from environment / .env file."""

from pydantic_settings import BaseSettings


class DispatcherSettings(BaseSettings):
    # ---- FC connection ----
    fc_base_url: str = "http://localhost:9000"
    api_key: str = "change_me"

    # ---- polling ----
    poll_interval_seconds: int = 5
    batch_size: int = 20

    # ---- GitHub (Kanban + gh CLI) ----
    github_token: str = ""
    github_project_id: str = ""          # ProjectV2 node ID
    github_status_field_id: str = ""     # Status field node ID
    kanban_backlog_id: str = ""
    kanban_ready_id: str = ""
    kanban_in_progress_id: str = ""
    kanban_in_review_id: str = ""
    kanban_done_id: str = ""

    # ---- agent filtering ----
    agent_skip_senders: str = "hola-bot"

    # ---- Hola-Switch (local data store) ----
    hola_switch_db_path: str = "~/.cc-switch/cc-switch.db"
    hola_switch_api_url: str = ""       # empty = read the local DB/files directly
    hola_switch_cache_ttl: int = 300    # identity binding cache (seconds)

    # ---- agent execution ----
    claude_bin: str = "claude"          # testable via fake_claude.py
    workspace_root: str = "./workspaces"
    max_ci_resumes: int = 3             # CI-failure resumes before escalation

    # ---- logging ----
    log_level: str = "INFO"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


_settings: DispatcherSettings | None = None


def get_settings() -> DispatcherSettings:
    global _settings
    if _settings is None:
        _settings = DispatcherSettings()
    return _settings
