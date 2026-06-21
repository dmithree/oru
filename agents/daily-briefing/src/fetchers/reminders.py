"""Apple Reminders — reads from /opt/state/reminders.json populated by host bridge.

The host runs ~/Documents/GitHub/oru/host-daemons/reminders-bridge.sh under launchd
every 15 minutes. Container has no access to AppleScript directly — only via this
shared file mount.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def fetch_today_reminders(file_path: str = "/opt/state/reminders.json") -> dict[str, Any]:
    p = Path(file_path)
    if not p.exists():
        return {"available": False, "reason": f"file not found: {file_path}"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Reminders JSON parse failed")
        return {"available": False, "reason": "parse error"}

    if "error" in data:
        return {"available": False, "reason": data["error"]}

    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()
    today: list[dict] = []
    overdue: list[dict] = []
    flagged: list[dict] = []

    for r in data.get("reminders", []):
        due = r.get("due")
        if due:
            try:
                due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                if due_dt.date().isoformat() == today_str:
                    today.append(r)
                elif due_dt < now:
                    overdue.append(r)
            except Exception:
                pass
        if r.get("flagged"):
            flagged.append(r)

    return {
        "available": True,
        "generated_at": data.get("generated_at"),
        "today": today[:10],
        "overdue": overdue[:5],
        "flagged": flagged[:5],
        "total_open": len(data.get("reminders", [])),
    }
