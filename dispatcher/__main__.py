"""Entry point: ``python -m dispatcher`` or ``python -m dispatcher --init-kanban``."""

import logging
import sys

from dispatcher.config import get_settings
from dispatcher.puller import run_loop
from dispatcher.kanban import init_project, fetch_project_config

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _print_env_block(result: dict) -> None:
    print("\nPaste this into dispatcher/.env:\n")
    for k, v in result.items():
        print(f"{k.upper()}={v}")
    missing = [k for k, v in result.items() if not v]
    if missing:
        print(f"\nWARNING: missing option IDs for: {missing}")


if __name__ == "__main__":
    if "--init-kanban" in sys.argv:
        result = init_project("HolAgents", "Hola Task Board", settings.github_token)
        _print_env_block(result)
    elif "--fetch-kanban" in sys.argv:
        try:
            project_id = sys.argv[sys.argv.index("--fetch-kanban") + 1]
        except IndexError:
            print("Usage: python -m dispatcher --fetch-kanban <project_id>")
            sys.exit(2)
        result = fetch_project_config(project_id, settings.github_token)
        _print_env_block(result)
    else:
        run_loop()
