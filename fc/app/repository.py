"""Events repository — backend selector.

With ``DATABASE_URL`` set, the PostgreSQL backend (PolarDB Serverless)
is used; otherwise the SQLite backend (local dev / legacy NAS) is used.
Both backends expose identical function signatures — see
``repository_sqlite`` and ``repository_pg``.
"""

from __future__ import annotations

from app.config import get_settings

if get_settings().database_url:
    from app.repository_pg import (  # noqa: F401
        _ALLOWED_PATCH_FIELDS,
        ack_batch,
        claim_batch,
        get_event,
        insert_event,
        patch_event,
        query_by_commit,
        query_events,
    )
else:
    from app.repository_sqlite import (  # noqa: F401
        _ALLOWED_PATCH_FIELDS,
        ack_batch,
        claim_batch,
        get_event,
        insert_event,
        patch_event,
        query_by_commit,
        query_events,
    )
