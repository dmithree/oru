"""Build morning brief and evening debrief via LLM.

Morning: pulls all sources -> 11-section structured brief per .claude/docs/morning-briefing.md
Evening: prompts user for debrief input, parses response into structured debrief.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, date
from typing import Any

from litellm import completion

from .config import settings
from . import state_bus
from .fetchers import oura, strava, tasks, linear, reminders, transcripts, nudges

logger = logging.getLogger(__name__)


TRANSCRIPT_DIRS = [
    "/opt/data/personal/therapy/transcripts",
    "/opt/data/personal/coach/transcripts",
    "/opt/data/personal/therapy/transcripts/Raw",
]


def _morning_prompt(data: dict[str, Any]) -> str:
    today = date.today()
    weekday_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][today.weekday()]
    sections_block = data.get("tasks_hub_sections_markdown") or ""
    context_signal = data.get("tasks_hub_context_applied") or {}
    signal_line = ""
    if context_signal.get("health_state") == "recovery_needed":
        signal_line = "\nВажный сигнал: today is a recovery day — план задач уже урезан вдвое, в брифе подсветить."

    # Cross-domain proactive nudges (health, travel, ...), ranked by urgency.
    # Each domain exposes ONE question/day via the uniform contract; the brief
    # renders them as a single «Сегодня двигаем» section. Verbatim — no passive
    # status lines invented by the LLM.
    nudges = data.get("nudges") or []
    travel_nudge = next((n for n in nudges if n.get("domain") == "travel"), None)
    days_until = travel_nudge.get("days_until") if travel_nudge else None
    # Only force a first-line travel mention when imminent/active (≤7 дн).
    if context_signal.get("traveling_to") and (days_until is None or days_until <= 7):
        signal_line += f"\nВажный сигнал: traveling to {context_signal['traveling_to']} — упомянуть в первой строке."

    nudge_block = ""
    if nudges:
        labels = {"health": "Здоровье", "travel": "Поездка"}
        lines = []
        for n in nudges:
            dom = labels.get(n["domain"], n["domain"])
            tag = n.get("topic_label") or ""
            header = f"{dom}" + (f" ({tag})" if tag else "")
            if n["domain"] == "travel" and n.get("days_until") is not None:
                header += f", до старта {n['days_until']} дн."
            lines.append(f"- {header}: {n['question']}")
        nudge_block = (
            "\n\n«Сегодня двигаем» — вставь СЛЕДУЮЩИЕ пункты VERBATIM отдельной "
            "секцией в конце брифа, каждый своим буллетом, в этом порядке "
            "(по убыванию срочности). Не перефразируй, не сокращай, не добавляй "
            "пассивных статусных строк:\n" + "\n".join(lines)
        )

    return f"""Ты Oru, личный AI-ассистент Димы. Сформируй утренний бриф на {weekday_ru} {today.day}.{today.month}.{today.year}.

Жёсткие правила:
- НИКАКИХ эмодзи. Ни одного.
- Прямой тон. Без приветствий "Доброе утро, дорогой". Дима не любит сахар.
- Конкретика: цифры, имена, точное время. Если данных нет — пропусти, не выдумывай.
- Без слов-паразитов: "просто", "по сути", "в общем".
- Про поездку и здоровье в секции «Сегодня двигаем» пиши ТОЛЬКО то, что дано ниже. Не выдумывай статусных строк.

Структура (только секции у которых есть данные; пустые — пропусти):

Состояние тела: одна строка с Oura — sleep, readiness, HRV. Если recovery_needed — пометь.
Вчера: одна строка с Strava (если активность была).
Темы недели (из therapy/coaching транскриптов за 24h): 1-2 строки если есть.

Задачи (готовый блок ниже, от tasks-hub) — вставь его VERBATIM, не перефразируй,
не переформатируй маркдаун, не убирай заголовки секций (carry_over, overdue,
today_due, plan_333_deep/short/admin, waiting, recent_open):

{sections_block or "(tasks-hub недоступен — пропусти этот блок)"}
{signal_line}{nudge_block}

Данные (для секций состояния тела / Strava / транскриптов):
{json.dumps({k: v for k, v in data.items() if k not in {"tasks_hub_sections_markdown", "tasks_hub_context_applied", "tasks_hub_sections", "nudges"}}, ensure_ascii=False, indent=2)}

