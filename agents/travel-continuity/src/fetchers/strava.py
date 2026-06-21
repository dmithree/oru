"""Strava — yesterday's activity for trip log."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
import requests
from ..config import settings

logger = logging.getLogger(__name__)
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"


def _refresh() -> str | None:
    if not (settings.strava_client_id and settings.strava_client_secret and settings.strava_refresh_token):
        return None
    try:
        r = requests.post(STRAVA_TOKEN_URL, data={
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "refresh_token": settings.strava_refresh_token,
            "grant_type": "refresh_token",
        }, timeout=15)
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception:
        logger.exception("Strava token refresh failed")
        return None


def fetch_yesterday() -> dict[str, Any]:
    token = _refresh()
    if not token:
        return {}
    end_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=1)
    try:
        r = requests.get(
            f"{STRAVA_API}/athlete/activities",
            headers={"Authorization": f"Bearer {token}"},
            params={"after": int(start_dt.timestamp()), "before": int(end_dt.timestamp()), "per_page": 30},
            timeout=15,
        )
        r.raise_for_status()
        acts = r.json() or []
    except Exception:
        logger.exception("Strava fetch failed")
        return {}
    return {
        "count": len(acts),
        "total_km": round(sum(a.get("distance", 0) or 0 for a in acts) / 1000.0, 1),
        "names": [a.get("name") for a in acts],
    }
