"""FC Webhook Service — application configuration.

All configuration is read from environment variables / .env file.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ---- required ----
    github_webhook_secret: str      # GitHub webhook HMAC shared secret
    api_key: str                    # shared key between FC and Dispatcher

    # ---- database ----
    db_path: str = "/mnt/nas/events.db"

    # ---- filters (comma-separated, empty = allow all) ----
    allowed_repos: str = ""         # e.g. "HolAgents/Hola-Infra,HolAgents/foo"
    allowed_events: str = ""        # e.g. "push,pull_request,issues"

    # ---- claim lifecycle ----
    claim_ttl_minutes: int = 15
    max_retries: int = 3

    # ---- logging ----
    log_level: str = "INFO"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy-singleton accessor; env file is read once on first call."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
