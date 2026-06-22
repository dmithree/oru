"""Inline-token parser coverage. Pure regex, no fixtures needed."""
from __future__ import annotations

from src import parsers


def test_extract_deadline_day():
    assert parsers.extract_deadline("Сдать налоги — due 2026-07-15") == ("2026-07-15", "day")
    assert parsers.extract_deadline("Foo (due 2026-01-02) bar") == ("2026-01-02", "day")
    assert parsers.extract_deadline("Foo due:2026-12-31") == ("2026-12-31", "day")


def test_extract_deadline_month():
    assert parsers.extract_deadline("Анализы — due 2026-09") == ("2026-09-01", "month")


def test_extract_deadline_none():
    assert parsers.extract_deadline("Просто задача без даты") == (None, None)
    assert parsers.extract_deadline("") == (None, None)
    # Year without month must NOT match
    assert parsers.extract_deadline("В 2026 году") == (None, None)


def test_extract_context_tags_basic():
    assert parsers.extract_context_tags("Купить молоко @phone @home") == ["@phone", "@home"]


def test_extract_context_tags_value():
    assert parsers.extract_context_tags("Жду ответ @waiting:Lyosha") == ["@waiting:Lyosha"]


def test_extract_context_tags_dedup():
    assert parsers.extract_context_tags("@phone @home @phone") == ["@phone", "@home"]


def test_extract_effort_minutes():
    assert parsers.extract_effort_min("Reply ~15m") == 15
    assert parsers.extract_effort_min("Deep work ~2h") == 120
    assert parsers.extract_effort_min("Hard task ~deep") == 90


def test_extract_effort_none():
    assert parsers.extract_effort_min("nothing here") is None


def test_extract_cog_type():
    assert parsers.extract_cog_type("foo cog:deep bar") == "deep"
    assert parsers.extract_cog_type("foo cog:Admin bar") == "admin"
    assert parsers.extract_cog_type("no marker") is None


def test_extract_priority():
    assert parsers.extract_priority("!P0 urgent") == "P0"
    assert parsers.extract_priority("foo !p2 bar") == "P2"
    assert parsers.extract_priority("plain") is None


def test_extract_recurrence():
    assert parsers.extract_recurrence("daily review every:1d") == "every:1d"
    assert parsers.extract_recurrence("standup every:mon") == "every:mon"
    assert parsers.extract_recurrence("quarterly every:3m") == "every:3m"
    assert parsers.extract_recurrence("one-off") is None


def test_parse_metadata_combined():
    meta = parsers.parse_metadata(
        "Доделать onboarding @laptop ~2h !P1 cog:deep every:1w — due 2026-07-15"
    )
    assert meta == {
        "due_at": "2026-07-15",
        "due_precision": "day",
        "context_tags": ["@laptop"],
        "effort_min": 120,
        "cog_type": "deep",
        "priority": "P1",
        "recurrence": "every:1w",
    }


def test_clean_text_strips_all_tokens():
    text = "Доделать onboarding @laptop ~2h !P1 cog:deep every:1w — due 2026-07-15"
    assert parsers.clean_text(text) == "Доделать onboarding"


def test_clean_text_preserves_text_without_tokens():
    assert parsers.clean_text("Обычная задача без токенов") == "Обычная задача без токенов"


def test_clean_text_handles_empty():
    assert parsers.clean_text("") == ""
    assert parsers.clean_text("@phone ~15m") == ""
