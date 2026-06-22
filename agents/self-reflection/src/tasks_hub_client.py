"""HTTP client for other Hermes agents to emit tasks (idea 16).

Bureau agents — health, gtv-tracker, self-reflection, thoughts-capture
— used to publish their followups via per-section keys in
personal-context.json (`health.next_followup`, `visa.deadline`,
`self.homework_open`). That made tasks-hub a special-case aggregator
of N agent-specific schemas.

The new contract: agents emit `TaskCreated` events through this tiny
client. tasks-hub stores them like any other source, derived views
replace the bespoke context keys.

Usage from inside another agent container (same docker network):

    from tasks_hub_client import emit_task   # vendored copy

    emit_task(
        text="Анализы крови",
        owner_agent="health",
        recurrence="every:3m",
        context_tags=["@phone"],
        priority="P2",
        base_url="http://oru-tasks-hub:8004",   # default
    )

This file is INTENDED to be vendored — copy it as
`tasks_hub_client.py` next to each agent's source. Keeping it stdlib-
only means agents don't need an extra dependency.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = os.environ.get("TASKS_HUB_URL", "http://oru-tasks-hub:8004")


class TasksHubError(RuntimeError):
    pass


def _post(path: str, body: dict[str, Any], *, base_url: str = DEFAULT_BASE_URL,
          timeout: float = 5.0) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    req = Request(
        url,
        method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise TasksHubError(f"HTTP {e.code} from {url}: {detail}") from None
    except URLError as e:
        raise TasksHubError(f"connect failed to {url}: {e}") from None


def _get(path: str, *, base_url: str = DEFAULT_BASE_URL,
         timeout: float = 5.0) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise TasksHubError(f"HTTP {e.code} from {url}: {detail}") from None
    except URLError as e:
        raise TasksHubError(f"connect failed to {url}: {e}") from None


def list_open_tasks(
    owner_agent: str,
    *,
    statuses: Iterable[str] = ("open", "next", "doing", "inbox"),
    limit: int = 100,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Return open tasks owned by the given agent. Used for LLM-driven
    semantic dedup: the agent passes this list as context so the model
    can match a new homework item against an existing one.

    tasks-hub /tasks accepts multi-status via repeated ?status= params,
    so we send one request per call instead of N round-trips."""
    from urllib.parse import urlencode
    params = [("owner_agent", owner_agent), ("limit", str(limit))]
    for s in statuses:
        params.append(("status", s))
    q = urlencode(params)
    try:
        resp = _get(f"/tasks?{q}", base_url=base_url, timeout=timeout)
    except TasksHubError:
        logger.exception("list_open_tasks failed for owner=%s", owner_agent)
        return []
    return list(resp.get("tasks") or resp.get("items") or [])


def emit_task(
    text: str,
    *,
    owner_agent: str,
    source: Optional[str] = None,
    status: str = "open",
    ext_id: Optional[str] = None,
    due_at: Optional[str] = None,
    priority: Optional[str] = None,
    context_tags: Optional[Iterable[str]] = None,
    cog_type: Optional[str] = None,
    effort_min: Optional[int] = None,
    recurrence: Optional[str] = None,
    project: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Create a task in tasks-hub. Returns the created task row.

    `source` defaults to `agent:<owner_agent>`. `owner_agent` is
    REQUIRED — it's how derived views (e.g. "weeks until GTV
    submission") locate the agent's tasks without grep-ing source."""
    if not owner_agent:
        raise ValueError("owner_agent is required")

    body: dict[str, Any] = {
        "text": text,
        "source": source or f"agent:{owner_agent}",
        "status": status,
        "owner_agent": owner_agent,
        "agent": owner_agent,  # author of the event in the log
    }
    if ext_id:
        body["ext_id"] = ext_id
    if due_at:
        body["due_at"] = due_at
        body["due_precision"] = "day"
    if priority:
        body["priority"] = priority
    if context_tags:
        body["context_tags"] = list(context_tags)
    if cog_type:
        body["cog_type"] = cog_type
    if effort_min is not None:
        body["effort_min"] = effort_min
    if recurrence:
        body["recurrence"] = recurrence
    if project:
        body["project"] = project

    resp = _post("/tasks", body, base_url=base_url, timeout=timeout)
    return resp.get("task") or resp


def complete_task(
    task_id: str,
    *,
    completed_via: str,
    owner_agent: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 5.0,
) -> dict[str, Any]:
    return _post(
        f"/tasks/{task_id}/status",
        {"to": "done", "completed_via": completed_via, "agent": owner_agent},
        base_url=base_url, timeout=timeout,
    )


def upsert_recurring(
    text: str,
    *,
    owner_agent: str,
    recurrence: str,
    due_at: Optional[str] = None,
    ext_id: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
    **fields: Any,
) -> dict[str, Any]:
    """Convenience: create a recurring task if no task with this
    `ext_id` exists, otherwise no-op. Useful for agents that want to
    "ensure" their followup exists without duplicating on every run."""
    if not ext_id:
        raise ValueError("upsert_recurring requires ext_id for idempotency")
    return emit_task(
        text,
        owner_agent=owner_agent,
        source=f"agent:{owner_agent}",
        ext_id=ext_id,
        due_at=due_at,
        recurrence=recurrence,
        base_url=base_url,
        **fields,
    )
