"""TaskStore — SQLite-backed single source of truth for all tasks.

Implements ideas 1 (rich-schema TaskStore) and 3 (state machine).
Sources (markdown, Reminders, Linear, agent emitters) are projections
written through this store; the store is authoritative.

Schema migrations are tracked via PRAGMA user_version. Add a new
migration to MIGRATIONS, bump SCHEMA_VERSION, do NOT mutate existing
migration strings.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .config import settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# === State machine (idea 3) ===========================================

STATUSES = (
    "inbox",       # captured, not triaged
    "open",        # triaged, available, not selected for today
    "next",        # selected for today
    "doing",       # in progress
    "waiting",     # waiting on external (waiting_on populated)
    "blocked",     # blocked by another task or known blocker (blocked_by populated)
    "deferred",    # snoozed until defer_until
    "done",        # completed
    "dropped",     # cancelled, won't do
)

# allowed_transitions[from] -> set(to). Any transition not listed raises.
TRANSITIONS: dict[str, set[str]] = {
    "inbox":    {"open", "dropped", "deferred", "done"},
    "open":     {"next", "doing", "waiting", "blocked", "deferred", "done", "dropped"},
    "next":     {"doing", "open", "waiting", "blocked", "deferred", "done", "dropped"},
    "doing":    {"done", "open", "waiting", "blocked", "deferred", "dropped"},
    "waiting":  {"open", "doing", "done", "dropped"},
    "blocked":  {"open", "doing", "done", "dropped"},
    "deferred": {"open", "dropped", "done"},
    "done":     {"open"},       # reopen
    "dropped":  {"open"},       # restore
}


class InvalidTransition(ValueError):
    pass


def validate_transition(old: str, new: str) -> None:
    if old == new:
        return
    if new not in TRANSITIONS.get(old, set()):
        raise InvalidTransition(f"cannot transition {old!r} -> {new!r}")


# === Canonical text hash for dedup ===================================

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s@~]", flags=re.UNICODE)


def canonical_text_hash(text: str) -> str:
    """Stable hash for dedup across sources.

    Lowercase, strip diacritics, collapse whitespace, drop most
    punctuation but keep @ (context tags) and ~ (effort marker) so two
    tasks with different tags are NOT collapsed.
    """
    s = unicodedata.normalize("NFKD", text or "").lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT.sub("", s)
    s = _WS.sub(" ", s).strip()
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16] if s else ""


# === Schema ===========================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    text_hash       TEXT NOT NULL,
    status          TEXT NOT NULL,
    source          TEXT NOT NULL,
    ext_id          TEXT,
    priority        TEXT,
    due_at          TEXT,
    due_precision   TEXT,
    context_tags    TEXT NOT NULL DEFAULT '[]',
    cog_type        TEXT,
    effort_min      INTEGER,
    energy          TEXT,
    recurrence      TEXT,
    project         TEXT,
    owner_agent     TEXT,
    blocked_by      TEXT,
    waiting_on      TEXT,
    defer_until     TEXT,
    parent_id       TEXT,
    raw             TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    closed_at       TEXT,
    completed_via   TEXT
);

CREATE INDEX IF NOT EXISTS ix_tasks_text_hash    ON tasks(text_hash);
CREATE INDEX IF NOT EXISTS ix_tasks_status       ON tasks(status);
CREATE INDEX IF NOT EXISTS ix_tasks_source       ON tasks(source);
CREATE INDEX IF NOT EXISTS ix_tasks_ext_id       ON tasks(source, ext_id);
CREATE INDEX IF NOT EXISTS ix_tasks_due_at       ON tasks(due_at);
CREATE INDEX IF NOT EXISTS ix_tasks_owner_agent  ON tasks(owner_agent);
CREATE INDEX IF NOT EXISTS ix_tasks_defer_until  ON tasks(defer_until);
"""


# Ordered migrations. NEVER mutate a past migration string.
MIGRATIONS: list[str] = [
    SCHEMA_SQL,
]


