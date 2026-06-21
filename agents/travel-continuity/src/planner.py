"""Full trip plan generation — applies all MANDATORY rules from personal-agent travel skill:

- Three recommendations per place (obvious / non-obvious / very non-obvious)
- Walking-first routing with public transport fallback
- Golden hour scheduling for photogenic spots
- Sources-first (reads mounted everything/personal/travel/sources.md)
- Packing list from baseline + user_packing_standard rules
- Cultural notes
- Passport reminder (two foreign passports)
- HTML metadata frontmatter (title format, letter-monogram favicon) for /share deploy

No emoji anywhere in output (incl. favicon) — per workspace no-emoji rule.

Output: trip markdown ready to drop into posmotri/pages/.
"""
from __future__ import annotations
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from litellm import completion

from .config import settings
from . import preferences
from .fetchers import weather, golden_hour

logger = logging.getLogger(__name__)

SOURCES_FILE = "/opt/data/sources.md"
PACKING_BASELINE_FILE = "/opt/data/packing-baseline.md"
TRIP_TEMPLATE_FILE = "/opt/data/templates/trip-template.md"


def _read_file(path: str, max_chars: int = 6000) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")[:max_chars]
    except Exception:
        return ""


def _llm(prompt: str, max_tokens: int = 4000) -> str:
    if not settings.anthropic_api_key:
        return "(LLM не настроен)"
    try:
        resp = completion(
            model=f"anthropic/{settings.anthropic_model}",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.4,
            api_key=settings.anthropic_api_key,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        logger.exception("LLM call failed")
        return "(LLM ошибка)"


def _favicon_letter(destination: str) -> str:
    """First alphanumeric character of the destination, uppercased. No emoji."""
    for ch in destination:
        if ch.isalnum():
            return ch.upper()
    return "T"


def _date_range_short(start: date, end: date) -> str:
    if start.month == end.month and start.year == end.year:
        return f"{start.day:02d}-{end.day:02d}.{end.month:02d}"
    if start.year == end.year:
        return f"{start.strftime('%d.%m')}-{end.strftime('%d.%m')}"
    return f"{start.strftime('%d.%m.%y')}-{end.strftime('%d.%m.%y')}"


def _html_frontmatter(destination: str, start: date, end: date) -> str:
    title = f"{destination}, {_date_range_short(start, end)}"
    letter = _favicon_letter(destination)
    favicon_svg = (
        'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 '
        'viewBox=%220 0 100 100%22><rect width=%22100%22 height=%22100%22 rx=%2220%22 '
        'fill=%22%23111%22/><text x=%2250%22 y=%2272%22 font-size=%2260%22 '
        'text-anchor=%22middle%22 fill=%22white%22 '
        'font-family=%22-apple-system,Helvetica,Arial,sans-serif%22>'
        f"{letter}</text></svg>"
    )
    return (
        "---\n"
        f"title: {title}\n"
        f"favicon: \"{favicon_svg}\"\n"
        f"destination: {destination}\n"
        f"start_date: {start.isoformat()}\n"
        f"end_date: {end.isoformat()}\n"
        "---\n\n"
    )


def build_full_plan(
    destination: str,
    start_date_iso: str,
    end_date_iso: str,
    budget: str | None = None,
    pace: str | None = None,
    interests: list[str] | None = None,
    purpose: str | None = None,
    country_hint: str | None = None,
    must_see: list[str] | None = None,
) -> dict[str, Any]:
    """Generate a full structured trip plan markdown ready for posmotri/share."""
    start = date.fromisoformat(start_date_iso)
    end = date.fromisoformat(end_date_iso)
    days = (end - start).days + 1

    prefs = preferences.load_all()
    sources = _read_file(SOURCES_FILE, max_chars=8000)
    packing_baseline = _read_file(PACKING_BASELINE_FILE, max_chars=4000)
    trip_template = _read_file(TRIP_TEMPLATE_FILE, max_chars=3000)

    # Coords + weather for the trip dates
    coords = weather.lookup_coords(destination)
    lat = lon = None
    tz_name = "UTC"
    forecast: dict[str, Any] = {}
    golden_per_day: list[dict[str, Any]] = []
    days_until_start = (start - date.today()).days
    FORECAST_HORIZON_DAYS = 14  # Open-Meteo only forecasts ~16 days out
    if coords:
        lat, lon, tz_name = coords
        # Golden/blue hour is astronomical — computable for any future date.
        for i in range(days):
            d = start + timedelta(days=i)
            golden_per_day.append(golden_hour.times_for(lat, lon, tz_name, target=d))
        # Live forecast only covers the next ~2 weeks. For distant trips, fetching
        # it would return *today's* weather mislabeled as the trip's — skip it and
        # let the model fall back to seasonal climate knowledge.
        if -days <= days_until_start <= FORECAST_HORIZON_DAYS:
            fc_days = min((end - date.today()).days + 1, 16)
            if fc_days > 0:
                forecast = weather.forecast(lat, lon, tz_name, days=fc_days)

    if forecast.get("daily"):
        weather_note = "Ниже в данных есть live-прогноз на даты поездки — опирайся на него."
    else:
        weather_note = (
            f"Live-прогноза нет (старт через {days_until_start} дн, вне {FORECAST_HORIZON_DAYS}-дневного окна). "
            f"Дай типичные сезонные климатические ожидания для {destination} на это время года из своих знаний: "
            "средние дневные/ночные температуры, вероятность осадков, во что одеваться."
        )

    prompt = f"""Ты Oru, личный travel-планировщик Димы. Сделай ПОЛНЫЙ план поездки в {destination} с {start_date_iso} по {end_date_iso} ({days} дней).

Жёсткие правила (нарушение = провал):

1. БЕЗ ЭМОДЗИ ВООБЩЕ — нигде: ни в тексте, ни в заголовках, ни флагов стран. Ноль эмодзи.
2. Русский язык (если destination английский — топонимы сохраняй на английском, остальной текст — русский).
3. ТРИ РЕКОМЕНДАЦИИ ПО КАЖДОМУ МЕСТУ. Для любой названной точки маршрута давай три уровня:
   - Obvious — headline activity для туристов
   - Non-obvious — менее известный угол: второстепенный экспонат, боковой вход, соседнее место, специфическое время дня
   - Very non-obvious — глубокий уровень: behind-the-scenes, нишевый viewpoint, конкретная скамейка/окно/блюдо, ритуал местных
4. PEDESTRIAN-FIRST. Маршруты по умолчанию пешком, группировка точек географически. Транспорт только когда расстояние/расписание не позволяют пешком — указывай конкретный сегмент (например, "DLR Cutty Sark → Bank, 12 мин"). Избегай такси/Uber.
5. GOLDEN HOUR для фото. Photogenic точки (viewpoints, skyline, набережные, lit architecture) ВНУТРИ golden hour (час до заката / час после рассвета) или blue hour (~30 мин после заката для городских скайлайнов с подсветкой). Указывай точное время по каждому дню (есть данные ниже).
6. SOURCES FIRST. Если есть подходящие источники в sources.md — цитируй их в рекомендациях.
7. ПАСПОРТА. У Димы два загранпаспорта. В чек-листе явно: "проверить, в каком паспорте действующая виза для {destination}; взять оба".
8. PACKING. Стартуй с packing-baseline (cosmetics, electronics, base clothing, rain gear), потом добавляй trip-specific. Из user_packing_standard правила: шампунь/гель маленькие тюбики если {days}<=4, большие если >4. Обязательно: крем рук/лица, жидкость для умывания, дождевик.
9. MUJI. Найди ближайший крупный/флагман Muji в {destination} (из своего знания). Интересы: одежда (Muji Labo), travel/storage items.
10. Матча и японские пекарни — обязательно секция с локальными опциями (Blank Street если есть в городе, иначе local).
11. БЕЗ воды, без приветствий, без trailing "хорошей поездки!" в конце.

Структура плана (используй markdown с ## заголовками):

## Обзор
{days} дней в {destination}, цель: {purpose or 'отдых/изучение'}. Темп: {pace or 'moderate'}. Бюджет: {budget or 'mid-range'}. Интересы: {', '.join(interests) if interests else 'general'}.
{f"Must-see: {', '.join(must_see)}" if must_see else ""}

## Документы и виза
Проверка визы для российского паспорта для {destination}, какой из двух паспортов везти.

## Погода
{weather_note}

## Golden / blue hour
Таблица по дням (date / sunrise / sunset / golden hour evening / blue hour).

## Day-by-Day Itinerary
По каждому дню: 3-5 точек, география группированно, walking distance per day, реалистичный тайминг, для каждого места — три-уровневые рекомендации.

## Packing list
Из baseline + travel-specific.

## Pre-trip checklist (по убыванию срочности)
2 недели / 1 неделя / 3 дня / накануне.

## Что НЕ пропустить
Топ-5 must-do для этого направления (с three-tier когда применимо).

## Cultural do's and don'ts
Краткая секция этикета.

## Muji в {destination}
Конкретный магазин(ы) с адресом и зачем именно туда.

## Матча / пекарни / кофе
Локальные варианты.

## Бюджет (грубо)
Accommodation / food / activities / transport / misc.

---

ДАННЫЕ:

user preferences (memory files):
{json.dumps({k: v[:1500] for k, v in prefs.items()}, ensure_ascii=False, indent=2)}

Weather forecast for trip dates:
{json.dumps(forecast, ensure_ascii=False, indent=2)}

Golden hour by day:
{json.dumps(golden_per_day, ensure_ascii=False, indent=2)}

Sources (curated):
{sources}

Packing baseline:
{packing_baseline}

ВЫВОДИ ТОЛЬКО МАРКДАУН ПЛАНА (без вводных, без "Вот ваш план"):
"""
    body = _llm(prompt, max_tokens=6000)

    frontmatter = _html_frontmatter(destination, start, end)
    full_markdown = frontmatter + body

    return {
        "generated_at": datetime.now().isoformat(),
        "destination": destination,
        "start_date": start_date_iso,
        "end_date": end_date_iso,
        "days": days,
        "country_hint": country_hint,
        "forecast_available": bool(forecast.get("daily")),
        "weather": forecast,
        "golden_hour": golden_per_day,
        "markdown": full_markdown,
    }


def build_three_recs(place: str, city: str | None = None) -> dict[str, Any]:
    """Three-tier recommendations for a single named place."""
    sources = _read_file(SOURCES_FILE, max_chars=6000)
    prompt = f"""Ты Oru. Дай ровно ТРИ уровня рекомендаций для места: "{place}"{f' в {city}' if city else ''}.

Жёсткие правила:
- БЕЗ эмодзи
- Русский
- Без воды и trailing советов

Структура (ровно три bullets):

- Obvious: headline activity которую делают все туристы
- Non-obvious: менее известный угол — второстепенный экспонат, боковой вход, соседняя точка, конкретное время дня
- Very non-obvious: deep-cut — behind-the-scenes, нишевый viewpoint, конкретная скамейка/окно/блюдо, ритуал местных

Если есть упоминание в sources.md — процитируй коротко.

Sources:
{sources}

Рекомендации:"""
    text = _llm(prompt, max_tokens=600)
    return {
        "generated_at": datetime.now().isoformat(),
        "place": place,
        "city": city,
        "recommendations": text,
    }
