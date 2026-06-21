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
from .fetchers import oura, strava, tasks, linear, reminders, transcripts

logger = logging.getLogger(__name__)


TRANSCRIPT_DIRS = [
    "/opt/data/personal/therapy/transcripts",
    "/opt/data/personal/coach/transcripts",
    "/opt/data/personal/therapy/transcripts/Raw",
]


def _morning_prompt(data: dict[str, Any]) -> str:
    today = date.today()
    weekday_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][today.weekday()]
    return f"""Ты Oru, личный AI-ассистент Димы. Сформируй утренний бриф на {weekday_ru} {today.day}.{today.month}.{today.year}.

Жёсткие правила:
- НИКАКИХ эмодзи. Ни одного.
- Прямой тон. Без приветствий "Доброе утро, дорогой". Дима не любит сахар.
- Конкретика: цифры, имена, точное время. Если данных нет — пропусти, не выдумывай.
- Без слов-паразитов: "просто", "по сути", "в общем".

Структура (только секции у которых есть данные; пустые — пропусти):

Состояние тела: одна строка с Oura — sleep, readiness, HRV. Если recovery_needed — пометь.
Вчера: одна строка с Strava (если активность была).
События дня (Apple Reminders с due=сегодня): bullet list.
Просрочено: bullet list (Reminders overdue + Linear stuck).
Открытые задачи: top-5 из tasks rollup.
Carry-over: одна строка если есть в personal_context.briefing.carry_over.
Темы недели (из therapy/coaching транскриптов за 24h): 1-2 строки если есть.
3-3-3 plan: предложи 3 deep, 3 short, 3 AI-задачи на сегодня — из открытых задач и просроченного. Если задач мало — меньше.

Данные:
{json.dumps(data, ensure_ascii=False, indent=2)}

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


def build_morning_brief() -> dict[str, Any]:
    oura_data = oura.fetch_today()
    strava_data = strava.fetch_yesterday()
    open_tasks = tasks.read_open_tasks(limit=30)
    linear_stuck = linear.fetch_stuck()
    reminders_data = reminders.fetch_today_reminders()
    recent_transcripts = transcripts.fetch_recent_transcripts(TRANSCRIPT_DIRS)
    context = state_bus.read()

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
            "travel": context.get("travel", {}),
            "briefing": context.get("briefing", {}),
        },
    }

    summary = "(LLM не настроен)"
    plan: dict[str, list[str]] = {"deep": [], "short": [], "ai": []}
    if settings.anthropic_api_key:
        try:
            resp = completion(
                model=f"anthropic/{settings.anthropic_model}",
                messages=[{"role": "user", "content": _morning_prompt(raw)}],
                max_tokens=900,
                temperature=0.3,
                api_key=settings.anthropic_api_key,
            )
            summary = resp.choices[0].message.content.strip()
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
