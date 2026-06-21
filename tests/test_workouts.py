"""Unit tests for the health agent's resistance-band workout module.

`workouts.py` lives inside the health agent package but has no package-relative
imports, so we load it standalone by path (no container / network needed).
"""
import importlib.util
from datetime import date
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents" / "health" / "src" / "workouts.py"
)
_spec = importlib.util.spec_from_file_location("health_workouts", _MOD_PATH)
workouts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workouts)


def test_load_program_cyclist():
    prog = workouts.load_program("program_1_cyclist")
    assert prog["id"] == "program_1_cyclist"
    assert set(prog["days"]) == {"A", "B", "C"}
    # Day titles come from the "## День X — <title>" headings.
    assert prog["days"]["A"]["title"]
    assert "Болгарский сплит-присед" in prog["days"]["A"]["body"]
    # 6-week progression table -> {1, 3, 5}
    assert set(prog["progression"]) == {1, 3, 5}
    assert all(v for v in prog["progression"].values())


def test_load_program_general():
    prog = workouts.load_program("program_2_general")
    assert set(prog["days"]) == {"A", "B", "C"}
    assert set(prog["progression"]) == {1, 3, 5}


def test_unknown_program_raises():
    try:
        workouts.load_program("nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown program")


def test_advance_rotation():
    assert workouts.advance("A") == "B"
    assert workouts.advance("B") == "C"
    assert workouts.advance("C") == "A"


def test_cycle_week():
    start = date(2026, 6, 1)
    assert workouts.cycle_week(start, date(2026, 6, 1)) == 1
    assert workouts.cycle_week(start, date(2026, 6, 7)) == 1   # day 6 -> week 1
    assert workouts.cycle_week(start, date(2026, 6, 8)) == 2   # day 7 -> week 2
    assert workouts.cycle_week(start, date(2026, 6, 15)) == 3  # day 14 -> week 3


def test_week_progression_buckets():
    prog = workouts.load_program("program_1_cyclist")
    p = prog["progression"]
    assert workouts.week_progression(prog, 1) == p[1]
    assert workouts.week_progression(prog, 2) == p[1]
    assert workouts.week_progression(prog, 3) == p[3]
    assert workouts.week_progression(prog, 4) == p[3]
    assert workouts.week_progression(prog, 5) == p[5]
    assert workouts.week_progression(prog, 9) == p[5]  # beyond cycle -> last bucket


def test_format_session_is_telegram_safe():
    prog = workouts.load_program("program_1_cyclist")
    text = workouts.format_session(prog, "A", 1)
    # Content of Day A is present...
    assert "Болгарский сплит-присед" in text
    assert "Велосипедист · День A" in text
    # ...and no raw markdown that breaks Telegram legacy Markdown.
    assert "###" not in text
    assert "**" not in text
    assert "---" not in text
    # Single-star bold stays balanced (even count of '*').
    assert text.count("*") % 2 == 0
    # Progression hint for week 1 is appended.
    assert "Прогрессия (неделя 1)" in text


def test_band_colours_applied():
    prog = workouts.load_program("program_1_cyclist")
    text = workouts.format_session(prog, "A", 1)
    # Levels are annotated with the owner's actual band colours...
    assert "чёрная (средняя)" in text          # Болгарский присед — средняя
    assert "красная (лёгкая, над коленями)" in text  # Monster Walk — лёгкая (над коленями)
    # ...no bare level word survives right after the label...
    assert "*Лента:* средняя" not in text
    assert "*Лента:* лёгкая" not in text
    # ...muscle name "среднюю ягодичную" must NOT be recoloured...
    assert "среднюю ягодичные" in text or "среднюю" in text
    # ...and the legend (incl. purple progression band) is present.
    assert "🟣 фиолетовая" in text


def test_heavy_band_is_green():
    prog = workouts.load_program("program_2_general")
    text = workouts.format_session(prog, "C", 1)
    assert "зелёная (тяжёлая)" in text


def test_all_sessions_have_balanced_markdown():
    """Every day of both programs must render Telegram-safe (balanced ``*``).

    program_2 has headings with inline italics like ``*(суперсет)*`` that previously
    produced an odd star count and would 400 on send.
    """
    for pid in ("program_1_cyclist", "program_2_general"):
        prog = workouts.load_program(pid)
        for day in ("A", "B", "C"):
            text = workouts.format_session(prog, day, 1)
            assert text.count("*") % 2 == 0, f"{pid} day {day}: unbalanced *"
            assert "###" not in text and "**" not in text
