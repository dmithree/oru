"""Daily health nudge — proactive, data-driven, anti-repeat.

The weekly digest is a passive status readout. The nudge is the active layer:
ONE proactive question/offer per day for the morning brief, picked by which
health dimension is most *off* right now AND hasn't been nagged about recently.

Mirrors the travel distant-engagement engine, but urgency here is dynamic
(derived from Oura/Strava deviation) rather than time-gated. A decisions log
lets Дима close a topic ("late calls are a fixed constraint") so the engine
stops re-surfacing it.

Contract: build_health_nudge() -> {topic, topic_label, urgency, question}
exposed via /daily-nudge so the brief speaks one protocol to every domain.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from litellm import completion

from .config import settings
from .analyzer import classify_state
from .fetchers import oura, strava

logger = logging.getLogger(__name__)

NUDGE_STATE_FILE = Path(settings.state_file).parent / "health-nudge.json"
# Don't re-ask the same topic within this many days unless nothing else is urgent.
_RECENT_DAYS = 4
# Topics below this urgency are not worth a nudge — brief just shows the metric line.
_URGENCY_FLOOR = 30


# ---------- urgency functions (0-100, data-driven) ----------

def _u_recovery(oura: dict, strava: dict, state: str) -> tuple[int, str]:
    r = oura.get("readiness_avg")
    hrv = oura.get("hrv_balance_avg")
    if state == "recovery_needed":
        return 90, "readiness/HRV просели — тело просит восстановления"
    if r is not None and r < 70:
        return 55, f"readiness {r} — ниже комфортного"
    if hrv is not None and hrv < 70:
        return 50, f"HRV balance {hrv} — снижен"
    return 0, ""


def _u_sleep(oura: dict, strava: dict, state: str) -> tuple[int, str]:
    s = oura.get("sleep_avg")
    if s is None:
        return 0, ""
    if s < 65:
        return 75, f"сон {s} за неделю — стабильно низко"
    if s < 75:
        return 45, f"сон {s} — есть куда расти"
    return 0, ""


def _u_load(oura: dict, strava: dict, state: str) -> tuple[int, str]:
    cnt = strava.get("activity_count", 0)
    mins = strava.get("total_time_min", 0.0)
    if state == "peak" and mins and mins > 360:
        return 60, f"нагрузка высокая ({int(mins)} мин/нед) на фоне peak — риск перетрена"
    if cnt == 0:
        return 50, "за неделю ноль активностей"
    if cnt and mins and (mins / max(cnt, 1)) < 25:
        return 35, "тренировки короткие — мало объёма"
    return 0, ""


def _u_zone_balance(oura: dict, strava: dict, state: str) -> tuple[int, str]:
    by_zone = strava.get("by_zone") or {}
    if not by_zone:
        return 0, ""
    total = sum(by_zone.values())
    z2 = by_zone.get("Z2", 0)
    hard = by_zone.get("Z4", 0) + by_zone.get("Z5", 0)
    if total >= 3 and z2 / total < 0.3 and hard / total > 0.4:
        return 40, "перекос в Z4/Z5, мало Z2 — аэробная база недозагружена"
    return 0, ""


HEALTH_TOPICS: list[dict[str, Any]] = [
    {"id": "recovery", "label": "Восстановление", "fn": _u_recovery},
    {"id": "sleep", "label": "Сон", "fn": _u_sleep},
    {"id": "load", "label": "Нагрузка", "fn": _u_load},
    {"id": "zone_balance", "label": "Баланс зон", "fn": _u_zone_balance},
]


# ---------- state tracker (anti-repeat + decisions) ----------

def _load_state() -> dict[str, Any]:
    if NUDGE_STATE_FILE.exists():
        try:
            d = json.loads(NUDGE_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            d = {}
    else:
        d = {}
    d.setdefault("covered", {})       # topic_id -> {asked_on, count}
    d.setdefault("decisions", [])      # [{on, topic, note}]
    d.setdefault("last_topic", None)
    d.setdefault("last_asked_on", None)
    return d


def _save_state(d: dict[str, Any]) -> None:
    NUDGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    NUDGE_STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_asked(topic_id: str, today_iso: str) -> None:
    d = _load_state()
    cov = d["covered"].get(topic_id, {"asked_on": today_iso, "count": 0})
    cov["asked_on"] = today_iso
    cov["count"] = int(cov.get("count", 0)) + 1
    d["covered"][topic_id] = cov
    d["last_topic"] = topic_id
    d["last_asked_on"] = today_iso
    _save_state(d)


def add_decision(topic_id: str, note: str, today_iso: str) -> dict[str, Any]:
    d = _load_state()
    d["decisions"].append({"on": today_iso, "topic": topic_id, "note": note})
    _save_state(d)
    return d


def get_state() -> dict[str, Any]:
    return _load_state()


# ---------- picker + builder ----------

def _days_since(asked_on: str | None, today: date) -> int:
    if not asked_on:
        return 9999
    try:
        return (today - date.fromisoformat(asked_on)).days
    except Exception:
        return 9999


def _pick(oura: dict, strava: dict, state: str, today: date) -> dict[str, Any] | None:
    st = _load_state()
    decided = {d["topic"] for d in st.get("decisions", [])}
    last_topic = st.get("last_topic")
    covered = st.get("covered", {})

    scored = []
    for t in HEALTH_TOPICS:
        if t["id"] in decided:
            continue
        urg, signal = t["fn"](oura, strava, state)
        if urg < _URGENCY_FLOOR:
            continue
        recent = _days_since((covered.get(t["id"]) or {}).get("asked_on"), today) < _RECENT_DAYS
        scored.append({"topic": t, "urgency": urg, "signal": signal, "recent": recent})

    if not scored:
        return None
    # prefer not-recently-asked, then highest urgency, then not yesterday's topic
    scored.sort(key=lambda x: (x["recent"], -x["urgency"], x["topic"]["id"] == last_topic))
    return scored[0]


def _llm(prompt: str, max_tokens: int = 500) -> str:
    if not settings.anthropic_api_key:
        return ""
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
        logger.exception("health nudge LLM failed")
        return ""


def build_health_nudge(today: date | None = None) -> dict[str, Any]:
    """ONE proactive health question/offer for the brief, or empty if nothing's off."""
    today = today or date.today()
    oura_data = oura.fetch_week()
    strava_data = strava.fetch_week()
    state = classify_state(oura_data, strava_data)
    pick = _pick(oura_data, strava_data, state, today)

    if pick is None:
        return {
            "generated_at": datetime.now().isoformat(),
            "kind": "health_nudge",
            "topic": None,
            "urgency": 0,
            "question": "",
        }

    topic = pick["topic"]
    st = _load_state()
    decided = "; ".join(f"{d['topic']}: {d['note']}" for d in st.get("decisions", [])) or "нет"

    prompt = f"""Ты Oru — личный ассистент Димы по здоровью и форме. Сформируй ОДИН короткий проактивный вопрос-предложение для утреннего брифа.

Тема: {topic['label']} (id={topic['id']}).
Сигнал из данных: {pick['signal']}.
Общее состояние недели (классификатор): {state}.

Задача: не пассивная констатация метрики — ты ведёшь Диму к действию и ПРЕДЛАГАЕШЬ конкретный шаг или эксперимент на основе сигнала. Опирайся на цифры ниже.

Жёсткие правила:
- БЕЗ эмодзи
- 2-3 предложения, плотно, по делу
- На русском, на "ты", неформально
- Конкретика из данных (цифры), не общие слова "следи за сном"
- Заверши конкретным предложением ("сдвинуть отбой на 30 мин на 3 дня?", "сделать завтра только Z2?", "глянуть, что сбивает глубокий сон?")
- НЕ давай медицинских диагнозов, не пиши "обратись к врачу"
- Учти уже принятые Димой решения (не переспрашивай): {decided}

Данные Oura (неделя): {json.dumps(oura_data, ensure_ascii=False)}
Данные Strava (неделя): {json.dumps(strava_data, ensure_ascii=False)}

Вопрос-предложение:"""
    question = _llm(prompt, max_tokens=500)

    return {
        "generated_at": datetime.now().isoformat(),
        "kind": "health_nudge",
        "topic": topic["id"],
        "topic_label": topic["label"],
        "urgency": pick["urgency"],
        "signal": pick["signal"],
        "state": state,
        "question": question,
    }
