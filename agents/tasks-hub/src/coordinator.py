"""Thin coordinator: wraps store mutations and emits matching events.

Use these helpers from HTTP/CLI/ingestor layers so events are never
forgotten. Direct calls to `store.*` are reserved for read paths and
backfill scripts.
"""
from __future__ import annotations

from typing import Any, Optional

from . import events, parsers, store


def create(
    text: str,
    *,
    source: str,
    agent: str = "tasks-hub",
    status: str = "open",
    enrich: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    """Create a task. When `enrich=True` (default), the text is run
    through parsers.parse_metadata to populate due_at, context_tags,
    effort_min, cog_type, priority, recurrence — and stripped of those
    inline tokens so display stays clean. Explicit kwargs always win
    over parsed values."""
    stored_text = text
    if enrich:
        parsed = parsers.parse_metadata(text)
        for k, v in parsed.items():
            if k not in fields or fields.get(k) is None:
                fields[k] = v
        stored_text = parsers.clean_text(text) or text  # never empty out

    task = store.create_task(stored_text, source=source, status=status, **fields)
    payload = {"text": stored_text, "status": status}
    payload.update({k: v for k, v in fields.items() if v is not None})
    events.emit(
        "TaskCreated",
        task_id=task["id"],
        agent=agent,
        source=source,
        payload=payload,
    )
    return task


def update(task_id: str, *, agent: str = "tasks-hub", **fields: Any) -> dict[str, Any]:
    task = store.update_task(task_id, **fields)
    events.emit(
        "TaskUpdated",
        task_id=task_id,
        agent=agent,
        source=task.get("source"),
        payload={k: v for k, v in fields.items()},
    )
    return task


def change_status(
    task_id: str,
    new_status: str,
    *,
    agent: str = "tasks-hub",
    completed_via: Optional[str] = None,
    defer_until: Optional[str] = None,
    blocked_by: Optional[str] = None,
    waiting_on: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    task, old_status = store.set_status(
        task_id,
        new_status,
        completed_via=completed_via,
        defer_until=defer_until,
        blocked_by=blocked_by,
        waiting_on=waiting_on,
    )

    # Emit a generic status-change event plus a specific one when
    # appropriate. Listeners can subscribe to whichever fits.
    payload: dict[str, Any] = {"from": old_status, "to": new_status}
    if completed_via:
        payload["completed_via"] = completed_via
    if defer_until:
        payload["defer_until"] = defer_until
    if blocked_by:
        payload["blocked_by"] = blocked_by
    if waiting_on:
        payload["waiting_on"] = waiting_on
    if reason:
        payload["reason"] = reason

    events.emit(
        "TaskStatusChanged",
        task_id=task_id,
        agent=agent,
        source=task.get("source"),
        payload=payload,
    )

    specific = _SPECIFIC_EVENT.get(new_status)
    if specific and old_status != new_status:
        events.emit(
            specific,
            task_id=task_id,
            agent=agent,
            source=task.get("source"),
            payload=payload,
        )
    if old_status == "done" and new_status != "done":
        events.emit(
            "TaskReopened",
            task_id=task_id,
            agent=agent,
            source=task.get("source"),
            payload=payload,
        )

    return task


_SPECIFIC_EVENT = {
    "done":     "TaskCompleted",
    "deferred": "TaskDeferred",
    "blocked":  "TaskBlocked",
    "waiting":  "TaskWaiting",
    "dropped":  "TaskDropped",
}


def triage(task_id: str, decision: str, *, agent: str = "user") -> dict[str, Any]:
    """inbox -> open|dropped via explicit triage decision."""
    if decision not in {"open", "dropped"}:
        raise ValueError(f"triage decision must be 'open' or 'dropped', got {decision!r}")
    task, old_status = store.set_status(task_id, decision)
    events.emit(
        "TaskTriaged",
        task_id=task_id,
        agent=agent,
        source=task.get("source"),
        payload={"from": old_status, "to": decision},
    )
    return task


def delete(task_id: str, *, agent: str = "tasks-hub", reason: Optional[str] = None) -> bool:
    """Tombstone the task. Prefer change_status(..., 'dropped')."""
    task = store.get_task(task_id)
    deleted = store.delete_task(task_id)
    if deleted:
        events.emit(
            "TaskDeleted",
            task_id=task_id,
            agent=agent,
            source=(task or {}).get("source"),
            payload={"reason": reason} if reason else {},
        )
    return deleted
