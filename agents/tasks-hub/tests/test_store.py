"""State machine + dedup coverage. Uses the fresh_state fixture so each
test gets its own tmp_path-backed sqlite."""
from __future__ import annotations

import pytest


def test_create_and_get(fresh_state):
    from src import store
    t = store.create_task("Test task", source="manual", context_tags=["@phone"])
    assert t["status"] == "open"
    assert t["context_tags"] == ["@phone"]
    fetched = store.get_task(t["id"])
    assert fetched["text"] == "Test task"


def test_state_machine_open_to_done(fresh_state):
    from src import store
    t = store.create_task("Foo", source="manual")
    moved, old = store.set_status(t["id"], "doing")
    assert moved["status"] == "doing"
    assert old == "open"
    done, _ = store.set_status(t["id"], "done", completed_via="test")
    assert done["status"] == "done"
    assert done["closed_at"] is not None
    assert done["completed_via"] == "test"


def test_invalid_transition_raises(fresh_state):
    from src import store
    t = store.create_task("Foo", source="manual")
    # open -> inbox not allowed (inbox is the entry state, not regression)
    with pytest.raises(store.InvalidTransition):
        store.set_status(t["id"], "inbox")


def test_reopen_clears_closed_fields(fresh_state):
    from src import store
    t = store.create_task("Foo", source="manual")
    store.set_status(t["id"], "done", completed_via="x")
    reopened, _ = store.set_status(t["id"], "open")
    assert reopened["status"] == "open"
    assert reopened["closed_at"] is None
    assert reopened["completed_via"] is None


def test_dedup_by_text_hash(fresh_state):
    from src import store
    a = store.create_task("Доделать onboarding", source="manual")
    matches = store.find_by_text_hash(a["text_hash"])
    assert len(matches) == 1 and matches[0]["id"] == a["id"]


def test_dedup_preserves_at_tags(fresh_state):
    """Two tasks identical except for @-tag should NOT collapse."""
    from src import store
    a = store.create_task("Купить молоко", source="manual")
    b = store.create_task("Купить молоко @phone", source="manual")
    assert a["text_hash"] != b["text_hash"]


def test_find_by_ext_id(fresh_state):
    from src import store
    t = store.create_task("Linear thing", source="linear:ENG", ext_id="ENG-42")
    found = store.find_by_ext_id("linear:ENG", "ENG-42")
    assert found and found["id"] == t["id"]
    assert store.find_by_ext_id("linear:ENG", "ENG-99") is None


def test_list_filters_status_and_source(fresh_state):
    from src import store
    store.create_task("A", source="manual")
    store.create_task("B", source="reminders:list:X")
    store.create_task("C", source="reminders:list:X")
    rem = store.list_tasks(source_prefix="reminders:")
    assert len(rem) == 2
    open_only = store.list_tasks(status=["open"])
    assert len(open_only) == 3


def test_stats_aggregates(fresh_state):
    from src import store
    store.create_task("A", source="manual")
    b = store.create_task("B", source="manual")
    store.set_status(b["id"], "done")
    s = store.stats()
    assert s["total"] == 2
    assert s["by_status"]["open"] == 1
    assert s["by_status"]["done"] == 1
