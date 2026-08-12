"""Ops recovery endpoints (API-key auth)."""

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.security import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_api_key)],
)


@router.post("/reset-db", summary="Rename the events DB away so the next connection recreates it")
def reset_db():
    """Recover from a corrupted SQLite file on NAS (Hola-Infra#34).

    Renames events.db (plus -wal/-shm siblings) to ``*.corrupt-<ts>``.
    Instances holding open handles keep working on the old inode until
    recycled; new connections create a fresh database. Event data is
    re-deliverable via GitHub webhook redelivery (delivery_id dedup).
    """
    settings = get_settings()
    db = Path(settings.db_path)
    ts = int(time.time())
    moved: list[str] = []
    for candidate in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        if candidate.exists():
            target = Path(str(candidate) + f".corrupt-{ts}")
            candidate.rename(target)
            moved.append(str(target))
    logger.warning("reset-db: renamed %s", moved)
    return {"status": "reset", "renamed": moved}
