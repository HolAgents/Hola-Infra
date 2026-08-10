"""Entry point: ``python -m dispatcher`` or ``python -m dispatcher --init-kanban``."""

import logging
import sys

from dispatcher.config import get_settings
from dispatcher.puller import run_loop
from dispatcher.kanban import init_project

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    if "--init-kanban" in sys.argv:
        result = init_project("HolAgents", "Hola Task Board", settings.github_token)
        print("\nPaste this into dispatcher/.env:\n")
        for k, v in result.items():
            print(f"{k.upper()}={v}")
    else:
        run_loop()
