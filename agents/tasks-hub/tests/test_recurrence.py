from __future__ import annotations
from datetime import date

import pytest

from src import recurrence


def test_every_days():
    assert recurrence.next_due_from("every:7d", anchor=date(2026, 6, 21)) == date(2026, 6, 28)
    assert recurrence.next_due_from("every:1d", anchor=date(2026, 12, 31)) == date(2027, 1, 1)


def test_every_weeks_aliases():
    # 1w is just 7d; we accept the alias for ergonomics.
    assert recurrence.next_due_from("every:1w", anchor=date(2026, 6, 21)) == date(2026, 6, 28)
    assert recurrence.next_due_from("every:2w", anchor=date(2026, 6, 21)) == date(2026, 7, 5)


def test_every_months_basic():
    assert recurrence.next_due_from("every:3m", anchor=date(2026, 6, 21)) == date(2026, 9, 21)


def test_every_months_clamp_to_month_end():
    # Jan 31 + 1m -> Feb 28 (non-leap) or Feb 29 (leap)
    assert recurrence.next_due_from("every:1m", anchor=date(2026, 1, 31)) == date(2026, 2, 28)
    assert recurrence.next_due_from("every:1m", anchor=date(2024, 1, 31)) == date(2024, 2, 29)


def test_every_year_leap_clamp():
    # Feb 29 (leap) + 1y -> Feb 28 (non-leap)
    assert recurrence.next_due_from("every:1y", anchor=date(2024, 2, 29)) == date(2025, 2, 28)


def test_every_weekday_advances_to_next():
    # 2026-06-21 is a Sunday (weekday=6). Next Monday is 2026-06-22.
    assert recurrence.next_due_from("every:mon", anchor=date(2026, 6, 21)) == date(2026, 6, 22)
    # Same anchor, next Friday is 2026-06-26.
    assert recurrence.next_due_from("every:fri", anchor=date(2026, 6, 21)) == date(2026, 6, 26)


def test_every_weekday_skips_today():
    # If anchor IS the target weekday, we advance to NEXT week's instance,
    # not return today. Otherwise repeat task spawns on its own close-date.
    sunday = date(2026, 6, 21)  # weekday=6
    assert recurrence.next_due_from("every:sun", anchor=sunday) == date(2026, 6, 28)


def test_is_valid():
    assert recurrence.is_valid("every:7d")
    assert recurrence.is_valid("every:mon")
    assert recurrence.is_valid("every:1y")
    assert not recurrence.is_valid("daily")
    assert not recurrence.is_valid("")
    assert not recurrence.is_valid(None)


def test_invalid_spec_raises():
    with pytest.raises(ValueError):
        recurrence.next_due_from("monthly", anchor=date(2026, 6, 21))
