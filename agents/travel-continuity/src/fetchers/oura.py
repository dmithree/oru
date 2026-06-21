"""Oura — today's readiness (for trip morning adjust)."""
from __future__ import annotations
import logging
from datetime import date, timedelta
from typing import Any
import requests
from ..config import settings

logger = logging.getLogger(__name__)
OURA_BASE = "https://api.ouraring.com/v2/usercollection"


def fetch_today() -> dict[str, Any]:
    if not settings.oura_access_token:
        return {}
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    headers = {"Authorization": f"Bearer {settings.oura_access_token}"}

    for d in (today, yesterday):
        try:
            r = requests.get(
                f"{OURA_BASE}/daily_readiness",
                headers=headers,
                params={"start_date": d, "end_date": d},
                timeout=10,
            )
            r.raise_for_status()
            items = r.json().get("data", [])
            if items:
                rd = items[-1]
                sleep = {}
                try:
                    s = requests.get(
                        f"{OURA_BASE}/daily_sleep",
                        headers=headers,
                        params={"start_date": d, "end_date": d},
                        timeout=10,
                    ).json().get("data", [])
                    if s:
                        sleep = s[-1].get("contributors") or {}
                except Exception:
                    pass
                return {
                    "date": d,
                    "readiness": rd.get("score"),
                    "hrv_balance": (rd.get("contributors") or {}).get("hrv_balance"),
                    "total_sleep_hours": round((sleep.get("total_sleep") or 0) / 60.0, 1) if sleep.get("total_sleep") else None,
                }
        except Exception:
            logger.exception("Oura fetch failed for %s", d)
    return {}
