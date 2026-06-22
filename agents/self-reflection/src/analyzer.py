"""IFS analyzer — reads recent therapy + coaching summaries, asks the
LLM to extract structured data, emits homework as TaskCreated events
into tasks-hub, updates personal-context bus, writes a per-run report.

Tool-use schema forces structured output so we don't have to parse
freeform markdown.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import litellm

from . import state_bus, tasks_hub_client
from .config import settings

logger = logging.getLogger(__name__)


TOOL_NAME = "record_self_reflection"

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Record structured findings from the user's recent therapy/coaching "
        "transcripts: active IFS parts, recurring themes, and concrete "
        "homework items the user agreed to or could benefit from."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "active_parts": {
                "type": "array",
                "description": "IFS parts that surfaced in the sessions. Short Russian labels.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "role": {"type": "string", "description": "manager | firefighter | exile"},
                        "note": {"type": "string", "description": "one-sentence context for what triggered this part"},
                    },
                    "required": ["name"],
                },
            },
            "weekly_themes": {
                "type": "array",
                "description": "1-4 short thematic threads across the recent sessions.",
                "items": {"type": "string"},
            },
            "homework": {
                "type": "array",
                "description": "Actionable items. Each becomes a tasks-hub task.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "context_tags": {"type": "array", "items": {"type": "string"}},
                        "priority": {"type": "string", "description": "P0..P3"},
                        "due_at": {"type": "string", "description": "ISO YYYY-MM-DD"},
                        "recurrence": {
                            "type": "string",
                            "description": "every:Nd|w|m|y or every:weekday. Omit for one-off (e.g., 'позвонить бабушке' is one-off; '3 раза за неделю замечать X' is every:1w).",
                        },
                    },
                    "required": ["text"],
                },
            },
            "carry_over": {
                "type": "string",
                "description": "One sentence the user might want surfaced in tomorrow's brief.",
            },
            "summary_for_telegram": {
                "type": "string",
                "description": "2-4 line summary suitable for Telegram. No emoji, direct tone.",
            },
        },
        "required": ["active_parts", "weekly_themes", "homework"],
    },
}


_SYSTEM = (
    "Ты IFS-аналитик Димы. Получаешь несколько последних саммари его "
    "терапевтических и коучинговых сессий и возвращаешь structured findings "
    "через tool call: активные части, темы, конкретные домашки, carry-over. "
    "Жёсткие правила: никаких эмодзи; части — короткие русские лейблы "
    "(Достигатор, Защитник, Контролёр и т.п.); homework.text должен быть "
    "конкретным, не общим ('Заметить momentum Достигатора 3 раза за неделю', "
    "не 'работать над собой'); если в данных нет ничего нового — возврати "
    "пустые массивы, не выдумывай."
)


def _hash_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        h.update(fh.read())
    return h.hexdigest()[:16]


def _load_processed() -> dict[str, Any]:
    p = Path(settings.processed_log)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_processed(state: dict[str, Any]) -> None:
    p = Path(settings.processed_log)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_new_transcripts(
    *,
    since_days: Optional[int] = None,
    max_files: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Find summary files newer than the last processed marker. Returns
    [{path, kind, content, hash}]. We hash so the same file edited
    twice gets re-analysed but identical re-runs are no-ops."""
    since_days = since_days if since_days is not None else settings.since_days
    max_files = max_files if max_files is not None else settings.max_files_per_run
    processed = _load_processed()
    out: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400

    for kind, base in (("therapy", settings.therapy_dir), ("coach", settings.coach_dir)):
        base_p = Path(base)
        if not base_p.exists():
            logger.info("self-reflection: %s dir missing at %s", kind, base)
            continue
        for f in sorted(base_p.glob("*.md")):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            h = _hash_file(f)
            key = f"{kind}:{f.name}"
            if processed.get(key) == h:
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                logger.exception("read failed: %s", f)
                continue
            out.append({
                "path": str(f),
                "rel": f.name,
                "kind": kind,
                "content": content,
                "hash": h,
                "key": key,
            })
            if len(out) >= max_files:
                return out
    return out


