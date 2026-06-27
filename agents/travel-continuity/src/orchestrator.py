"""Phase orchestration + LLM-driven outputs.

Phases by date relative to active_trip:
  - pre-trip:  start_date - 7  <=  today  <  start_date
  - active:    start_date      <=  today  <=  end_date
  - post-trip: today == end_date + 1

Outside these windows: idle (no notifications).
"""
from __future__ import annotations
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from litellm import completion

from .config import settings
from . import state_bus, preferences
from .fetchers import oura, strava, weather, golden_hour

logger = logging.getLogger(__name__)


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def detect_phase(today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    trip = state_bus.get_active_trip()
    if not trip:
        return {"phase": "idle", "trip": None}
    start = _parse_date(trip.get("start_date", ""))
    end = _parse_date(trip.get("end_date", ""))
    if not start or not end:
        return {"phase": "idle", "trip": trip, "error": "invalid dates"}

    pre_start = start - timedelta(days=7)
    if today < pre_start:
        return {"phase": "scheduled_distant", "trip": trip, "days_until": (start - today).days}
    if pre_start <= today < start:
        return {"phase": "pre_trip", "trip": trip, "days_until": (start - today).days}
    if start <= today <= end:
        day_num = (today - start).days + 1
        total = (end - start).days + 1
        return {"phase": "active", "trip": trip, "day_of_trip": day_num, "total_days": total}
    if today == end + timedelta(days=1):
        return {"phase": "post_trip", "trip": trip}
    return {"phase": "past", "trip": trip, "days_since": (today - end).days}


# ---------- LLM helpers ----------

def _llm(prompt: str, max_tokens: int = 800) -> str:
    if not settings.anthropic_api_key:
        return "(LLM не настроен)"
    try:
        resp = completion(
            model=f"anthropic/{settings.anthropic_model}",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.3,
            api_key=settings.anthropic_api_key,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        logger.exception("LLM call failed")
        return "(LLM ошибка)"


# ---------- Distant-phase engagement ----------
#
# One proactive question/offer per day in the morning brief, rotating through
# trip-prep topics. Urgency-gated: a topic only becomes eligible once we're
# within `open_days_before` of departure, and within the eligible set we pick
# by priority (lower = more urgent) and least-recently-asked.
#
# Oru PROACTIVELY OFFERS to do the work (price alerts, gather options, book on
# OK), not just asks. The actual research lives in research-notes.md, refreshed
# weekly by a Hermes cron.

# topic_id, label, priority (1=most urgent), open_days_before (eligible when days_until <= this)
TOPIC_BACKLOG: list[dict[str, Any]] = [
    {"id": "flights",   "label": "Авиабилеты",        "priority": 1, "open_days_before": 9999},
    {"id": "visa",      "label": "Виза и документы",  "priority": 2, "open_days_before": 9999},
    {"id": "stay",      "label": "Жильё",             "priority": 3, "open_days_before": 110},
    {"id": "budget",    "label": "Бюджет",            "priority": 4, "open_days_before": 90},
    {"id": "poi",       "label": "Что посмотреть",    "priority": 5, "open_days_before": 70},
    {"id": "bookings",  "label": "Брони заранее",     "priority": 6, "open_days_before": 40},
    {"id": "health",    "label": "Здоровье",          "priority": 7, "open_days_before": 28},
    {"id": "packing",   "label": "Сборы",             "priority": 8, "open_days_before": 12},
]


def _read_research_notes(trip: dict[str, Any]) -> str:
    """Read research-notes.md from the trip folder (refreshed weekly by Hermes cron)."""
    start = _parse_date(trip.get("start_date", ""))
    year = start.year if start else date.today().year
    slug = trip.get("slug") or f"{trip['destination'].lower().replace(' ', '-')}-{trip['start_date']}"
    notes_file = Path(settings.travel_dir) / str(year) / slug / "research-notes.md"
    if notes_file.exists():
        return notes_file.read_text(encoding="utf-8")
    return ""


def _pick_topic(trip: dict[str, Any], days_until: int, today: date) -> dict[str, Any] | None:
    """Pick today's topic: eligible (by urgency window) + least-recently / least-asked,
    tie-broken by priority. Avoid repeating yesterday's topic when alternatives exist."""
    slug = trip.get("slug") or trip["destination"]
    rec = state_bus.get_engagement(slug)
    covered = rec.get("covered", {})
    last_topic = rec.get("last_topic")

    eligible = [t for t in TOPIC_BACKLOG if days_until <= t["open_days_before"]]
    if not eligible:
        return None

    def sort_key(t: dict[str, Any]) -> tuple:
        c = covered.get(t["id"], {})
        count = int(c.get("count", 0))
        asked_on = c.get("asked_on") or "0000-00-00"
        # least-asked first, then oldest-asked, then most urgent (priority)
        return (count, asked_on, t["priority"])

    ordered = sorted(eligible, key=sort_key)
    # don't repeat yesterday's topic if there's another option at the same min tier
    if len(ordered) > 1 and ordered[0]["id"] == last_topic:
        return ordered[1]
    return ordered[0]


def build_distant_engagement(trip: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Generate ONE proactive question/offer for today's morning brief."""
    today = today or date.today()
    start = _parse_date(trip["start_date"])
    days_until = (start - today).days if start else 0
    notes = _read_research_notes(trip)
    topic = _pick_topic(trip, days_until, today)

    if topic is None:
        return {
            "generated_at": datetime.now().isoformat(),
            "kind": "distant_engagement",
            "trip": trip,
            "topic": None,
            "question": "",
        }

    slug = trip.get("slug") or trip["destination"]
    rec = state_bus.get_engagement(slug)
    decisions = rec.get("decisions", [])
    decided_topics = {d["topic"] for d in decisions}
    already = "Уже решено по этим темам: " + ", ".join(sorted(decided_topics)) if decided_topics else "Пока ничего не решено."

    prompt = f"""Ты Oru — личный тревел-ассистент Димы. Поездка {trip['destination']} ({trip['start_date']} → {trip['end_date']}), до старта {days_until} дн.

Тема сегодня: {topic['label']} (id={topic['id']}).

Твоя задача: ОДИН короткий проактивный вопрос-предложение для утреннего брифа по этой теме. Не пассивная констатация — ты ведёшь Диму к решению и ПРЕДЛАГАЕШЬ сделать работу сам (поставить price alert, собрать варианты с ценами, проверить визу, забронировать по его ОК).

Жёсткие правила:
- БЕЗ эмодзи
- 2-4 предложения максимум, плотно, по делу
- На русском, на "ты", неформально
- Конкретика из research ниже (цифры, районы, окна покупки), не общие слова
- Заверши конкретным предложением действия ("поставить alert?", "собрать 3 варианта?", "забронировать?")
- НЕ повторяй то, что уже решено

{already}

Research notes (твой источник фактов):
{notes[:6000]}

Вопрос-предложение на сегодня:"""
    question = _llm(prompt, max_tokens=700)

    return {
        "generated_at": datetime.now().isoformat(),
        "kind": "distant_engagement",
        "trip": trip,
        "days_until": days_until,
        "topic": topic["id"],
        "topic_label": topic["label"],
        "question": question,
    }


def build_distant_weekly_summary(trip: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Weekly recap: what's decided, what we learned this week, the overall picture."""
    today = today or date.today()
    start = _parse_date(trip["start_date"])
    days_until = (start - today).days if start else 0
    notes = _read_research_notes(trip)
    slug = trip.get("slug") or trip["destination"]
    rec = state_bus.get_engagement(slug)
    decisions = rec.get("decisions", [])
    covered = rec.get("covered", {})

    decisions_txt = "\n".join(f"- {d['on']} [{d['topic']}]: {d['note']}" for d in decisions) or "(пока решений нет)"
    asked_txt = ", ".join(f"{tid}×{c.get('count', 0)}" for tid, c in covered.items()) or "(тем ещё не касались)"

    prompt = f"""Ты Oru. Еженедельная итоговая сводка по подготовке к поездке Димы: {trip['destination']} ({trip['start_date']} → {trip['end_date']}), до старта {days_until} дн.

Структура (плотно, без воды, без эмодзи, на русском, на "ты"):
1. Что уже решено/сделано (из decisions)
2. Что нового узнал за неделю (свежие находки из research notes — цены, окна, риски)
3. Общая картина: что горит сейчас, что следующее по приоритету
4. 1-2 конкретных предложения действий на эту неделю

Решения на данный момент:
{decisions_txt}

Темы, которые поднимали: {asked_txt}

Research notes:
{notes[:7000]}

Еженедельная сводка:"""
    summary = _llm(prompt, max_tokens=900)

    rec["last_weekly_on"] = today.isoformat()
    state_bus.save_engagement(slug, rec)

    return {
        "generated_at": datetime.now().isoformat(),
        "kind": "distant_weekly_summary",
        "trip": trip,
        "days_until": days_until,
        "summary": summary,
    }


# ---------- Pre-trip ----------

def build_pretrip(trip: dict[str, Any]) -> dict[str, Any]:
    """Packing list + passport reminder + weather + Muji + golden hours for trip dates."""
    prefs = preferences.load_all()
    start = _parse_date(trip["start_date"])
    end = _parse_date(trip["end_date"])
    trip_days = (end - start).days + 1

    coords = weather.lookup_coords(trip["destination"])
    if coords:
        lat, lon, tz = coords
        forecast = weather.forecast(lat, lon, tz, days=min(trip_days + 1, 14))
        first_day_golden = golden_hour.times_for(lat, lon, tz, target=start)
    else:
        lat = lon = None
        tz = trip.get("timezone", "UTC")
        forecast = {}
        first_day_golden = {}

    prompt = f"""Ты Oru. Готовишь pre-trip pack/info для Димы на поездку в {trip['destination']} ({trip['start_date']} → {trip['end_date']}, {trip_days} дней).

Жёсткие правила:
- БЕЗ эмодзи
- БЕЗ воды. Только конкретика
- На русском

Структура (только секции у которых есть данные):

1. Паспорта: упомянуть что у Димы два загранпаспорта; явно спросить какой паспорт берёт с визой для {trip['destination']}
2. Packing-list: применяя правила из user_packing_standard (шампунь по длине поездки, обязательные пункты)
3. Погода: краткий прогноз на дни поездки из forecast
4. Golden hour первого дня (если есть)
5. Muji: ближайший крупный/флагман в {trip['destination']} (используй своё знание)
6. Места для матчи / хорошего кофе (Blank Street или местные)
7. Японские пекарни (если применимо)
8. Прогулочные маршруты — Дима ходит пешком (см user_travel_walking)

Memory files (user preferences):
{json.dumps({k: v[:1500] for k, v in prefs.items()}, ensure_ascii=False, indent=2)}

Weather forecast:
{json.dumps(forecast, ensure_ascii=False, indent=2)}

Golden hour day 1:
{json.dumps(first_day_golden, ensure_ascii=False, indent=2)}

Pre-trip:"""
    text = _llm(prompt, max_tokens=1200)
    return {
        "generated_at": datetime.now().isoformat(),
        "kind": "pre_trip",
        "trip": trip,
        "weather_forecast": forecast,
        "golden_hour_day_1": first_day_golden,
        "summary": text,
    }


# ---------- Active morning ----------

def build_active_morning(trip: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    day_num = (today - _parse_date(trip["start_date"])).days + 1
    total = (_parse_date(trip["end_date"]) - _parse_date(trip["start_date"])).days + 1

    oura_data = oura.fetch_today()
    coords = weather.lookup_coords(trip["destination"])
    if coords:
        lat, lon, tz = coords
        forecast = weather.forecast(lat, lon, tz, days=2)
        gh = golden_hour.times_for(lat, lon, tz, target=today)
    else:
        forecast = {}
        gh = {}

    intensity = "облегчённый" if (oura_data.get("readiness") or 100) < 65 else "полный"

    prompt = f"""Ты Oru. Утренняя сводка дня {day_num}/{total} поездки в {trip['destination']}.

Жёсткие правила:
- Без эмодзи
- Конкретика
- Русский
- Учти что readiness={oura_data.get('readiness')} → темп {intensity}

Структура:
1. Заголовок дня: "{trip['destination']} — день {day_num}/{total}"
2. Ночь (sleep / readiness одной строкой)
3. Погода сегодня (одна строка)
4. Golden hour сегодня (если есть)
5. Top-3 рекомендации на день — учти темп ({intensity}), пешеходные маршруты, Muji интерес, поиск матчи

Данные:
- Oura: {json.dumps(oura_data, ensure_ascii=False)}
- Forecast: {json.dumps(forecast.get('daily', [])[:2], ensure_ascii=False)}
- Golden hour: {json.dumps(gh, ensure_ascii=False)}

Сводка:"""
    text = _llm(prompt, max_tokens=600)
    return {
        "generated_at": datetime.now().isoformat(),
        "kind": "active_morning",
        "trip": trip,
        "day": day_num,
        "total_days": total,
        "intensity": intensity,
        "summary": text,
        "data": {"oura": oura_data, "weather": forecast, "golden_hour": gh},
    }


# ---------- Active evening ----------

def build_active_evening_prompt(trip: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    day_num = (today - _parse_date(trip["start_date"])).days + 1
    strava_data = strava.fetch_yesterday()  # today's activity is "today" from yesterday's window — but Strava end-of-day for active trip = today
    question = f"{trip['destination']} — вечер дня {day_num}.\n\nКак прошёл день? Что посмотрел? Что удивило?\n(Пара предложений — сохраню в дневник поездки.)"
    return {
        "generated_at": datetime.now().isoformat(),
        "kind": "active_evening_prompt",
        "trip": trip,
        "day": day_num,
        "question": question,
        "strava": strava_data,
    }


def save_evening_checkin(trip: dict[str, Any], user_text: str, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    # Append to daily-log under travel/YYYY/[trip-slug]/daily-log.md (mounted volume)
    year = today.year
    slug = trip.get("slug") or f"{trip['destination'].lower().replace(' ', '-')}-{trip['start_date']}"
    log_dir = Path(settings.travel_dir) / str(year) / slug
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "daily-log.md"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"\n## {today.isoformat()}\n\n{user_text}\n")
    return {"saved_to": str(log_file), "date": today.isoformat()}


# ---------- Post-trip ----------

def build_recap(trip: dict[str, Any]) -> dict[str, Any]:
    year = _parse_date(trip["start_date"]).year
    slug = trip.get("slug") or f"{trip['destination'].lower().replace(' ', '-')}-{trip['start_date']}"
    log_dir = Path(settings.travel_dir) / str(year) / slug
    log_file = log_dir / "daily-log.md"
    log_content = log_file.read_text(encoding="utf-8") if log_file.exists() else ""

    prompt = f"""Скомпилируй recap поездки в {trip['destination']} ({trip['start_date']} → {trip['end_date']}) из дневника.

Структура recap:
- Лучшие моменты (top 3-5)
- Что бы сделал иначе
- Рекомендации для следующего раза туда же

Без эмодзи, на русском, конкретно.

Daily log:
{log_content[:6000]}

Recap:"""
    text = _llm(prompt, max_tokens=900)

    recap_file = log_dir / "recap.md"
    recap_file.write_text(f"# Recap: {trip['destination']} ({trip['start_date']} — {trip['end_date']})\n\n{text}\n", encoding="utf-8")
    return {
        "generated_at": datetime.now().isoformat(),
        "kind": "recap",
        "trip": trip,
        "recap_file": str(recap_file),
        "summary": text,
    }
