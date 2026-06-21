"""View executor (idea 12 + 15).

A view is a yaml file that declares N sections, each with a `where`
filter, a sort order, and a limit. The executor turns each section
into a SQL query against tasks.db and returns a dict keyed by
section id, ready to be handed to a jinja template.

Supported `where` keys — each compiles to one SQL fragment:

  status: [open, next, ...]       -> status IN (...)
  source_prefix: str              -> source LIKE 'prefix%'
  owner_agent: str
  cog_type: str | [a, b]          -> cog_type = ? or IN (...)
  context_tag: str | [a, b]       -> LIKE '%"@tag"%' (one or many)
  has_due: bool                   -> due_at IS NOT NULL / IS NULL
  due_before: date-keyword        -> due_at < ?
  due_on_or_before: date-keyword  -> due_at <= ?
  due_after: date-keyword         -> due_at > ?
  defer_until_lte: date-keyword   -> defer_until <= ?
  priority: str | [a, b]          -> priority = ? or IN (...)
  text_contains: str              -> text LIKE '%substr%'

Date keywords (resolved at run time):
  today, yesterday, tomorrow,
  today_plus_Nd, today_minus_Nd,
  start_of_week, end_of_week,
  start_of_month, end_of_month,
  ISO date (passed through).

Sort `order` is a list of column names, optionally prefixed with `-`
for descending. `due_at` always treats NULL as last.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

from ..config import settings
from ..store import _row_to_task

logger = logging.getLogger(__name__)


# === Date keyword resolver ==========================================


_REL_PLUS = re.compile(r"^today_plus_(\d+)d$")
_REL_MINUS = re.compile(r"^today_minus_(\d+)d$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_date(keyword: str, *, today: Optional[date] = None) -> str:
    """Resolve a date keyword into an ISO YYYY-MM-DD string.

    `today` is injectable for testing; defaults to date.today() (which
    uses the container's local timezone — Europe/Belgrade per Dockerfile)."""
    base = today or date.today()
    if not isinstance(keyword, str):
        raise ValueError(f"date keyword must be a string, got {type(keyword).__name__}")

    k = keyword.strip().lower()
    if k == "today":
        return base.isoformat()
    if k == "yesterday":
        return (base - timedelta(days=1)).isoformat()
    if k == "tomorrow":
        return (base + timedelta(days=1)).isoformat()
    m = _REL_PLUS.match(k)
    if m:
        return (base + timedelta(days=int(m.group(1)))).isoformat()
    m = _REL_MINUS.match(k)
    if m:
        return (base - timedelta(days=int(m.group(1)))).isoformat()
    if k == "start_of_week":
        return (base - timedelta(days=base.weekday())).isoformat()
    if k == "end_of_week":
        return (base + timedelta(days=6 - base.weekday())).isoformat()
    if k == "start_of_month":
        return base.replace(day=1).isoformat()
    if k == "end_of_month":
        # last day of month: jump to first of next month, minus 1 day
        if base.month == 12:
            nxt = base.replace(year=base.year + 1, month=1, day=1)
        else:
            nxt = base.replace(month=base.month + 1, day=1)
        return (nxt - timedelta(days=1)).isoformat()
    if _ISO_DATE.match(k):
        return k
    raise ValueError(f"unknown date keyword: {keyword!r}")


# === Where compiler =================================================


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def compile_where(where: dict[str, Any], *, today: Optional[date] = None) -> tuple[str, list[Any]]:
    """Translate a view-spec `where` dict into (sql_fragment, args)."""
    parts: list[str] = []
    args: list[Any] = []

    for key, raw in (where or {}).items():
        if raw is None:
            continue
        k = key.strip().lower()

        if k == "status":
            vs = _as_list(raw)
            if not vs:
                continue
            parts.append(f"status IN ({','.join('?' * len(vs))})")
            args.extend(vs)
        elif k == "source_prefix":
            parts.append("source LIKE ?")
            args.append(str(raw) + "%")
        elif k == "owner_agent":
            parts.append("owner_agent = ?")
            args.append(str(raw))
        elif k == "cog_type":
            vs = _as_list(raw)
            if len(vs) == 1:
                parts.append("cog_type = ?")
                args.append(vs[0])
            else:
                parts.append(f"cog_type IN ({','.join('?' * len(vs))})")
                args.extend(vs)
        elif k == "context_tag":
            for tag in _as_list(raw):
                parts.append("context_tags LIKE ?")
                args.append(f'%"{tag}"%')
        elif k == "has_due":
            parts.append("due_at IS NOT NULL" if raw else "due_at IS NULL")
        elif k == "due_before":
            parts.append("due_at IS NOT NULL AND due_at < ?")
            args.append(resolve_date(str(raw), today=today))
        elif k == "due_on_or_before":
            parts.append("due_at IS NOT NULL AND due_at <= ?")
            args.append(resolve_date(str(raw), today=today))
        elif k == "due_after":
            parts.append("due_at IS NOT NULL AND due_at > ?")
            args.append(resolve_date(str(raw), today=today))
        elif k == "defer_until_lte":
            parts.append("defer_until IS NOT NULL AND defer_until <= ?")
            args.append(resolve_date(str(raw), today=today))
        elif k == "priority":
            vs = _as_list(raw)
            if len(vs) == 1:
                parts.append("priority = ?")
                args.append(vs[0])
            else:
                parts.append(f"priority IN ({','.join('?' * len(vs))})")
                args.extend(vs)
        elif k == "text_contains":
            parts.append("text LIKE ?")
            args.append(f"%{raw}%")
        else:
            raise ValueError(f"unknown where key: {key!r}")

    if not parts:
        return "1=1", []
    return " AND ".join(parts), args


def compile_order(order: list[str] | None) -> str:
    """`order` is a list like ['-priority', 'due_at']. NULLs last for due_at."""
    if not order:
        return "ORDER BY created_at"
    cols: list[str] = []
    for c in order:
        c = c.strip()
        desc = c.startswith("-")
        col = c[1:] if desc else c
        # whitelist columns to avoid SQL injection via yaml
        if col not in {
            "created_at", "updated_at", "closed_at", "due_at", "defer_until",
            "priority", "effort_min", "status", "cog_type", "text", "source",
        }:
            raise ValueError(f"unknown order column: {col!r}")
        if col == "due_at":
            cols.append(f"(due_at IS NULL), due_at{' DESC' if desc else ''}")
        else:
            cols.append(f"{col}{' DESC' if desc else ''}")
    return "ORDER BY " + ", ".join(cols)


# === Section executor ===============================================


@contextmanager
def _ro_connect() -> Iterator[sqlite3.Connection]:
    """Read-only connection. View execution never writes."""
    p = Path(settings.db_file)
    if not p.exists():
        # treat missing db as empty store rather than crashing render
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT)")
        try:
            yield conn
        finally:
            conn.close()
        return
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def run_section(section: dict[str, Any], *, today: Optional[date] = None) -> list[dict[str, Any]]:
    limit = int(section.get("limit", 50))
    if limit <= 0:
        return []
    where_sql, args = compile_where(section.get("where", {}), today=today)
    order_sql = compile_order(section.get("order"))
    sql = f"SELECT * FROM tasks WHERE {where_sql} {order_sql} LIMIT ?"
    args.append(limit)

    with _ro_connect() as conn:
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as e:
            # Table may not exist yet (fresh deploy, no ingestion)
            logger.info("view: query failed (%s) — treating as empty", e)
            return []
    return [_row_to_task(r) for r in rows]


