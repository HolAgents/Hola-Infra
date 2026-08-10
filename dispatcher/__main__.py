"""Entry point: ``python -m dispatcher``."""

import logging

from dispatcher.config import get_settings
from dispatcher.puller import run_loop

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    run_loop()
