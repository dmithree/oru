"""Recurrence spec parser + next-instance scheduling (idea 9).

A recurrence spec lives in the `recurrence` column as a string token:

    every:7d    every 7 days
    every:1w    every 1 week  (alias for every:7d)
    every:1m    every 1 month
    every:3m    every 3 months
    every:1y    every 1 year
    every:mon   every Monday  (mon|tue|wed|thu|fri|sat|sun)

When a recurring task transitions to `done`, the coordinator calls
`spawn_next()` which inserts a fresh instance with the same metadata,
status=`open`, defer_until cleared, and a recalculated `due_at`.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_PERIOD_RE = re.compile(r"^every:(\d+)(d|w|m|y)$", re.IGNORECASE)
_WEEKDAY_RE = re.compile(r"^every:(mon|tue|wed|thu|fri|sat|sun)$", re.IGNORECASE)


def next_due_from(
    recurrence: str,
    *,
    anchor: Optional[date] = None,
) -> date:
    """Return the next due-date for a recurrence spec.

    `anchor` is the date the spawn is anchored to — typically the
    closure date of the just-completed instance. Falls back to today.
    """
    base = anchor or date.today()
    spec = (recurrence or "").strip().lower()

    m = _PERIOD_RE.match(spec)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "d":
            return base + timedelta(days=n)
        if unit == "w":
            return base + timedelta(weeks=n)
        if unit == "m":
            # month arithmetic: keep day-of-month if possible, else
            # clamp to month end (e.g., Jan 31 + 1m = Feb 28/29).
            new_month = base.month - 1 + n
            new_year = base.year + new_month // 12
            new_month = new_month % 12 + 1
            day = min(base.day, _last_day(new_year, new_month))
            return date(new_year, new_month, day)
        if unit == "y":
            try:
                return base.replace(year=base.year + n)
            except ValueError:
                # Feb 29 in a non-leap year
                return base.replace(year=base.year + n, day=28)

    m = _WEEKDAY_RE.match(spec)
    if m:
        target = _WEEKDAYS[m.group(1).lower()]
        delta = (target - base.weekday()) % 7
        if delta == 0:
            delta = 7  # always advance to NEXT occurrence, not today
        return base + timedelta(days=delta)

    raise ValueError(f"unknown recurrence spec: {recurrence!r}")


def _last_day(year: int, month: int) -> int:
    if month == 12:
        first_next = date(year + 1, 1, 1)
    else:
        first_next = date(year, month + 1, 1)
    return (first_next - timedelta(days=1)).day


def is_valid(recurrence: Optional[str]) -> bool:
    if not recurrence:
        return False
    s = recurrence.strip().lower()
    return bool(_PERIOD_RE.match(s) or _WEEKDAY_RE.match(s))


if __name__ == "__main__":
    samples = [
        ("every:7d", date(2026, 6, 21)),
        ("every:1w", date(2026, 6, 21)),
        ("every:3m", date(2026, 6, 21)),
        ("every:1y", date(2024, 2, 29)),
        ("every:mon", date(2026, 6, 21)),  # Sunday -> next Monday
        ("every:fri", date(2026, 6, 21)),
    ]
    for spec, anchor in samples:
        nxt = next_due_from(spec, anchor=anchor)
        print(f"{spec:<14} anchor={anchor.isoformat()} -> {nxt.isoformat()}")
