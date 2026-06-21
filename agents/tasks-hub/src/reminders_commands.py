"""Container -> Host command queue for Apple Reminders write-back (idea 5).

Docker can't talk to AppleScript / JXA — so the container appends
commands to `state/reminders-commands.jsonl` and a host-side launchd
watcher pops them and applies via osascript. After application, the
host writes a result line into `state/reminders-commands-log.jsonl`
so the container can confirm the operation later (or via the next
snapshot from reminders-bridge.sh).

Commands:

    {"action": "create", "name": "Buy milk", "list": "AI",
     "due": "2026-06-22T15:00:00Z", "body": "...", "task_id": "..."}

    {"action": "complete", "task_id": "..."}   (resolved by ingestor
                                                ext_id -> reminder)

    {"action": "complete_by_match", "name": "...", "list": "..."}

    {"action": "snooze", "task_id": "...", "until": "2026-06-25"}

Each line gets a `cmd_id` (uuid4) and `enqueued_at` automatically. The
host watcher keeps a byte-offset cursor in
`state/reminders-commands-cursor` so commands are processed exactly
once across watcher restarts.
"""
from __future__ import annotations

import fcntl
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import settings

logger = logging.getLogger(__name__)

_lock = threading.RLock()


def _queue_path() -> Path:
    return Path(settings.db_file).parent / "reminders-commands.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enqueue(action: str, **payload: Any) -> dict[str, Any]:
    """Append a command line. Returns the enqueued envelope."""
    cmd = {
        "cmd_id": uuid.uuid4().hex,
        "action": action,
        "enqueued_at": _now_iso(),
        **payload,
    }
    line = json.dumps(cmd, ensure_ascii=False) + "\n"
    p = _queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with p.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    logger.info("reminders-cmd queued: %s %s", action, cmd["cmd_id"])
    return cmd


def enqueue_create(
    name: str,
    *,
    list_name: str = "AI",
    due: Optional[str] = None,
    body: Optional[str] = None,
    task_id: Optional[str] = None,
    priority: Optional[int] = None,
) -> dict[str, Any]:
    return enqueue(
        "create",
        name=name,
        list=list_name,
        due=due,
        body=body,
        task_id=task_id,
        priority=priority,
    )


def enqueue_complete(*, task_id: str, ext_id: Optional[str] = None) -> dict[str, Any]:
    return enqueue("complete", task_id=task_id, ext_id=ext_id)


def enqueue_snooze(*, task_id: str, until: str) -> dict[str, Any]:
    return enqueue("snooze", task_id=task_id, until=until)


def pending_count() -> int:
    """Number of unprocessed commands by reading cursor vs file size.

    Cheap heuristic: the watcher updates `state/reminders-commands-cursor`
    after each batch. If the queue has grown past that offset, return
    the line count of the tail.
    """
    p = _queue_path()
    if not p.exists():
        return 0
    cursor_path = p.parent / "reminders-commands-cursor"
    cursor = 0
    if cursor_path.exists():
        try:
            cursor = int(cursor_path.read_text(encoding="utf-8").strip() or "0")
        except (ValueError, OSError):
            cursor = 0
    total = p.stat().st_size
    if total <= cursor:
        return 0
    with p.open("rb") as fh:
        fh.seek(cursor)
        tail = fh.read()
    return sum(1 for line in tail.splitlines() if line.strip())


def read_results(limit: int = 50) -> list[dict[str, Any]]:
    """Tail the host-side result log (created by the watcher)."""
    log = Path(settings.db_file).parent / "reminders-commands-log.jsonl"
    if not log.exists():
        return []
    out: list[dict[str, Any]] = []
    with log.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out[-limit:]