def call_llm(transcripts: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not (settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")):
        logger.warning("self-reflection: ANTHROPIC_API_KEY not set; skipping")
        return None
    if not transcripts:
        return {"active_parts": [], "weekly_themes": [], "homework": []}

    block = "\n\n---\n\n".join(
        f"### {t['kind']} :: {t['rel']}\n\n{t['content']}"
        for t in transcripts
    )
    user = (
        "Свежие саммари ({n} файлов) ниже. Извлеки структуру через tool call.\n\n"
        "{b}"
    ).format(n=len(transcripts), b=block)

    api_key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
    resp = litellm.completion(
        model=f"anthropic/{settings.anthropic_model}",
        api_key=api_key,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        tools=[{"type": "function", "function": {
            "name": TOOL_SCHEMA["name"],
            "description": TOOL_SCHEMA["description"],
            "parameters": TOOL_SCHEMA["input_schema"],
        }}],
        tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
        max_tokens=2048,
    )
    msg = resp.choices[0].message
    tcs = getattr(msg, "tool_calls", None) or []
    if not tcs:
        logger.warning("self-reflection: LLM returned no tool call")
        return None
    return json.loads(tcs[0].function.arguments)


def emit_homework(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each homework item, emit into tasks-hub. ext_id is a stable
    hash of the text so re-runs dedup. Recurrence is LLM-decided —
    "позвонить бабушке" is one-off, "3 раза за неделю замечать X" is
    every:1w. Without recurrence, completed tasks don't auto-respawn."""
    out: list[dict[str, Any]] = []
    for h in items or []:
        text = (h.get("text") or "").strip()
        if not text:
            continue
        ext_id = "selfref:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        kwargs: dict[str, Any] = {
            "owner_agent": "self-reflection",
            "ext_id": ext_id,
            "context_tags": h.get("context_tags"),
            "priority": h.get("priority"),
            "due_at": h.get("due_at"),
        }
        recurrence = h.get("recurrence")
        if recurrence:
            kwargs["recurrence"] = recurrence
        try:
            task = tasks_hub_client.emit_task(text, **{k: v for k, v in kwargs.items() if v is not None})
            out.append({
                "ok": True, "ext_id": ext_id, "task_id": task.get("id"),
                "text": text, "recurrence": recurrence,
            })
        except tasks_hub_client.TasksHubError as e:
            out.append({"ok": False, "ext_id": ext_id, "text": text, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            out.append({"ok": False, "ext_id": ext_id, "text": text, "error": repr(e)})
    return out


def update_context(parsed: dict[str, Any]) -> None:
    parts = parsed.get("active_parts") or []
    state_bus.write_section("self", {
        "last_analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "active_parts": [p.get("name") for p in parts if p.get("name")],
        "weekly_themes": parsed.get("weekly_themes") or [],
        "homework_open": len(parsed.get("homework") or []),
        "carry_over": parsed.get("carry_over"),
    })


def write_state(parsed: dict[str, Any], applied: list[dict[str, Any]],
                processed_count: int) -> Path:
    p = Path(settings.state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "transcripts_processed": processed_count,
        "active_parts": parsed.get("active_parts") or [],
        "weekly_themes": parsed.get("weekly_themes") or [],
        "homework": parsed.get("homework") or [],
        "carry_over": parsed.get("carry_over"),
        "summary_for_telegram": parsed.get("summary_for_telegram"),
        "homework_applied": applied,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def run(*, dry_run: bool = False) -> dict[str, Any]:
    """End-to-end. Returns the run report."""
    transcripts = collect_new_transcripts()
    if not transcripts:
        logger.info("self-reflection: no new transcripts")
        return {"ok": True, "transcripts": 0, "applied": [], "parsed": None}

    parsed = call_llm(transcripts) or {}
    applied: list[dict[str, Any]] = []
    if not dry_run and parsed.get("homework"):
        applied = emit_homework(parsed["homework"])
        update_context(parsed)
        # Mark these transcripts processed so next run skips them
        processed = _load_processed()
        for t in transcripts:
            processed[t["key"]] = t["hash"]
        _save_processed(processed)

    state_path = write_state(parsed, applied, len(transcripts)) if not dry_run else None
    return {
        "ok": True,
        "transcripts": len(transcripts),
        "applied": applied,
        "parsed": parsed,
        "state_file": str(state_path) if state_path else None,
    }
