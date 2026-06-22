"""Shared fixtures.

Each test gets a fresh SQLite + events JSONL in tmp_path. We re-import
config so the rest of the modules (store, events) pick up the new
paths — settings are read at call time, not at import time.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    """Ephemeral tasks.db + events.jsonl + personal-context.json in tmp_path.

    pydantic Settings is imported once; mutating env after import won't
    propagate. We mutate the live settings instance's attributes via
    monkeypatch so it auto-restores after the test.
    """
    db = tmp_path / "tasks.db"
    events = tmp_path / "events.jsonl"
    ctx = tmp_path / "personal-context.json"

    from src import config as _c
    monkeypatch.setattr(_c.settings, "db_file", str(db))
    monkeypatch.setattr(_c.settings, "events_file", str(events))
    monkeypatch.setattr(_c.settings, "personal_context_file", str(ctx))

    from src import store as _s
    _s.init_db()

    yield {"db": db, "events": events, "context": ctx, "tmp": tmp_path}