Бриф:"""


def _debrief_parse_prompt(user_text: str, plan: dict) -> str:
    return f"""Дима написал вечерний debrief в свободной форме. Распарси его в структуру.

Сегодняшний план был:
{json.dumps(plan, ensure_ascii=False, indent=2)}

Debrief Димы:
{user_text}

Верни ТОЛЬКО валидный JSON (без markdown, без эмодзи) с полями:
- "done": список задач из плана которые Дима явно отметил сделанными
- "missed": список задач которые остались
- "insights": список инсайтов / наблюдений (string array)
- "carry_over": одна строка — что переносится на завтра (или null если ничего)
- "mood": одна строка — общее настроение дня (или null)

JSON:"""


def _render_tasks_hub_sections(view: dict[str, Any]) -> str:
    """Render the structured tasks-hub view as a markdown block the LLM
    pastes verbatim. We do the layout here so the prompt doesn't have
    to reason about empty sections — they just don't appear."""
    sections = view.get("sections") or []
    if not sections:
        return ""
    lines: list[str] = []
    for s in sections:
        tasks_list = s.get("tasks") or []
        if not tasks_list:
            continue
        lines.append(f"## {s.get('title', s.get('id', ''))}")
        lines.append("")
        for t in tasks_list:
            text = t.get("text", "")
            decorations: list[str] = []
            due = t.get("due_at")
            if due:
                try:
                    y, m, d = due.split("-")
                    yy = date.today().year
                    decorations.append(f"({d}.{m})" if int(y) == yy else f"({d}.{m}.{y})")
                except ValueError:
                    decorations.append(f"({due})")
            tags = t.get("context_tags") or []
            if tags:
                decorations.append(" ".join(tags))
            eff = t.get("effort_min")
            if eff is not None:
                if eff >= 90:
                    decorations.append("~deep")
                elif eff >= 60 and eff % 60 == 0:
                    decorations.append(f"~{eff // 60}h")
                else:
                    decorations.append(f"~{eff}m")
            pri = t.get("priority")
            if pri:
                decorations.append(f"!{pri}")
            suffix = " " + " ".join(decorations) if decorations else ""
            lines.append(f"- {text}{suffix}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _derive_plan_333_from_view(view: dict[str, Any]) -> dict[str, list[str]]:
    """Pull plan_333 directly from structured view sections instead of
    re-regex'ing it back out of the LLM summary."""
    plan = {"deep": [], "short": [], "ai": []}
    section_map = {
        "plan_333_deep":  "deep",
        "plan_333_short": "short",
        "plan_333_admin": "short",   # admin stacks with short for the legacy 3-3-3 shape
        "plan_333_ai":    "ai",
    }
    for s in view.get("sections", []):
        key = section_map.get(s.get("id"))
        if not key:
            continue
        for t in s.get("tasks", []):
            txt = t.get("text", "").strip()
            if txt and txt not in plan[key]:
                plan[key].append(txt)
    return plan


def build_morning_brief() -> dict[str, Any]:
    oura_data = oura.fetch_today()
    strava_data = strava.fetch_yesterday()

    # Phase 3 brief refactor: pull the structured morning view from
    # tasks-hub instead of fetching a flat task list. Render the sections
    # to markdown here and feed both the markdown block and the raw view
    # to the LLM, so the prompt can paste sections verbatim AND derive
    # plan_333 without regex.
    hub_view = tasks.fetch_brief_sections() or {}
    sections_md = _render_tasks_hub_sections(hub_view)
    context_applied = hub_view.get("context_applied") or {}

    # Legacy flat list kept as a fallback when tasks-hub is down; analyzer
    # falls back to the markdown file the brief used pre-Phase-2.5.
    open_tasks = tasks.read_open_tasks(limit=30) if not sections_md else []

    linear_stuck = linear.fetch_stuck()
    reminders_data = reminders.fetch_today_reminders()
    recent_transcripts = transcripts.fetch_recent_transcripts(TRANSCRIPT_DIRS)
    context = state_bus.read()
    all_nudges = nudges.fetch_all_nudges()

    raw = {
        "oura": oura_data,
        "strava_yesterday": strava_data,
        "open_tasks": open_tasks[:15],
        "linear_stuck": linear_stuck,
        "reminders": reminders_data,
        "recent_transcripts_count": len(recent_transcripts),
        "transcript_snippets": [t["content"][:1000] for t in recent_transcripts],
        "personal_context": {
            "health": context.get("health", {}),
            "briefing": context.get("briefing", {}),
        },
        # Cross-domain proactive nudges (health, travel, ...). Each domain
        # exposes ONE question/day via the uniform contract; ranked by urgency
        # and rendered verbatim as «Сегодня двигаем» by _morning_prompt. We no
        # longer dump raw trip/health-status data that produced passive lines.
        "nudges": all_nudges,
        # Hand-off to the LLM prompt — verbatim block + context signal.
        "tasks_hub_sections_markdown": sections_md,
        "tasks_hub_context_applied": context_applied,
        "tasks_hub_sections": hub_view.get("sections"),
    }

    summary = "(LLM не настроен)"
    # Prefer pulling plan_333 from the structured view directly.
    plan = _derive_plan_333_from_view(hub_view) if hub_view else {"deep": [], "short": [], "ai": []}

    if settings.anthropic_api_key:
        try:
            resp = completion(
                model=f"anthropic/{settings.anthropic_model}",
                messages=[{"role": "user", "content": _morning_prompt(raw)}],
                max_tokens=1100,
                temperature=0.3,
                api_key=settings.anthropic_api_key,
            )
            summary = resp.choices[0].message.content.strip()
            # If hub_view was empty, fall back to the regex extractor
            # so plan_333 still gets populated from the LLM output.
            if not any(plan.values()):
                plan = _extract_333(summary)
        except Exception:
            logger.exception("Morning LLM failed")
            summary = "(LLM ошибка; см. логи)"

    brief = {
        "generated_at": datetime.now().isoformat(),
        "kind": "morning",
        "summary": summary,
        "plan_333": plan,
        "data": raw,
    }

    state_bus.write_section("briefing", {
        "last_morning_at": brief["generated_at"],
        "plan_333": plan,
        "carry_over": context.get("briefing", {}).get("carry_over", ""),
    })
    return brief


def _extract_333(summary: str) -> dict[str, list[str]]:
    """Best-effort regex extraction of 3-3-3 plan from LLM markdown."""
    import re
    out = {"deep": [], "short": [], "ai": []}
    sections = {"deep": r"Deep[:：]?\s*(.+?)(?=Short|Short[a-zA-Z]|AI|$)",
                "short": r"Short[:：]?\s*(.+?)(?=AI|Deep|$)",
                "ai": r"AI[:：]?\s*(.+?)(?=Deep|Short|$)"}
    body = " ".join(summary.split())
    for key, pat in sections.items():
        m = re.search(pat, body, re.IGNORECASE | re.DOTALL)
        if m:
            chunk = m.group(1).strip()
            items = [x.strip(" -•,;") for x in re.split(r"[,;\n•]+", chunk) if x.strip()]
            out[key] = items[:3]
    return out


def parse_evening_debrief(user_text: str) -> dict[str, Any]:
    """Parse Dima's freeform evening text into structured debrief."""
    context = state_bus.read()
    plan = context.get("briefing", {}).get("plan_333", {})

    parsed: dict[str, Any] = {"raw": user_text, "done": [], "missed": [], "insights": [], "carry_over": None, "mood": None}
    if settings.anthropic_api_key:
        try:
            resp = completion(
                model=f"anthropic/{settings.anthropic_model}",
                messages=[{"role": "user", "content": _debrief_parse_prompt(user_text, plan)}],
                max_tokens=600,
                temperature=0.1,
                api_key=settings.anthropic_api_key,
            )
            content = resp.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1].lstrip("json").strip()
            parsed.update(json.loads(content))
        except Exception:
            logger.exception("Debrief parse failed")

    state_bus.write_section("briefing", {
        **context.get("briefing", {}),
        "last_evening_at": datetime.now().isoformat(),
        "carry_over": parsed.get("carry_over") or "",
        "last_mood": parsed.get("mood"),
    })

    return {
        "generated_at": datetime.now().isoformat(),
        "kind": "evening",
        "parsed": parsed,
    }