# === View loader + runner ===========================================


def load_view(view_name: str, *, search_dirs: Optional[list[Path]] = None) -> dict[str, Any]:
    """Load views/<name>.yaml from search_dirs (first hit wins)."""
    fname = f"{view_name}.yaml"
    dirs = search_dirs or [Path(__file__).parent / "views"]
    for d in dirs:
        p = d / fname
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raise FileNotFoundError(f"view not found: {fname}")


def _load_personal_context() -> dict[str, Any]:
    p = Path(settings.personal_context_file)
    if not p.exists():
        return {}
    try:
        import json
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _apply_adaptive(section: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Adaptive filtering hook (idea 17). Reads personal-context.json
    and shrinks plan / drops irrelevant contexts based on health and
    travel state.

    Returns a (possibly mutated) copy of `section`. Original spec
    untouched so the same yaml stays cache-friendly.

    Conservative rules — they only TRIM the plan, never silently
    inject filters that would zero-out user-tagged tasks:

      - health.state == "recovery_needed"
          plan_333_* sections: limit = max(1, limit // 2). Priority
          filters NOT auto-injected so unscored tasks still surface.
      - travel.active_trip present
          plan_333_* sections that explicitly target @home or @office
          are dropped (limit=0 -> view drops them as optional).
          @city is NOT auto-injected — that's the user's call when
          they tag their travel tasks.
    """
    if not context:
        return section
    health = (context.get("health") or {})
    travel = (context.get("travel") or {})
    out = dict(section)
    where = dict(out.get("where") or {})
    sid = (out.get("id") or "").lower()

    if health.get("state") == "recovery_needed" and sid.startswith("plan_333_"):
        cur_limit = int(out.get("limit", 5))
        out["limit"] = max(1, cur_limit // 2)

    if travel.get("active_trip"):
        tag = where.get("context_tag")
        if tag in {"@home", "@office"}:
            out["limit"] = 0
            out["optional"] = True

    out["where"] = where
    return out


def run_view(
    view_name_or_spec: str | dict[str, Any],
    *,
    today: Optional[date] = None,
    search_dirs: Optional[list[Path]] = None,
    context: Optional[dict[str, Any]] = None,
    adaptive: bool = True,
) -> dict[str, Any]:
    """Run all sections of a view. Returns:

        {
          "view": "morning_brief",
          "generated_at": "...",
          "context_applied": {...},   # only when adaptive adjustments fired
          "sections": [
            {"id": "carry_over", "title": "...", "tasks": [...]},
            ...
          ],
          "totals": {"sections": N, "tasks": M},
        }

    When `adaptive=True` (default), personal-context.json is consulted
    and idea 17 adjustments are applied per section.
    """
    from datetime import datetime, timezone

    spec = (
        load_view(view_name_or_spec, search_dirs=search_dirs)
        if isinstance(view_name_or_spec, str) else view_name_or_spec
    )

    ctx = context if context is not None else (_load_personal_context() if adaptive else {})

    sections_out: list[dict[str, Any]] = []
    total_tasks = 0

    for section in spec.get("sections") or []:
        sid = section.get("id") or section.get("title", "unnamed")
        adjusted = _apply_adaptive(section, ctx) if adaptive else section
        tasks = run_section(adjusted, today=today)
        optional = bool(adjusted.get("optional"))
        if optional and not tasks:
            continue
        sections_out.append({
            "id": sid,
            "title": adjusted.get("title", sid),
            "tasks": tasks,
        })
        total_tasks += len(tasks)

    out = {
        "view": spec.get("name") or (view_name_or_spec if isinstance(view_name_or_spec, str) else "inline"),
        "description": spec.get("description"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sections": sections_out,
        "totals": {"sections": len(sections_out), "tasks": total_tasks},
    }
    if adaptive and ctx:
        # Surface what was applied so the brief can show "today is a
        # recovery day, plan was trimmed".
        signals = {}
        if (ctx.get("health") or {}).get("state"):
            signals["health_state"] = ctx["health"]["state"]
        if (ctx.get("travel") or {}).get("active_trip"):
            signals["traveling_to"] = ctx["travel"]["active_trip"].get("destination")
        if signals:
            out["context_applied"] = signals
    return out
