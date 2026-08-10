"""Identity resolver — look up agent configuration from Hola-Switch.

Each agent_type routing key maps to one or more identities registered in the
Hola-Switch identity system.  An identity defines *who* the agent is when it
calls GitHub (gh CLI), what permissions / repo scope it has, and how it
should be invoked.

In production this queries the Hola-Switch API; for now it is backed by a
local YAML file that mirrors the Switch registry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentIdentity:
    """Configuration for a single coding-agent instance."""

    agent_id: str              # unique identifier
    agent_type: str            # routing key: triage | review | quality | response | ci-debug
    display_name: str
    gh_user: str               # GitHub username the agent acts as
    repo_scope: list[str] = field(default_factory=list)  # repos this agent handles
    claude_prompt_template: str = ""  # custom prompt prefix
    # ---- CI Resume ----
    target_id: str = "claude-code"   # agent adapter type (claude-code, hermes, etc.)
    identity_name: str = ""          # Hola-Switch identity name (defaults to gh_user)


# ---------------------------------------------------------------------------
# Built-in identity registry (mirrors Hola-Switch)
# ---------------------------------------------------------------------------

_IDENTITIES: dict[str, list[AgentIdentity]] = {
    "triage": [
        AgentIdentity(
            agent_id="triage-01",
            agent_type="triage",
            display_name="Issue Triage Agent",
            gh_user="hola-bot",
            repo_scope=["HolAgents/*"],
            target_id="claude-code",
            identity_name="holagent001",
        ),
    ],
    "review": [
        AgentIdentity(
            agent_id="review-01",
            agent_type="review",
            display_name="PR Review Agent",
            gh_user="hola-bot",
            repo_scope=["HolAgents/*"],
            target_id="claude-code",
            identity_name="holagent001",
        ),
    ],
    "quality": [
        AgentIdentity(
            agent_id="quality-01",
            agent_type="quality",
            display_name="Commit Quality Agent",
            gh_user="hola-bot",
            repo_scope=["HolAgents/*"],
            target_id="claude-code",
            identity_name="holagent001",
        ),
    ],
    "response": [
        AgentIdentity(
            agent_id="response-01",
            agent_type="response",
            display_name="Comment Response Agent",
            gh_user="hola-bot",
            repo_scope=["HolAgents/*"],
            target_id="claude-code",
            identity_name="holagent001",
        ),
    ],
    "ci-debug": [
        AgentIdentity(
            agent_id="ci-debug-01",
            agent_type="ci-debug",
            display_name="CI Debug Agent",
            gh_user="hola-bot",
            repo_scope=["HolAgents/*"],
            target_id="claude-code",
            identity_name="holagent001",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def resolve(agent_type: str, repo: str = "") -> Optional[AgentIdentity]:
    """Return the first matching identity for *agent_type* and optional *repo*.

    Returns ``None`` when no identity is registered for the type.
    """
    candidates = _IDENTITIES.get(agent_type, [])
    if not candidates:
        logger.warning("no identity registered for agent_type=%s", agent_type)
        return None

    # Filter by repo scope (if repo is specified)
    for ident in candidates:
        if not ident.repo_scope:
            return ident
        for scope in ident.repo_scope:
            if _repo_matches(repo, scope):
                return ident

    logger.warning("no identity for agent_type=%s matching repo=%s", agent_type, repo)
    return None


def _repo_matches(repo: str, scope: str) -> bool:
    """Simple glob match: ``owner/*`` matches any repo under that owner."""
    if scope.endswith("/*"):
        owner = scope[:-2]
        return repo.startswith(owner + "/")
    return repo == scope


# ---------------------------------------------------------------------------
# Re-bind detection (CI resume)
# ---------------------------------------------------------------------------


def get_current_target(identity_name: str) -> str | None:
    """Return the currently bound *target_id* for *identity_name*.

    Current implementation searches the local in-memory registry.
    When Hola-Switch API is available, this will be swapped to an HTTP
    call with a local TTL cache.
    """
    for identities in _IDENTITIES.values():
        for ident in identities:
            if ident.identity_name == identity_name:
                return ident.target_id
    logger.warning("no identity found for identity_name=%s", identity_name)
    return None
