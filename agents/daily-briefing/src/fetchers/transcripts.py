"""Therapy / coaching transcript carry-over.

Scans mounted /opt/data/transcripts/ for files modified in last 24h, returns
their content for inclusion in morning brief.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def fetch_recent_transcripts(directories: list[str], hours: int = 24, max_files: int = 3) -> list[dict]:
    cutoff = datetime.now().timestamp() - hours * 3600
    found: list[tuple[float, Path]] = []

    for d in directories:
        root = Path(d)
        if not root.exists():
            continue
        for f in root.rglob("*.md"):
            try:
                mtime = f.stat().st_mtime
                if mtime >= cutoff:
                    found.append((mtime, f))
            except OSError:
                continue

    found.sort(reverse=True)
    out: list[dict] = []
    for mtime, path in found[:max_files]:
        try:
            content = path.read_text(encoding="utf-8")[:4000]
            out.append({
                "path": str(path),
                "modified": datetime.fromtimestamp(mtime).isoformat(),
                "content": content,
            })
        except Exception:
            continue
    return out
