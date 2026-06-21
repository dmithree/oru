"""Reminders adapter — reads state/reminders.json populated by the host bridge.

Phase 1 is read-only: the bridge writes a snapshot of Apple Reminders
every 15 minutes and we ingest it. Phase 3 adds the command-queue side
(write-back to Reminders.app via JXA on the host).

`ext_id` is set to a deterministic hash of (list, name, due) since the
host bridge does not currently emit a stable Reminder UUID. When we
add UUIDs in Phase 3, swap the hash for the real id and ingestion will
auto-link via `find_by_ext_id`.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .base import RawTask

logger = logging.getLogger(__name__)


def _stable_ext_id(name: str, list_name: str, due: Optional[str]) -> str:
    payload = f"{list_name}|{name}|{due or ''}".encode("utf-8")
    return "rem-" + hashlib.sha1(payload).hexdigest()[:16]


def _normalize_due(due: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not due:
        return None, None
    try:
        dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date().isoformat(), "day"
    except Exception:
        logger.warning("reminders: bad due format %r", due)
        return None, None


@dataclass
class RemindersAdapter:
    name: str = "reminders"
    file_path: Path = field(default_factory=lambda: Path("/opt/state/reminders.json"))
    list_to_context: dict[str, str] = field(default_factory=lambda: {
        # Map Reminders.app list names to default context tags. Adjust per user.
        "AI": "@ai",
        "Покупки": "@shopping",
        "Звонки": "@phone",
        "Дом": "@home",
        "Inbox": "@inbox",
    })

    def read(self) -> Iterator[RawTask]:
        if not self.file_path.exists():
            logger.info("reminders adapter: snapshot file missing at %s", self.file_path)
            return
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("reminders adapter: parse failed")
            return

        if data.get("error") and not data.get("reminders"):
            logger.info("reminders adapter: snapshot reported error %r and is empty", data["error"])
            return

        for r in data.get("reminders", []):
            name = (r.get("name") or "").strip()
            if not name:
                continue
            list_name = (r.get("list") or "").strip()
            due_raw = r.get("due")
            due_at, due_precision = _normalize_due(due_raw)

            overrides: dict[str, Any] = {}
            if due_at:
                overrides["due_at"] = due_at
                overrides["due_precision"] = due_precision

            tag = self.list_to_context.get(list_name)
            if tag:
                overrides["context_tags"] = [tag]

            if r.get("flagged"):
                overrides["priority"] = "P1"

            ext_id = _stable_ext_id(name, list_name, due_raw)
            yield RawTask(
                text=name,
                source=f"reminders:list:{list_name or 'unknown'}",
                ext_id=ext_id,
                project=list_name or None,
                raw={
                    "list": list_name,
                    "due": due_raw,
                    "body": r.get("body"),
                    "priority": r.get("priority"),
                    "flagged": r.get("flagged"),
                },
                overrides=overrides,
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import os
    p = Path(os.environ.get(
        "REMINDERS_FILE",
        "/Users/dmitry/Documents/GitHub/oru/state/reminders.json",
    ))
    adapter = RemindersAdapter(file_path=p)
    n = 0
    for rt in adapter.read():
        n += 1
        print(rt.source, "::", rt.text, "::", rt.overrides)
    print(f"total: {n}")
