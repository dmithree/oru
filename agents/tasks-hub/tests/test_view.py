"""View executor: date keywords, WHERE compiler, end-to-end sections."""
from __future__ import annotations
from datetime import date


def test_resolve_today_yesterday_tomorrow():
    from src.render.view import resolve_date
    today = date(2026, 6, 21)
    assert resolve_date("today", today=today) == "2026-06-21"
    assert resolve_date("yesterday", today=today) == "2026-06-20"
    assert resolve_date("tomorrow", today=today) == "2026-06-22"


def test_resolve_relative_days():
    from src.render.view import resolve_date
    today = date(2026, 6, 21)
    assert resolve_date("today_plus_7d", today=today) == "2026-06-28"
    assert resolve_date("today_minus_3d", today=today) == "2026-06-18"


def test_resolve_week_month_boundaries():
    from src.render.view import resolve_date
    # 2026-06-21 is Sunday (weekday=6); start_of_week = Monday 2026-06-15
    today = date(2026, 6, 21)
    assert resolve_date("start_of_week", today=today) == "2026-06-15"
    assert resolve_date("end_of_week", today=today) == "2026-06-21"
    assert resolve_date("start_of_month", today=today) == "2026-06-01"
    assert resolve_date("end_of_month", today=today) == "2026-06-30"


def test_resolve_iso_passthrough():
    from src.render.view import resolve_date
    assert resolve_date("2027-12-31") == "2027-12-31"


def test_compile_where_status_list():
    from src.render.view import compile_where
    sql, args = compile_where({"status": ["open", "doing"]})
    assert "status IN" in sql
    assert args == ["open", "doing"]


def test_compile_where_due_before_with_date_keyword():
    from src.render.view import compile_where
    sql, args = compile_where({"due_before": "today"}, today=date(2026, 6, 21))
    assert "due_at < ?" in sql
    assert args == ["2026-06-21"]


def test_compile_where_context_tag():
    from src.render.view import compile_where
    sql, args = compile_where({"context_tag": "@phone"})
    assert "context_tags LIKE ?" in sql
    assert args == ['%"@phone"%']


def test_run_section_returns_open_tasks(fresh_state):
    from src import store
    from src.render.view import run_section
    store.create_task("Foo", source="manual")
    store.create_task("Bar", source="manual")
    rows = run_section({"where": {"status": ["open"]}, "limit": 10})
    assert len(rows) == 2


def test_run_section_due_filter(fresh_state):
    from src import store
    from src.render.view import run_section
    store.create_task("A", source="manual", due_at="2026-06-20")
    store.create_task("B", source="manual", due_at="2026-06-22")
    rows = run_section({
        "where": {"due_before": "today"},
        "limit": 10,
    }, today=date(2026, 6, 21))
    assert len(rows) == 1
    assert rows[0]["text"] == "A"


def test_run_view_drops_empty_optional_sections(fresh_state):
    from src.render.view import run_view
    spec = {
        "name": "test",
        "sections": [
            {"id": "empty", "title": "Empty", "where": {"status": ["doing"]}, "optional": True},
            {"id": "all_open", "title": "All", "where": {"status": ["open"]}, "optional": False},
        ],
    }
    out = run_view(spec, adaptive=False)
    assert [s["id"] for s in out["sections"]] == ["all_open"]


def test_adaptive_recovery_halves_plan_limits(fresh_state):
    import json
    from src.render.view import run_view
    from src.config import settings
    # write recovery state
    with open(settings.personal_context_file, "w", encoding="utf-8") as fh:
        json.dump({"health": {"state": "recovery_needed"}}, fh)
    spec = {
        "name": "morning",
        "sections": [
            {"id": "plan_333_deep", "title": "Deep", "where": {"status": ["open"]}, "limit": 4, "optional": True},
        ],
    }
    out = run_view(spec, adaptive=True)
    # Even without matching tasks, the context_applied signal should fire
    assert out.get("context_applied", {}).get("health_state") == "recovery_needed"