# === Connection management ===========================================

_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    p = Path(settings.db_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Run pending migrations. Idempotent."""
    with _lock, _connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS, start=1):
            if i <= version:
                continue
            logger.info("Applying migration %d", i)
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {i}")
        applied = conn.execute("PRAGMA user_version").fetchone()[0]
        logger.info("Schema version: %d", applied)


# === Row <-> dict ====================================================

_JSON_FIELDS = {"context_tags", "raw"}


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in _JSON_FIELDS:
        if d.get(k) is not None:
            try:
                d[k] = json.loads(d[k])
            except (TypeError, json.JSONDecodeError):
                pass
    return d


def _serialize(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


# === CRUD =============================================================


def create_task(
    text: str,
    *,
    source: str,
    status: str = "open",
    ext_id: Optional[str] = None,
    priority: Optional[str] = None,
    due_at: Optional[str] = None,
    due_precision: Optional[str] = None,
    context_tags: Optional[list[str]] = None,
    cog_type: Optional[str] = None,
    effort_min: Optional[int] = None,
    energy: Optional[str] = None,
    recurrence: Optional[str] = None,
    project: Optional[str] = None,
    owner_agent: Optional[str] = None,
    blocked_by: Optional[str] = None,
    waiting_on: Optional[str] = None,
    defer_until: Optional[str] = None,
    parent_id: Optional[str] = None,
    raw: Optional[dict[str, Any]] = None,
    task_id: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a new task. Returns the stored row as dict.

    Does NOT dedup — caller (ingestor) decides via `find_by_text_hash`
    or `find_by_ext_id`. Validates status is known.
    """
    if not text or not text.strip():
        raise ValueError("text required")
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status!r}")

    tid = task_id or uuid.uuid4().hex
    now = _now_iso()
    th = canonical_text_hash(text)
    tags_json = json.dumps(list(context_tags or []), ensure_ascii=False)
    raw_json = json.dumps(raw, ensure_ascii=False) if raw is not None else None

    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, text, text_hash, status, source, ext_id, priority,
                due_at, due_precision, context_tags, cog_type, effort_min,
                energy, recurrence, project, owner_agent, blocked_by,
                waiting_on, defer_until, parent_id, raw,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tid, text, th, status, source, ext_id, priority,
                due_at, due_precision, tags_json, cog_type, effort_min,
                energy, recurrence, project, owner_agent, blocked_by,
                waiting_on, defer_until, parent_id, raw_json,
                now, now,
            ),
        )
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    return _row_to_task(row)


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def find_by_text_hash(text_hash: str) -> list[dict[str, Any]]:
    if not text_hash:
        return []
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE text_hash = ? ORDER BY created_at",
            (text_hash,),
        ).fetchall()
    return [_row_to_task(r) for r in rows]


def find_by_ext_id(source: str, ext_id: str) -> Optional[dict[str, Any]]:
    if not ext_id:
        return None
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE source = ? AND ext_id = ?",
            (source, ext_id),
        ).fetchone()
    result = _row_to_task(row) if row else None
    return result


# Fields the caller is allowed to update via update_task(). Status
# changes go through set_status() so transitions are validated.
_UPDATABLE = {
    "text", "source", "ext_id", "priority", "due_at", "due_precision",
    "context_tags", "cog_type", "effort_min", "energy", "recurrence",
    "project", "owner_agent", "blocked_by", "waiting_on", "defer_until",
    "parent_id", "raw",
}


def update_task(task_id: str, **fields: Any) -> dict[str, Any]:
    """Update arbitrary fields. Re-hashes text if text changed. Rejects
    unknown fields and status (use set_status())."""
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")
    if not fields:
        existing = get_task(task_id)
        if not existing:
            raise KeyError(task_id)
        return existing

    sets: list[str] = []
    args: list[Any] = []
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        args.append(_serialize(v))
    if "text" in fields:
        sets.append("text_hash = ?")
        args.append(canonical_text_hash(fields["text"]))
    sets.append("updated_at = ?")
    args.append(_now_iso())
    args.append(task_id)

    with _lock, _connect() as conn:
        cur = conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", args)
        if cur.rowcount == 0:
            raise KeyError(task_id)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row)


