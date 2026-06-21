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
