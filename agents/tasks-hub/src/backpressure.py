"""Stale-task detection (idea 18 — backpressure / weekly cleanup).

Surfaces open tasks that have been sitting without movement for N days
and have no deadline. The intended weekly Sunday flow:

    1. Cron triggers GET /stale (this module)
    2. The bot DMs Дима a small batch with one-tap decisions
    3. Decisions POST to /stale/triage which calls coordinator.change_status

Today only the detection + triage endpoints are implemented; the
Telegram bot wiring is Phase 4.5 (separate worker on TELEGRAM_BOT_TOKEN).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .store import _row_to_task


@contextmanager
def _ro() -> Iterator[sqlite3.Connection]:
    p = Path(settings.db_file)
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def find_stale(
    *,
    older_than_days: int = 30,
    limit: int = 20,
    include_statuses: tuple[str, ...] = ("open", "waiting", "blocked"),
) -> list[dict[str, Any]]:
    """Open tasks not updated in `older_than_days` AND with no deadline.

    Sorted oldest-first so the user sees the most rotted items first.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    placeholders = ",".join("?" * len(include_statuses))
    sql = (
        f"SELECT * FROM tasks "
        f"WHERE status IN ({placeholders}) "
        f"AND due_at IS NULL "
        f"AND updated_at < ? "
        f"ORDER BY updated_at "
        f"LIMIT ?"
    )
    args = [*include_statuses, cutoff, int(limit)]
    with _ro() as conn:
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
    return [_row_to_task(r) for r in rows]


def summary(*, older_than_days: int = 30) -> dict[str, Any]:
    """Counts of stale tasks bucketed by age — for a Telegram digest."""
    buckets = [
        ("30-60d", 30, 60),
        ("60-90d", 60, 90),
        ("90d+",   90, 36500),
    ]
    out = {}
    now = datetime.now(timezone.utc)
    for label, lo, hi in buckets:
        lo_iso = (now - timedelta(days=hi)).isoformat(timespec="seconds")
        hi_iso = (now - timedelta(days=lo)).isoformat(timespec="seconds")
        with _ro() as conn:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM tasks "
                    "WHERE status IN ('open','waiting','blocked') "
                    "AND due_at IS NULL "
                    "AND updated_at >= ? AND updated_at < ?",
                    [lo_iso, hi_iso],
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
        out[label] = (row["n"] if row else 0)
    out["threshold_days"] = older_than_days
    return out