def set_status(
    task_id: str,
    new_status: str,
    *,
    completed_via: Optional[str] = None,
    defer_until: Optional[str] = None,
    blocked_by: Optional[str] = None,
    waiting_on: Optional[str] = None,
) -> tuple[dict[str, Any], str]:
    """Validated state transition. Returns (new_row, previous_status)."""
    if new_status not in STATUSES:
        raise ValueError(f"invalid status: {new_status!r}")

    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise KeyError(task_id)
        old_status = row["status"]
        validate_transition(old_status, new_status)

        now = _now_iso()
        sets = ["status = ?", "updated_at = ?"]
        args: list[Any] = [new_status, now]

        if new_status == "done":
            sets.append("closed_at = ?")
            args.append(now)
            if completed_via:
                sets.append("completed_via = ?")
                args.append(completed_via)
        elif old_status == "done":
            sets.append("closed_at = NULL")
            sets.append("completed_via = NULL")

        if new_status == "deferred" and defer_until is not None:
            sets.append("defer_until = ?")
            args.append(defer_until)
        if new_status == "blocked" and blocked_by is not None:
            sets.append("blocked_by = ?")
            args.append(blocked_by)
        if new_status == "waiting" and waiting_on is not None:
            sets.append("waiting_on = ?")
            args.append(waiting_on)

        args.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", args)
        new_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    return _row_to_task(new_row), old_status


def list_tasks(
    *,
    status: Optional[Iterable[str]] = None,
    source_prefix: Optional[str] = None,
    owner_agent: Optional[str] = None,
    due_before: Optional[str] = None,
    context_tag: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    order: str = "due_at_then_created",
) -> list[dict[str, Any]]:
    """List with simple filters. For richer queries use raw SQL via
    `with_connection()` in render-layer."""
    where: list[str] = []
    args: list[Any] = []

    if status is not None:
        statuses = list(status)
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        where.append(f"status IN ({placeholders})")
        args.extend(statuses)
    if source_prefix:
        where.append("source LIKE ?")
        args.append(source_prefix + "%")
    if owner_agent:
        where.append("owner_agent = ?")
        args.append(owner_agent)
    if due_before:
        where.append("(due_at IS NOT NULL AND due_at <= ?)")
        args.append(due_before)
    if context_tag:
        # naive substring search inside the JSON array; good enough at
        # the volumes expected (low thousands). Replace with json_each
        # if perf matters later.
        where.append("context_tags LIKE ?")
        args.append(f'%"{context_tag}"%')

    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if order == "due_at_then_created":
        sql += " ORDER BY (due_at IS NULL), due_at, created_at"
    elif order == "created":
        sql += " ORDER BY created_at"
    elif order == "updated":
        sql += " ORDER BY updated_at DESC"
    sql += " LIMIT ? OFFSET ?"
    args.append(int(limit))
    args.append(int(offset))

    with _lock, _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_task(r) for r in rows]


def stats() -> dict[str, Any]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        sources = conn.execute(
            """
            SELECT
                CASE
                    WHEN INSTR(source, ':') > 0 THEN SUBSTR(source, 1, INSTR(source, ':') - 1)
                    ELSE source
                END AS src,
                COUNT(*) AS n
            FROM tasks
            GROUP BY src
            """
        ).fetchall()
        by_source = {r["src"]: r["n"] for r in sources}
    return {
        "total": total,
        "by_status": by_status,
        "by_source": by_source,
        "schema_version": SCHEMA_VERSION,
    }


def delete_task(task_id: str) -> bool:
    """Hard-delete. Use sparingly — prefer set_status('dropped') so the
    event log retains history."""
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return cur.rowcount > 0
