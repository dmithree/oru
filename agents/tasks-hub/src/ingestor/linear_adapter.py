"""Linear adapter — Phase 1.5: read all of Дима's open assigned issues.

Linear is queried via GraphQL using the user's personal API token.
Phase 1.5 is READ-ONLY: pulls every issue assigned to the viewer that
isn't completed/canceled and ingests them as RawTasks with
ext_id=identifier (e.g., "ENG-42"). Closing or modifying issues in
Linear is NOT done from tasks-hub yet — that's Phase 1.6 once we have
a clear answer for "if a task transitions to done in the store via
debrief, should Linear see it?" (yes, but only for issues whose source
is linear:).

Token lives in settings.linear_api_key (LINEAR_API_KEY env in
secrets/tasks-hub.env). Without the token, the adapter logs once and
yields nothing — same shape as the Phase 1 stub so the runner can list
it without crashing.

Why we override the legacy `LINEAR_ALLOWED_PATHS=['prototypes/design-system']`
whitelist from personal-agent's tasks_sync.py: that whitelist was a
PUSH filter (which markdown files map to Linear issues). For tasks-hub
PULL we want everything assigned to Дима across all teams.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config import settings
from .base import RawTask

logger = logging.getLogger(__name__)

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

_QUERY = """
query AssignedOpenIssues($cursor: String) {
  viewer {
    assignedIssues(
      filter: {
        state: { type: { nin: ["completed", "canceled"] } }
      }
      first: 50
      after: $cursor
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        identifier
        title
        url
        priority
        priorityLabel
        dueDate
        updatedAt
        estimate
        state { name type }
        team { key name }
        project { name }
        labels { nodes { name } }
      }
    }
  }
}
"""


def _priority_to_p(linear_priority: int | None) -> str | None:
    # Linear: 0 = No priority, 1 = Urgent, 2 = High, 3 = Medium, 4 = Low
    return {1: "P0", 2: "P1", 3: "P2", 4: "P3"}.get(linear_priority or 0)


def _state_to_status(state_type: str) -> str:
    # Linear state types: triage, backlog, unstarted, started, completed, canceled
    return {
        "triage":    "inbox",
        "backlog":   "open",
        "unstarted": "open",
        "started":   "doing",
        # completed / canceled filtered out at query level
    }.get(state_type or "", "open")


def _post_gql(api_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = Request(
        LINEAR_GRAPHQL_URL,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key,   # Linear PATs go in raw Authorization (no Bearer)
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Linear HTTP {e.code}: {detail}") from None
    except URLError as e:
        raise RuntimeError(f"Linear network: {e}") from None


@dataclass
class LinearAdapter:
    name: str = "linear"
    max_pages: int = 10           # safety cap (500 issues max per run)
    api_key: str = field(default_factory=lambda: settings.linear_api_key)

    def read(self) -> Iterator[RawTask]:
        if not self.api_key:
            logger.info("linear adapter: LINEAR_API_KEY not set, skipping")
            return

        cursor: str | None = None
        page = 0
        total = 0
        while page < self.max_pages:
            page += 1
            try:
                resp = _post_gql(self.api_key, _QUERY, {"cursor": cursor})
            except RuntimeError as e:
                logger.warning("linear adapter: %s", e)
                return
            if "errors" in resp:
                logger.warning("linear adapter: GraphQL errors %r", resp["errors"])
                return

            viewer = (resp.get("data") or {}).get("viewer") or {}
            conn = viewer.get("assignedIssues") or {}
            nodes = conn.get("nodes") or []

            for n in nodes:
                identifier = n.get("identifier") or ""
                if not identifier:
                    continue
                state = n.get("state") or {}
                state_type = state.get("type") or ""
                team = n.get("team") or {}
                project = n.get("project") or {}
                labels = [lbl.get("name") for lbl in ((n.get("labels") or {}).get("nodes") or []) if lbl.get("name")]

                overrides: dict[str, Any] = {}
                pr = _priority_to_p(n.get("priority"))
                if pr:
                    overrides["priority"] = pr
                if n.get("dueDate"):
                    overrides["due_at"] = n["dueDate"]
                    overrides["due_precision"] = "day"
                # Map Linear labels to context tags (only the @-prefixed ones; ignore the rest)
                context_tags = [f"@{lbl.lstrip('@')}" for lbl in labels if lbl.startswith("@")]
                if context_tags:
                    overrides["context_tags"] = context_tags

                yield RawTask(
                    text=n.get("title") or identifier,
                    source=f"linear:{team.get('key') or 'X'}",
                    status=_state_to_status(state_type),
                    ext_id=identifier,
                    project=project.get("name"),
                    owner_agent=None,
                    raw={
                        "id": n.get("id"),
                        "url": n.get("url"),
                        "state": state.get("name"),
                        "state_type": state_type,
                        "team": team,
                        "labels": labels,
                        "estimate": n.get("estimate"),
                        "priorityLabel": n.get("priorityLabel"),
                        "updatedAt": n.get("updatedAt"),
                    },
                    overrides=overrides,
                )
                total += 1

            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        logger.info("linear adapter: ingested %d issues across %d page(s)", total, page)
