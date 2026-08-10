"""Dispatcher configuration — reads from environment / .env file."""

from pydantic_settings import BaseSettings


class DispatcherSettings(BaseSettings):
    # ---- FC connection ----
    fc_base_url: str                     # FC HTTP trigger URL
    api_key: str                         # shared with FC

    # ---- polling ----
    poll_interval_seconds: int = 5
    batch_size: int = 20

    # ---- GitHub (Kanban + gh CLI) ----
    github_token: str
    github_project_id: str = ""          # ProjectV2 node ID
    github_status_field_id: str = ""     # Status field node ID
    kanban_backlog_id: str = ""
    kanban_ready_id: str = ""
    kanban_in_progress_id: str = ""
    kanban_in_review_id: str = ""
    kanban_done_id: str = ""

    # ---- agent filtering ----
    agent_skip_senders: str = "hola-bot"

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
