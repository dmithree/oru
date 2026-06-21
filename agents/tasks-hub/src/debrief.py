"""Debrief as event ingestion (idea 14).

The user types a freeform end-of-day note ("сделал X, перенёс Y,
заблокирован Z, ещё надо завтра позвонить врачу"). The LLM matches
each statement against today's open/next/doing tasks plus the morning
plan_333 and emits structured events that this module then applies
through the coordinator — replacing the old "parse JSON in the air,
no state change" debrief flow.

After events are applied, free-form insights are appended to a
human-readable debrief file at state/debriefs/YYYY-MM-DD-debrief.md
and personal-context.json#briefing is updated with the latest mood
and carry-over note.

Phase 3 design choices:
  - We pass a *candidate list* of tasks (id, text, status, due, tags)
    to the LLM — capped at 60 entries by priority/recency — instead of
    the full store. This keeps the prompt bounded.
  - Tool use is the structured-output channel (Anthropic native).
  - On any LLM failure we return a 502 with the upstream error and
    apply nothing; the user can rerun safely (events are idempotent
    via state-machine validation).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import litellm

from . import coordinator, store
from .config import settings

logger = logging.getLogger(__name__)


# === Candidate selection ============================================


def gather_candidates(limit: int = 60) -> list[dict[str, Any]]:
    """Tasks the user might be talking about: open/next/doing/waiting,
    ordered by recent updates first."""
    tasks = store.list_tasks(
        status=["open", "next", "doing", "waiting", "blocked", "deferred"],
        limit=limit,
        order="updated",
    )
    out: list[dict[str, Any]] = []
    for t in tasks:
        out.append({
            "id": t["id"],
            "text": t["text"],
            "status": t["status"],
            "due_at": t.get("due_at"),
            "context_tags": t.get("context_tags") or [],
            "priority": t.get("priority"),
            "project": t.get("project"),
        })
    return out


def gather_morning_plan() -> Optional[dict[str, Any]]:
    """Read today's plan_333 from personal-context.json if present."""
    p = Path(settings.personal_context_file)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return (data.get("briefing") or {}).get("plan_333")


# === LLM call ========================================================


_TOOL_SCHEMA = {
    "name": "record_debrief_events",
    "description": (
        "Record the user's end-of-day debrief as a list of structured events. "
        "Match each statement to a candidate task by id when possible. "
        "Use kind=created only when the user mentions a task that isn't in candidates."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["completed", "deferred", "blocked", "waiting", "created"],
                        },
                        "task_id": {"type": "string"},
                        "matched_text": {"type": "string"},
                        "new_text": {"type": "string"},
                        "defer_until": {"type": "string", "description": "ISO YYYY-MM-DD"},
                        "blocked_by": {"type": "string"},
                        "waiting_on": {"type": "string"},
                    },
                    "required": ["kind"],
                },
            },
            "insights": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Free-form observations or reflections from the day.",
            },
            "mood": {"type": "string"},
            "carry_over": {"type": "string"},
        },
        "required": ["events"],
    },
}


_SYSTEM = (
    "Ты — debrief-парсер для tasks-hub Димы. Получаешь его свободный текст "
    "по итогам дня и список кандидатов-задач. Возвращаешь structured events "
    "через tool call: completed / deferred / blocked / waiting / created. "
    "Жёсткие правила: matched_text — это короткая цитата из текста Димы, на "
    "которую опираешься. task_id — обязателен для completed/deferred/blocked/"
    "waiting (берёшь из candidates). new_text — только для kind=created. "
    "defer_until — ISO дата (YYYY-MM-DD); если пользователь сказал 'на завтра' "
    "— подставь завтрашнюю дату относительно today_iso. Insights, mood и "
    "carry_over — необязательные. Никаких эмодзи."
)


