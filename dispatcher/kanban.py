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

ADD_ITEM = """
mutation AddItem($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
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

        Returns ``True`` on success; ``False`` when Kanban is not configured,
        the column is unknown, or the GraphQL call fails.
        """
        if not self._project_id or not self._field_id:
            logger.debug("kanban not configured, skipping move_card")
            return False
        option_id = self._status_map.get(column)
        if not option_id:
            logger.warning("unknown kanban column: %s", column)
            return False

        # Ensure the item is in the project first (idempotent — returns the
        # existing item if already added). Covers items outside the template's
        # auto-add workflow scope.
        try:
            resp = self._client.post(
                "/graphql",
                json={
                    "query": ADD_ITEM,
                    "variables": {
                        "project": self._project_id,
                        "content": issue_node_id,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                logger.debug("add item skipped: %s", data["errors"])
        except Exception as exc:
            logger.error("add item failed: %s", exc)

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


# ---------------------------------------------------------------------------
# One-shot init: create org-level ProjectV2 with Status columns
# ---------------------------------------------------------------------------

CREATE_PROJECT = """
mutation($org: ID!, $title: String!) {
  createProjectV2(input: {
    ownerId: $org
    title: $title
  }) {
    projectV2 { id }
  }
}
"""

GET_ORG_ID = """
query($org: String!) {
  organization(login: $org) { id }
}
"""

GET_STATUS_FIELD = """
query($project: ID!) {
  node(id: $project) {
    ... on ProjectV2 {
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
        }
      }
    }
  }
}
"""

ADD_STATUS_OPTION = """
mutation($project: ID!, $field: ID!, $name: String!, $color: String!) {
  createProjectV2SingleSelectFieldOption(input: {
    projectId: $project
    fieldId: $field
    name: $name
    color: $color
  }) {
    clientMutationId
  }
}
"""

DELETE_DEFAULT_OPTIONS = """
mutation($project: ID!, $options: [ProjectV2StatusOption!]!) {
  deleteProjectV2Status(input: {
    projectId: $project
    statusId: $options
  }) {
    clientMutationId
  }
}
"""

STATUS_COLUMNS = [
    ("Backlog",      "GRAY"),
    ("Ready",        "BLUE"),
    ("In progress",  "YELLOW"),
    ("In review",    "PURPLE"),
    ("Done",         "GREEN"),
]


def init_project(org_name: str, project_title: str, token: str) -> dict:
    """Create an org-level ProjectV2 Kanban and return its config IDs.

    Returns a dict ready to paste into ``dispatcher/.env``::

        {
          "github_project_id": "PVT_xxx",
          "github_status_field_id": "PVTSSF_yyy",
          "kanban_backlog_id":       "...",
          "kanban_ready_id":         "...",
          "kanban_in_progress_id":   "...",
          "kanban_in_review_id":     "...",
          "kanban_done_id":          "...",
        }
    """
    client = httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )

    def gql(query: str, variables: dict) -> dict:
        resp = client.post("/graphql", json={"query": query, "variables": variables})
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data["data"]

    # 1. Get org node ID
    data = gql(GET_ORG_ID, {"org": org_name})
    org_id = data["organization"]["id"]
    print(f"Org ID: {org_id}")

    # 2. Create project
    data = gql(CREATE_PROJECT, {"org": org_id, "title": project_title})
    project_id = data["createProjectV2"]["projectV2"]["id"]
    print(f"Project ID: {project_id}")

    # 3. Get Status field (GitHub auto-creates one with default options)
    data = gql(GET_STATUS_FIELD, {"project": project_id})
    fields = data["node"]["fields"]["nodes"]
    status_field = next((f for f in fields if f["name"] == "Status"), None)
    if not status_field:
        raise RuntimeError("Status field not found on new project")
    field_id = status_field["id"]
    print(f"Status field ID: {field_id}")

    # 4. GitHub auto-creates "Todo", "In Progress", "Done" — rename/replace
    # Strategy: add our 5 columns, then optionally delete originals
    existing = {o["name"]: o["id"] for o in status_field["options"]}
    print(f"Existing options: {list(existing.keys())}")

    result: dict[str, str] = {
        "github_project_id": project_id,
        "github_status_field_id": field_id,
    }

    for name, color in STATUS_COLUMNS:
        if name in existing:
            result[f"kanban_{_slug(name)}"] = existing[name]
            print(f"  {name}: {existing[name]} (existing)")
        else:
            gql(ADD_STATUS_OPTION, {
                "project": project_id,
                "field": field_id,
                "name": name,
                "color": color,
            })
            # Re-fetch to get the new option ID
            data = gql(GET_STATUS_FIELD, {"project": project_id})
            opts = data["node"]["fields"]["nodes"][0]["options"]
            opt = next(o for o in opts if o["name"] == name)
            result[f"kanban_{_slug(name)}"] = opt["id"]
            print(f"  {name}: {opt['id']} (created)")

    client.close()
    return result


def fetch_project_config(project_id: str, token: str) -> dict[str, str]:
    """Read config IDs from an existing ProjectV2 (e.g. one created with
    GitHub's 5-column Kanban template) — no creation, no column edits.

    Returns a dict ready to paste into ``dispatcher/.env``, with empty
    values for any of the expected columns missing on the project.
    """
    client = httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )

    def gql(query: str, variables: dict) -> dict:
        resp = client.post("/graphql", json={"query": query, "variables": variables})
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data["data"]

    try:
        data = gql(GET_STATUS_FIELD, {"project": project_id})
        fields = data["node"]["fields"]["nodes"]
        status_field = next((f for f in fields if f.get("name") == "Status"), None)
        if not status_field:
            raise RuntimeError("Status field not found on project")

        options = {o["name"]: o["id"] for o in status_field.get("options", [])}
        print(f"Status field ID: {status_field['id']}")
        print(f"Existing options: {sorted(options.keys())}")

        result: dict[str, str] = {
            "github_project_id": project_id,
            "github_status_field_id": status_field["id"],
        }
        for name, _ in STATUS_COLUMNS:
            result[f"kanban_{_slug(name)}"] = options.get(name, "")
        return result
    finally:
        client.close()


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_")

