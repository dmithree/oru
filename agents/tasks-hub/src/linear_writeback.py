"""Linear write-back (Phase 1.6).

When a task with source=linear:* transitions to done/dropped in the
store (via debrief, manual close, or any other channel), this module
mirrors the closure to Linear so the issue board stays in sync.

Same one-direction safety as the Reminders bridge: we only ever push
the CLOSE event for tasks Linear knows about (have an ext_id). We
never re-open Linear issues from the store (low value, high blast
radius if state inference is wrong).

The "done" state in Linear is per-team — every team has its own
workflow with different state IDs. We resolve it lazily: first
mutation per team queries `workflowStates(filter: {team: {key: ...}})`
and caches the first `type=completed` state ID for the rest of the
process lifetime.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings

logger = logging.getLogger(__name__)

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

_completed_state_cache: dict[str, str] = {}
_cancelled_state_cache: dict[str, str] = {}
_cache_lock = threading.RLock()


def _post_gql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    api_key = settings.linear_api_key
    if not api_key:
        raise RuntimeError("LINEAR_API_KEY not set; cannot write back to Linear")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = Request(
        LINEAR_GRAPHQL_URL,
        method="POST",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": api_key},
    )
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Linear HTTP {e.code}: {detail}") from None
    except URLError as e:
        raise RuntimeError(f"Linear network: {e}") from None


_STATE_QUERY = """
query StatesByTeam($key: String!, $kind: String!) {
  workflowStates(
    filter: { team: { key: { eq: $key } }, type: { eq: $kind } }
    first: 1
  ) {
    nodes { id name type }
  }
}
"""


def _resolve_state(team_key: str, kind: str) -> Optional[str]:
    """Return the first workflowState of `kind` (completed|canceled) for
    the given team. Cached per process."""
    cache = _completed_state_cache if kind == "completed" else _cancelled_state_cache
    with _cache_lock:
        if team_key in cache:
            return cache[team_key]
    resp = _post_gql(_STATE_QUERY, {"key": team_key, "kind": kind})
    if "errors" in resp:
        logger.warning("linear-writeback: state lookup failed for %s/%s: %r",
                       team_key, kind, resp["errors"])
        return None
    nodes = ((resp.get("data") or {}).get("workflowStates") or {}).get("nodes") or []
    if not nodes:
        logger.warning("linear-writeback: no %s state for team %s", kind, team_key)
        return None
    state_id = nodes[0]["id"]
    with _cache_lock:
        cache[team_key] = state_id
    return state_id


_TEAM_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)-")


def _team_key_from_identifier(identifier: str) -> Optional[str]:
    m = _TEAM_KEY_RE.match(identifier or "")
    return m.group(1) if m else None


_ISSUE_QUERY = """
query IssueByIdentifier($identifier: String!) {
  issueVcsBranchSearch(branchName: $identifier) { id identifier team { key } }
  issues(filter: { number: { eq: 0 } }) { nodes { id } }
}
"""

# Simpler: resolve via identifier in a direct filter
_ISSUE_LOOKUP = """
query ResolveIssue($id: String!) {
  issue(id: $id) { id identifier team { key } }
}
"""

# When ext_id is the identifier (ENG-42), we can't pass it directly to
# `issue(id:)` — that field expects a UUID. Use the issues() query with
# a number+team filter, or store the UUID at ingest time. We stored the
# UUID in raw.id at ingest, so callers can pass it through.

_ISSUE_UPDATE = """
mutation IssueUpdate($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) {
    success
    issue { id identifier state { name type } }
  }
}
"""


def close_issue(task: dict[str, Any], *, as_kind: str = "completed") -> dict[str, Any]:
    """Mark the Linear issue underlying `task` as completed (or cancelled).

    `task` is a store row. We expect:
      - source like "linear:ENG"
      - ext_id like "ENG-42"
      - raw["id"] holds the UUID we got from the GraphQL list response

    Returns {ok, ...details} or {ok: False, error}."""
    src = task.get("source") or ""
    if not src.startswith("linear:"):
        return {"ok": False, "error": f"task not linear-sourced (source={src!r})"}
    ext_id = task.get("ext_id")
    if not ext_id:
        return {"ok": False, "error": "task has no ext_id"}
    raw = task.get("raw") or {}
    uuid = raw.get("id")
    if not uuid:
        return {"ok": False, "error": "task.raw.id (Linear UUID) missing; re-ingest to capture"}

    team_key = _team_key_from_identifier(ext_id)
    if not team_key:
        return {"ok": False, "error": f"could not extract team key from {ext_id!r}"}

    try:
        state_id = _resolve_state(team_key, as_kind)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if not state_id:
        return {"ok": False, "error": f"no {as_kind} state for team {team_key}"}

    try:
        resp = _post_gql(_ISSUE_UPDATE, {"id": uuid, "stateId": state_id})
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if "errors" in resp:
        return {"ok": False, "error": f"GraphQL: {resp['errors']}"}
    update = ((resp.get("data") or {}).get("issueUpdate") or {})
    if not update.get("success"):
        return {"ok": False, "error": "issueUpdate returned success=false"}
    issue = update.get("issue") or {}
    return {
        "ok": True,
        "identifier": issue.get("identifier"),
        "state": (issue.get("state") or {}).get("name"),
        "state_type": (issue.get("state") or {}).get("type"),
    }
