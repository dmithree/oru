"""Thin coordinator: wraps store mutations and emits matching events.

Use these helpers from HTTP/CLI/ingestor layers so events are never
forgotten. Direct calls to `store.*` are reserved for read paths and
backfill scripts.
"""
from __future__ import annotations

from typing import Any, Optional

from . import events, parsers, recurrence, reminders_commands, store


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

    # Recurrence respawn (idea 9): when a recurring task closes, spawn
    # the next instance with a recalculated due_at. Only on the close
    # transition (not reopen), and only if recurrence is valid.
    if (
        new_status == "done"
        and old_status != "done"
        and recurrence.is_valid(task.get("recurrence"))
    ):
        try:
            _spawn_recurrence(task, agent=agent)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("recurrence spawn failed for %s: %s", task_id, e)

    # Bidirectional Reminders sync (idea 5): if this task originated in
    # Apple Reminders and is moving to done/dropped, enqueue a host
    # command so Reminders.app reflects the closure. The host watcher
    # applies the command via JXA within ~60s.
    src = task.get("source") or ""
    if (
        src.startswith("reminders:")
        and new_status in {"done", "dropped"}
        and old_status not in {"done", "dropped"}
    ):
        try:
            _queue_reminder_close(task, agent=agent)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "reminders queue failed for %s: %s", task_id, e
            )

    return task


def _queue_reminder_close(task: dict[str, Any], *, agent: str) -> None:
    """Enqueue a host-side `complete` command for a reminders-sourced task.

    The host watcher matches on (name, list); we pull both from the raw
    payload the adapter stashed, falling back to the task's text and
    the source suffix if raw is missing.
    """
    raw = task.get("raw") or {}
    name = raw.get("name") or task.get("text") or ""
    list_name = raw.get("list")
    if not list_name:
        # source format: "reminders:list:<LISTNAME>"
        prefix = "reminders:list:"
        src = task.get("source") or ""
        if src.startswith(prefix):
            list_name = src[len(prefix):]
    list_name = list_name or "AI"

    reminders_commands.enqueue(
        "complete",
        task_id=task["id"],
        ext_id=task.get("ext_id"),
        name=name,
        list=list_name,
    )
    events.emit(
        "TaskUpdated",
        task_id=task["id"],
        agent=agent,
        source=task.get("source"),
        payload={"reminders_command": "complete", "list": list_name, "name": name},
    )


def _spawn_recurrence(parent: dict[str, Any], *, agent: str) -> dict[str, Any]:
    """Insert a fresh open instance of `parent` with a new due_at.

    The new task carries the same metadata (text, source, project,
    owner_agent, recurrence, context_tags, cog_type, effort_min,
    priority) so the cycle continues. parent_id points back to the
    just-closed instance so the chain stays traceable.
    """
    from datetime import date
    next_due = recurrence.next_due_from(parent["recurrence"], anchor=date.today()).isoformat()

    spawned = store.create_task(
        parent["text"],
        source=parent.get("source", "manual"),
        status="open",
        priority=parent.get("priority"),
        due_at=next_due,
        due_precision="day",
        context_tags=parent.get("context_tags") or [],
        cog_type=parent.get("cog_type"),
        effort_min=parent.get("effort_min"),
        recurrence=parent.get("recurrence"),
        project=parent.get("project"),
        owner_agent=parent.get("owner_agent"),
        parent_id=parent["id"],
        raw=None,
    )
    events.emit(
        "TaskRecurred",
        task_id=spawned["id"],
        agent=agent,
        source=spawned.get("source"),
        payload={
            "parent_id": parent["id"],
            "due_at": next_due,
            "recurrence": parent.get("recurrence"),
        },
    )
    return spawned


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
