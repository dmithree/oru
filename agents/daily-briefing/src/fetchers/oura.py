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


def fetch_today() -> dict[str, Any]:
    """Returns metrics for today or yesterday (whichever has data)."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    for candidate in (today, yesterday):
        readiness = _fetch_one("daily_readiness", candidate)
        if not readiness:
            continue
        sleep = _fetch_one("daily_sleep", candidate)
        result = {
            "date": candidate,
            "readiness_score": readiness.get("score"),
        }
        contributors = readiness.get("contributors") or {}
        result["hrv_balance"] = contributors.get("hrv_balance")
        result["recovery_index"] = contributors.get("recovery_index")
        result["resting_hr"] = contributors.get("resting_heart_rate")

        if sleep:
            result["sleep_score"] = sleep.get("score")
            sc = sleep.get("contributors") or {}
            result["total_sleep_hours"] = round((sc.get("total_sleep") or 0) / 60.0, 1) if sc.get("total_sleep") else None
            result["deep_sleep"] = sc.get("deep_sleep")
            result["rem_sleep"] = sc.get("rem_sleep")
            result["efficiency"] = sc.get("efficiency")
        return result
    return {}
