"""Pydantic models — request bodies and response shapes."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Webhook ingest
# ---------------------------------------------------------------------------

class IngestResponse(BaseModel):
    status: str                     # "accepted" | "duplicate" | "filtered"
    delivery_id: Optional[str] = None
    event_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

class ClaimRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    event_types: Optional[list[str]] = None  # e.g. ["issues", "pull_request"]


class ClaimedEvent(BaseModel):
    id: int
    delivery_id: str
    event_type: str
    repo_full_name: str
    payload: Any                     # parsed JSON
    claim_token: str
    received_at: str


class ClaimResponse(BaseModel):
    count: int
    events: list[ClaimedEvent]
    remaining: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Ack
# ---------------------------------------------------------------------------

class AckItem(BaseModel):
    event_id: int
    claim_token: str
    status: str                     # "completed" | "failed"
    agent_id: Optional[str] = None
    message: Optional[str] = None
    # ---- CI Resume 新增 ----
    task_status: Optional[str] = None    # planned | pr_opened | pushed | ci_failed | ci_passed | done | blocked | released
    commit_sha: Optional[str] = None
    session_id: Optional[str] = None
    identity_name: Optional[str] = None
    target_id: Optional[str] = None


class AckRejected(BaseModel):
    event_id: int
    reason: str


class AckRequest(BaseModel):
    results: list[AckItem]


class AckResponse(BaseModel):
    acked: int
    rejected: list[AckRejected]


# ---------------------------------------------------------------------------
# Partial update (PATCH)
# ---------------------------------------------------------------------------

class EventPatchRequest(BaseModel):
    claim_token: str                           # required for ownership check
    task_status: Optional[str] = None
    session_id: Optional[str] = None
    commit_sha: Optional[str] = None
    identity_name: Optional[str] = None
    target_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str                     # "ok"
    db: str                         # "connected" | "error: …"
    net: str = ""                   # optional VPC context (src ip diagnostics)


# ---------------------------------------------------------------------------
# Heartbeat (claim lease refresh)
# ---------------------------------------------------------------------------

class HeartbeatRequest(BaseModel):
    claim_token: str    # required for ownership check
