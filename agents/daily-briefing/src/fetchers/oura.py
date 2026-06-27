"""Oura — single day fetch (most recent available)."""
from __future__ import annotations
import logging
from datetime import date, timedelta
from typing import Any

import requests
from ..config import settings

logger = logging.getLogger(__name__)
OURA_BASE = "https://api.ouraring.com/v2/usercollection"


def _fetch_one(endpoint: str, date_str: str) -> dict[str, Any] | None:
    if not settings.oura_access_token:
        return None
    try:
        r = requests.get(
            f"{OURA_BASE}/{endpoint}",
            headers={"Authorization": f"Bearer {settings.oura_access_token}"},
            params={"start_date": date_str, "end_date": date_str},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("data", [])
        return items[-1] if items else None
    except Exception:
        logger.exception("Oura fetch failed: %s %s", endpoint, date_str)
        return None


def _fetch_sleep_session(candidate: str) -> dict[str, Any] | None:
    """Real sleep durations live in the `sleep` endpoint (seconds), NOT in
    daily_sleep.contributors — those are 0-100 quality scores, not minutes.

    The main sleep is dated to the day it ENDS but the `sleep` endpoint
    filters by the record's start date, so a night that begins before
    midnight is missed by a same-day query. Widen the window by ±1 day and
    pick the `long_sleep` session whose `day` matches the candidate."""
    if not settings.oura_access_token:
        return None
    try:
        d = date.fromisoformat(candidate)
        r = requests.get(
            f"{OURA_BASE}/sleep",
            headers={"Authorization": f"Bearer {settings.oura_access_token}"},
            params={
                "start_date": (d - timedelta(days=1)).isoformat(),
                "end_date": (d + timedelta(days=1)).isoformat(),
            },
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("data", [])
    except Exception:
        logger.exception("Oura sleep session fetch failed: %s", candidate)
        return None
    if not items:
        return None
    longs = [s for s in items if s.get("type") == "long_sleep"]
    for s in longs:
        if s.get("day") == candidate:
            return s
    return longs[-1] if longs else items[-1]


def _hours(seconds: Any) -> float | None:
    return round(seconds / 3600.0, 1) if seconds else None


def fetch_today() -> dict[str, Any]:
    """Returns metrics for today or yesterday (whichever has data)."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    for candidate in (today, yesterday):
        readiness = _fetch_one("daily_readiness", candidate)
        if not readiness:
            continue
        result = {
            "date": candidate,
            "readiness_score": readiness.get("score"),
        }
        contributors = readiness.get("contributors") or {}
        result["hrv_balance"] = contributors.get("hrv_balance")
        result["recovery_index"] = contributors.get("recovery_index")
        result["resting_hr"] = contributors.get("resting_heart_rate")

        sleep = _fetch_one("daily_sleep", candidate)
        if sleep:
            result["sleep_score"] = sleep.get("score")

        # Real durations come from the sleep session, not the daily_sleep
        # contributor scores (which are 0-100, not minutes).
        session = _fetch_sleep_session(candidate)
        if session:
            result["total_sleep_hours"] = _hours(session.get("total_sleep_duration"))
            result["deep_sleep_hours"] = _hours(session.get("deep_sleep_duration"))
            result["rem_sleep_hours"] = _hours(session.get("rem_sleep_duration"))
            result["light_sleep_hours"] = _hours(session.get("light_sleep_duration"))
            result["efficiency"] = session.get("efficiency")
        return result
    return {}
