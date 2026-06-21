"""Weekly health digest: fetch -> classify state -> LLM-formatted summary.

State classifier — deterministic Python (no LLM). Then LLM generates a 3-5 line
human summary based on the structured signals. This keeps the cost predictable
and the state machine debuggable.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from litellm import completion

from .config import settings
from .fetchers import oura, strava

logger = logging.getLogger(__name__)


def classify_state(oura_data: dict[str, Any], strava_data: dict[str, Any]) -> str:
    """Map structured signals to state. Mirrors the classifier in bureau/health/spec.md."""
    readiness = oura_data.get("readiness_avg")
    hrv = oura_data.get("hrv_balance_avg")
    activity_count = strava_data.get("activity_count", 0)
    total_time_min = strava_data.get("total_time_min", 0.0)

    weekly_load_score = min(1.0, (total_time_min / 60.0) / 8.0) if total_time_min else 0.0

    if readiness is None and hrv is None:
        return "unknown"
    if (readiness is not None and readiness < 65) or (hrv is not None and hrv < 65):
        return "recovery_needed"
    if (readiness is not None and readiness > 80) and (hrv is not None and hrv > 80):
        return "peak" if weekly_load_score > 0.7 else "optimal"
    return "building"


def _summary_prompt(oura_data: dict[str, Any], strava_data: dict[str, Any], state: str) -> str:
    return f"""Ты помощник Димы по здоровью. Сгенерируй краткую сводку за неделю — 3-5 строк, по-русски.

Жёсткие правила:
- Без эмодзи
- Без приветствий, без trailing summary
- Без слов "просто", "по сути", "в общем"
- Цифры конкретные, если данные есть. Если данных нет — пропусти, не выдумывай.
- Первая строка: одна фраза с главным сигналом ("HRV восстановился", "нагрузка избыточна", и т.п.)
- Последующие — короткие bullet points с конкретикой
- Без советов "береги себя" — только наблюдения

State (определён классификатором): {state}

Данные Oura за неделю: {oura_data}
Данные Strava за неделю: {strava_data}

Сводка:"""


def _llm_summary(oura_data: dict[str, Any], strava_data: dict[str, Any], state: str) -> str:
    if not settings.anthropic_api_key:
        return "(LLM не настроен; данные получены)"
    try:
        resp = completion(
            model=f"anthropic/{settings.anthropic_model}",
            messages=[{"role": "user", "content": _summary_prompt(oura_data, strava_data, state)}],
            max_tokens=400,
            temperature=0.3,
            api_key=settings.anthropic_api_key,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        logger.exception("LLM summary failed")
        return "(LLM ошибка; см. логи)"


def build_digest() -> dict[str, Any]:
    """One pass: fetch -> classify -> LLM summary. Returns full digest dict."""
    oura_data = oura.fetch_week()
    strava_data = strava.fetch_week()
    state = classify_state(oura_data, strava_data)
    summary = _llm_summary(oura_data, strava_data, state)
    return {
        "generated_at": datetime.now().isoformat(),
        "state": state,
        "summary": summary,
        "oura": oura_data,
        "strava": strava_data,
    }
