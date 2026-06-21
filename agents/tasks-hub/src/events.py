"""Append-only event log (idea 2).

Every mutation goes through emit(). Events are written line-by-line as
JSON to settings.events_file with an exclusive POSIX lock (so other
agents / readers can tail safely). Format per line:

    {"ts": "ISO", "kind": "TaskCreated|TaskUpdated|...", "task_id": "...",
     "agent": "tasks-hub|reminders-bridge|user|...", "source": "...",
     "payload": {...}}

The log is the durable record; the SQLite store is a derived
materialization. If state ever drifts, the log is the source of truth.
"""
from __future__ import annotations

import fcntl
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import settings

logger = logging.getLogger(__name__)

KINDS = (
    "TaskCreated",
    "TaskUpdated",
    "TaskStatusChanged",   # generic status transition
    "TaskCompleted",       # convenience: status_changed to done
    "TaskReopened",        # convenience: from done -> open
    "TaskDeferred",        # status -> deferred (defer_until in payload)
    "TaskBlocked",         # status -> blocked (blocked_by in payload)
    "TaskWaiting",         # status -> waiting (waiting_on in payload)
    "TaskDropped",         # status -> dropped
    "TaskTriaged",         # inbox -> open|dropped (payload: triage_decision)
    "TaskRecurred",        # spawn of a new instance from a recurring done task
    "TaskMerged",          # dedup: payload {keeper_id, dropped_id}
    "TaskDeleted",         # hard delete (rare; tombstone for audit)
)

_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path() -> Path:
    return Path(settings.events_file)


def emit(
    kind: str,
    *,
    task_id: Optional[str] = None,
    agent: str = "tasks-hub",
    source: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Append one event line. Returns the event dict."""
    if kind not in KINDS:
        raise ValueError(f"unknown event kind: {kind!r}")
    evt: dict[str, Any] = {
        "ts": _now_iso(),
        "kind": kind,
        "task_id": task_id,
        "agent": agent,
        "source": source,
        "payload": payload or {},
    }
    line = json.dumps(evt, ensure_ascii=False) + "\n"

    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with p.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return evt


def tail(limit: int = 50, kind: Optional[str] = None) -> list[dict[str, Any]]:
    """Read last `limit` events, newest last. Skips malformed lines.

    For Phase 0 this scans the whole file; that's fine for the volumes
    we expect (a few thousand events). If the log grows past a few MB
    we'll add periodic rotation in a later phase.
    """
    p = _path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind and evt.get("kind") != kind:
                continue
            out.append(evt)
    if len(out) > limit:
        out = out[-limit:]
    return out


def iter_events() -> Iterator[dict[str, Any]]:
    p = _path()
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def count() -> int:
    p = _path()
    if not p.exists():
        return 0
    n = 0
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                n += 1
    return n
