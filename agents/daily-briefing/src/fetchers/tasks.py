"""Read open tasks from the mounted personal-agent file."""
from __future__ import annotations
import logging
import re
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)


def read_open_tasks(limit: int = 30) -> list[str]:
    """Return open `- [ ]` task lines from settings.tasks_file."""
    path = Path(settings.tasks_file)
    if not path.exists():
        logger.warning("Tasks file not found: %s", path)
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
