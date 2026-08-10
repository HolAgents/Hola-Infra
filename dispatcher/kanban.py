"""GitHub ProjectV2 Kanban — move cards via GraphQL API."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from dispatcher.config import get_settings

logger = logging.getLogger(__name__)

MUTATION = """
mutation MoveCard($project: ID!, $item: ID!, $field: ID!, $value: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project
    itemId: $item
    fieldId: $field
    value: { singleSelectOptionId: $value }
  }) {
    clientMutationId
  }
}
"""


class KanbanClient:
    """Move issues/PRs between ProjectV2 Kanban columns."""

    def __init__(self) -> None:
        settings = get_settings()
        self._project_id = settings.github_project_id
        self._field_id = settings.github_status_field_id
        self._client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )

        # status → option_id mapping
        self._status_map: dict[str, str] = {
            "Backlog": settings.kanban_backlog_id,
            "Ready": settings.kanban_ready_id,
            "In progress": settings.kanban_in_progress_id,
            "In review": settings.kanban_in_review_id,
            "Done": settings.kanban_done_id,
        }

    def move_card(self, issue_node_id: str, column: str) -> bool:
        """Move an issue/PR card to a Kanban column by name.

        Returns ``True`` on success; ``False`` when the column is unknown or
        the GraphQL call fails.
        """
        option_id = self._status_map.get(column)
        if not option_id:
            logger.error("unknown kanban column: %s", column)
            return False

        variables = {
            "project": self._project_id,
            "item": issue_node_id,
            "field": self._field_id,
            "value": option_id,
        }

        try:
            resp = self._client.post(
                "/graphql",
                json={"query": MUTATION, "variables": variables},
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                logger.error("graphql error: %s", data["errors"])
                return False
            logger.debug("moved %s → %s", issue_node_id, column)
            return True
        except Exception as exc:
            logger.error("move_card failed: %s", exc)
            return False

    def close(self) -> None:
        self._client.close()