def call_llm(
    user_text: str,
    candidates: list[dict[str, Any]],
    *,
    morning_plan: Optional[dict[str, Any]] = None,
    model: Optional[str] = None,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Single round-trip to the LLM. Returns the parsed tool input dict.

    Raises if no tool call comes back or the call name doesn't match."""
    if not (settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")):
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    today_iso = (today or date.today()).isoformat()
    candidates_block = json.dumps(candidates, ensure_ascii=False, indent=2)
    plan_block = (
        json.dumps(morning_plan, ensure_ascii=False, indent=2)
        if morning_plan else "null"
    )
    user_block = (
        f"today_iso: {today_iso}\n\n"
        f"morning plan_333:\n{plan_block}\n\n"
        f"candidates (max 60, most-recent first):\n{candidates_block}\n\n"
        f"--- debrief text from Дима ---\n{user_text}\n--- end debrief ---"
    )

    model_id = model or f"anthropic/{settings.anthropic_model}"
    api_key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")

    resp = litellm.completion(
        model=model_id,
        api_key=api_key,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_block},
        ],
        tools=[{"type": "function", "function": {
            "name": _TOOL_SCHEMA["name"],
            "description": _TOOL_SCHEMA["description"],
            "parameters": _TOOL_SCHEMA["input_schema"],
        }}],
        tool_choice={"type": "function", "function": {"name": _TOOL_SCHEMA["name"]}},
        max_tokens=2048,
    )

    choice = resp.choices[0]
    message = choice.message
    tool_calls = getattr(message, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("LLM returned no tool calls")
    tc = tool_calls[0]
    name = getattr(tc.function, "name", None) if hasattr(tc, "function") else None
    if name != _TOOL_SCHEMA["name"]:
        raise RuntimeError(f"unexpected tool call: {name!r}")
    args_raw = getattr(tc.function, "arguments", None) if hasattr(tc, "function") else None
    if not args_raw:
        raise RuntimeError("LLM tool call had no arguments")
    return json.loads(args_raw)


# === Event applier ==================================================


def apply_events(parsed: dict[str, Any], *, agent: str = "debrief") -> list[dict[str, Any]]:
    """Apply each event via coordinator. Returns per-event outcome rows."""
    results: list[dict[str, Any]] = []
    for ev in parsed.get("events") or []:
        kind = ev.get("kind")
        tid = ev.get("task_id")
        row: dict[str, Any] = {"kind": kind, "task_id": tid, "matched_text": ev.get("matched_text")}
        try:
            if kind == "completed":
                if not tid:
                    raise ValueError("completed requires task_id")
                t = coordinator.change_status(tid, "done", agent=agent, completed_via="debrief")
                row["ok"] = True
                row["text"] = t["text"]
            elif kind == "deferred":
                if not tid:
                    raise ValueError("deferred requires task_id")
                t = coordinator.change_status(
                    tid, "deferred",
                    agent=agent,
                    defer_until=ev.get("defer_until"),
                    reason=ev.get("matched_text"),
                )
                row["ok"] = True
                row["defer_until"] = ev.get("defer_until")
                row["text"] = t["text"]
            elif kind == "blocked":
                if not tid:
                    raise ValueError("blocked requires task_id")
                t = coordinator.change_status(
                    tid, "blocked",
                    agent=agent,
                    blocked_by=ev.get("blocked_by") or "unspecified",
                )
                row["ok"] = True
                row["blocked_by"] = ev.get("blocked_by")
                row["text"] = t["text"]
            elif kind == "waiting":
                if not tid:
                    raise ValueError("waiting requires task_id")
                t = coordinator.change_status(
                    tid, "waiting",
                    agent=agent,
                    waiting_on=ev.get("waiting_on") or "unspecified",
                )
                row["ok"] = True
                row["waiting_on"] = ev.get("waiting_on")
                row["text"] = t["text"]
            elif kind == "created":
                new_text = ev.get("new_text") or ev.get("matched_text") or ""
                if not new_text:
                    raise ValueError("created requires new_text")
                # If the LLM included a date for a fresh task it's a
                # deadline (not a snooze of an existing item), so map
                # to due_at — the new task can stay status=open and
                # show up in today_due / overdue views naturally.
                extra: dict[str, Any] = {}
                if ev.get("defer_until"):
                    extra["due_at"] = ev["defer_until"]
                    extra["due_precision"] = "day"
                t = coordinator.create(new_text, source="debrief", agent=agent, **extra)
                row["ok"] = True
                row["task_id"] = t["id"]
                row["text"] = t["text"]
                if extra.get("due_at"):
                    row["due_at"] = extra["due_at"]
            else:
                raise ValueError(f"unknown event kind: {kind!r}")
        except Exception as e:  # noqa: BLE001
            row["ok"] = False
            row["error"] = str(e)
        results.append(row)
    return results


# === Side-effects: insights file + personal-context bus =============


def save_insights_file(
    user_text: str,
    parsed: dict[str, Any],
    applied: list[dict[str, Any]],
    *,
    today: Optional[date] = None,
) -> Path:
    """Append a human-readable debrief entry to state/debriefs/YYYY-MM-DD.md."""
    base = today or date.today()
    out_dir = Path(settings.db_file).parent / "debriefs"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{base.isoformat()}-debrief.md"

    lines: list[str] = []
    lines.append(f"# Debrief — {base.isoformat()}")
    lines.append("")
    lines.append(f"_recorded_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    lines.append("")
    lines.append("## Свободный текст")
    lines.append("")
    lines.append(user_text.strip())
    lines.append("")

    if parsed.get("mood"):
        lines.append(f"**Настроение:** {parsed['mood']}")
        lines.append("")
    if parsed.get("carry_over"):
        lines.append("## Carry-over на завтра")
        lines.append("")
        lines.append(parsed["carry_over"])
        lines.append("")
    if parsed.get("insights"):
        lines.append("## Insights")
        lines.append("")
        for ins in parsed["insights"]:
            lines.append(f"- {ins}")
        lines.append("")

    lines.append("## Применённые события")
    lines.append("")
    for row in applied:
        flag = "ok" if row.get("ok") else "FAIL"
        kind = row.get("kind")
        text = row.get("text", "")
        tid_short = (row.get("task_id") or "")[:8]
        extra = []
        if row.get("defer_until"):
            extra.append(f"defer_until={row['defer_until']}")
        if row.get("blocked_by"):
            extra.append(f"blocked_by={row['blocked_by']}")
        if row.get("waiting_on"):
            extra.append(f"waiting_on={row['waiting_on']}")
        if not row.get("ok"):
            extra.append(f"error={row.get('error')}")
        extra_str = (" — " + ", ".join(extra)) if extra else ""
        lines.append(f"- [{flag}] {kind} {tid_short} — {text}{extra_str}")
    lines.append("")

    # Append (not overwrite) so multiple debriefs in one day stack
    with p.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n\n---\n\n")
    return p


def update_personal_context(parsed: dict[str, Any]) -> None:
    """Update briefing.last_evening_at, last_mood, carry_over."""
    p = Path(settings.personal_context_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if p.exists():
        try:
            current = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    briefing = current.get("briefing") or {}
    briefing["last_evening_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if parsed.get("mood"):
        briefing["last_mood"] = parsed["mood"]
    if parsed.get("carry_over"):
        briefing["carry_over"] = parsed["carry_over"]
    current["briefing"] = briefing
    p.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


# === Public entry point =============================================


def ingest_debrief(
    user_text: str,
    *,
    model: Optional[str] = None,
    today: Optional[date] = None,
    agent: str = "debrief",
) -> dict[str, Any]:
    """End-to-end: candidates + plan -> LLM -> apply events -> save insights.

    Returns:
        {
          "parsed": {...llm output...},
          "applied": [...per-event rows...],
          "summary": {events: N, ok: M, failed: K},
          "debrief_file": "/opt/state/debriefs/YYYY-MM-DD-debrief.md",
        }
    """
    candidates = gather_candidates()
    plan = gather_morning_plan()
    parsed = call_llm(user_text, candidates, morning_plan=plan, model=model, today=today)
    applied = apply_events(parsed, agent=agent)
    df = save_insights_file(user_text, parsed, applied, today=today)
    update_personal_context(parsed)

    ok_count = sum(1 for r in applied if r.get("ok"))
    return {
        "parsed": parsed,
        "applied": applied,
        "summary": {
            "events": len(applied),
            "ok": ok_count,
            "failed": len(applied) - ok_count,
        },
        "debrief_file": str(df),
    }
