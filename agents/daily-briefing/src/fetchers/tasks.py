"""Task fetcher — Phase 2.5: call tasks-hub /render instead of reading
all-tasks.md directly.

tasks-hub owns the store; daily-briefing now just asks it for a rendered
view. Falls back to the legacy markdown read if tasks-hub is unreachable
(e.g., container not up yet) so the brief never goes blank.

Returned shape matches what analyzer.build_morning_brief expects:
  - read_open_tasks(limit=N)   -> list[str] of cleaned task texts
  - fetch_brief_sections()     -> tasks-hub /render/morning view dict
"""
from __future__ import annotations
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config import settings

logger = logging.getLogger(__name__)

TASKS_HUB_URL = os.environ.get("TASKS_HUB_URL", "http://oru-tasks-hub:8004")
_TASKS_HUB_TIMEOUT = float(os.environ.get("TASKS_HUB_TIMEOUT", "5"))


def _get_json(path: str) -> Optional[dict[str, Any]]:
    url = TASKS_HUB_URL.rstrip("/") + path
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=_TASKS_HUB_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as e:
        logger.info("tasks-hub %s unreachable (%s); falling back", url, e)
        return None
    except Exception:
        logger.exception("tasks-hub %s call failed", url)
        return None


def fetch_brief_sections() -> Optional[dict[str, Any]]:
    """Hit tasks-hub for the morning view. Returns the full view dict
    (sections, context_applied, totals) or None on failure — caller
    chooses to fall back.
    """
    resp = _get_json("/render/morning?format=json")
    if not resp or not resp.get("ok"):
        return None
    return resp.get("view")


def fetch_evening_sections() -> Optional[dict[str, Any]]:
    resp = _get_json("/render/evening?format=json")
    if not resp or not resp.get("ok"):
        return None
    return resp.get("view")


def read_open_tasks(limit: int = 30) -> list[str]:
    """Backwards-compatible flat list of open task texts.

    Tries tasks-hub first (preferred — clean text, deduped, structured).
    Falls back to scanning the mounted all-tasks.md so we never block
    the brief on tasks-hub being down."""
    view = fetch_brief_sections()
    if view:
        out: list[str] = []
        for section in view.get("sections", []):
            for t in section.get("tasks", []):
                text = t.get("text", "").strip()
                if text and text not in out:
                    out.append(text)
                    if len(out) >= limit:
                        return out
        return out

    # Legacy fallback: parse the static markdown file
    path = Path(settings.tasks_file)
    if not path.exists():
        logger.warning("Tasks file not found and tasks-hub unreachable: %s", path)
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Tasks file read failed")
        return []

    tasks: list[str] = []
    for line in content.splitlines():
        if re.match(r"^\s*-\s*\[ \]\s+", line):
            clean = re.sub(r"^\s*-\s*\[ \]\s+", "", line).strip()
            if clean:
                tasks.append(clean)
                if len(tasks) >= limit:
                    break
    return tasks
