"""apply_events end-to-end without hitting the LLM. We feed it a parsed
dict shaped like what call_llm would return."""
from __future__ import annotations


def test_apply_completed(fresh_state):
    from src import coordinator
    from src.debrief import apply_events
    t = coordinator.create("Foo", source="manual")
    parsed = {"events": [{"kind": "completed", "task_id": t["id"], "matched_text": "did foo"}]}
    results = apply_events(parsed)
    assert results[0]["ok"]
    assert results[0]["text"] == "Foo"

    from src import store
    assert store.get_task(t["id"])["status"] == "done"


def test_apply_deferred_sets_defer_until(fresh_state):
    from src import coordinator, store
    from src.debrief import apply_events
    t = coordinator.create("Tax", source="manual")
    parsed = {"events": [{
        "kind": "deferred", "task_id": t["id"],
        "defer_until": "2026-07-01", "matched_text": "перенёс налоги",
    }]}
    results = apply_events(parsed)
    assert results[0]["ok"]
    updated = store.get_task(t["id"])
    assert updated["status"] == "deferred"
    assert updated["defer_until"] == "2026-07-01"


def test_apply_blocked_records_blocker(fresh_state):
    from src import coordinator, store
    from src.debrief import apply_events
    t = coordinator.create("Release", source="manual")
    parsed = {"events": [{
        "kind": "blocked", "task_id": t["id"],
        "blocked_by": "Сбер анкета", "matched_text": "заблокирован",
    }]}
    apply_events(parsed)
    updated = store.get_task(t["id"])
    assert updated["status"] == "blocked"
    assert updated["blocked_by"] == "Сбер анкета"


def test_apply_created_with_due(fresh_state):
    from src.debrief import apply_events
    from src import store
    parsed = {"events": [{
        "kind": "created", "new_text": "Позвонить врачу",
        "defer_until": "2026-06-25", "matched_text": "надо врачу",
    }]}
    results = apply_events(parsed)
    assert results[0]["ok"]
    tid = results[0]["task_id"]
    new_task = store.get_task(tid)
    assert new_task["text"] == "Позвонить врачу"
    assert new_task["due_at"] == "2026-06-25"
    assert new_task["status"] == "open"


def test_apply_unknown_task_id_fails_cleanly(fresh_state):
    from src.debrief import apply_events
    parsed = {"events": [{"kind": "completed", "task_id": "nonexistent"}]}
    results = apply_events(parsed)
    assert not results[0]["ok"]
    assert "error" in results[0]
