"""Strava API — 7-day activity aggregation with real HR zones.

Pulls user's configured HR zones from /athlete/zones. If zones aren't configured,
falls back to a max-HR estimate computed from a 90-day activity scan.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ..config import settings

logger = logging.getLogger(__name__)

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"


def _refresh_access_token() -> str | None:
    if not (settings.strava_client_id and settings.strava_client_secret and settings.strava_refresh_token):
        return None
    try:
        r = requests.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
                "refresh_token": settings.strava_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception:
        logger.exception("Strava token refresh failed")
        return None


def _fetch_configured_zones(token: str) -> list[tuple[int, int]] | None:
    """Return [(lo, hi), ...] in BPM if user configured HR zones in Strava, else None."""
    try:
        r = requests.get(
            f"{STRAVA_API}/athlete/zones",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        hr = r.json().get("heart_rate") or {}
        zones = hr.get("zones") or []
        if len(zones) >= 5:
            result = []
            for z in zones:
                lo = z.get("min") or 0
                hi = z.get("max") or -1
                result.append((lo, hi if hi > 0 else 9999))
            return result
    except Exception:
        logger.exception("Strava /athlete/zones fetch failed")
    return None


def _fetch_activities(token: str, after: int, before: int) -> list[dict[str, Any]]:
    try:
        r = requests.get(
            f"{STRAVA_API}/athlete/activities",
            headers={"Authorization": f"Bearer {token}"},
            params={"after": after, "before": before, "per_page": 100},
            timeout=15,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception:
        logger.exception("Strava activities fetch failed (after=%s before=%s)", after, before)
        return []


def _max_hr_from_recent(token: str) -> int | None:
    """Scan ~90 days of activities, return observed max HR. None if no HR data anywhere."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=90)
    activities = _fetch_activities(token, int(start.timestamp()), int(end.timestamp()))
    observed = [a.get("max_heartrate") for a in activities if a.get("max_heartrate")]
    return int(max(observed)) if observed else None


def _build_pct_zones(max_hr: int) -> list[tuple[int, int]]:
    """Standard 5-zone split by %max_hr — fallback when user hasn't configured zones."""
    return [
        (0, int(max_hr * 0.60)),       # Z1 recovery
        (int(max_hr * 0.60), int(max_hr * 0.70)),  # Z2 endurance
        (int(max_hr * 0.70), int(max_hr * 0.80)),  # Z3 tempo
        (int(max_hr * 0.80), int(max_hr * 0.90)),  # Z4 threshold
        (int(max_hr * 0.90), 9999),                # Z5 vo2/anaerobic
    ]


def _zone_for_hr(hr: float | None, zones: list[tuple[int, int]] | None) -> str:
    if hr is None or not zones:
        return "unknown"
    for i, (lo, hi) in enumerate(zones, start=1):
        if lo <= hr < hi:
            return f"Z{i}"
    return f"Z{len(zones)}"


def fetch_week(end_date: datetime | None = None) -> dict[str, Any]:
    token = _refresh_access_token()
    if not token:
        return {}

    end_dt = end_date or datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=7)

    zones = _fetch_configured_zones(token)
    max_hr_used: int | None = None
    zones_source = "configured"
    if zones is None:
        max_hr_used = _max_hr_from_recent(token)
        if max_hr_used:
            zones = _build_pct_zones(max_hr_used)
            zones_source = f"derived_from_max_hr={max_hr_used}"
        else:
            zones_source = "unavailable"

    activities = _fetch_activities(token, int(start_dt.timestamp()), int(end_dt.timestamp()))

    if not activities:
        return {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "activity_count": 0,
            "total_distance_km": 0.0,
            "total_time_min": 0.0,
            "by_zone": {},
            "longest": None,
            "zones_source": zones_source,
            "max_hr_used": max_hr_used,
        }

    by_zone: dict[str, int] = {}
    total_distance = 0.0
    total_time = 0.0
    longest: dict[str, Any] | None = None
    longest_dist = 0.0

    for a in activities:
        dist_m = a.get("distance") or 0
        time_s = a.get("moving_time") or 0
        avg_hr = a.get("average_heartrate")
        total_distance += dist_m
        total_time += time_s
        bucket = _zone_for_hr(avg_hr, zones)
        by_zone[bucket] = by_zone.get(bucket, 0) + 1
        if dist_m > longest_dist:
            longest_dist = dist_m
            longest = {
                "name": a.get("name"),
                "type": a.get("sport_type") or a.get("type"),
                "distance_km": round(dist_m / 1000.0, 1),
                "duration_min": round(time_s / 60.0, 1),
                "avg_hr": avg_hr,
                "max_hr": a.get("max_heartrate"),
                "date": a.get("start_date_local"),
            }

    return {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "activity_count": len(activities),
        "total_distance_km": round(total_distance / 1000.0, 1),
        "total_time_min": round(total_time / 60.0, 1),
        "by_zone": by_zone,
        "longest": longest,
        "zones_source": zones_source,
        "max_hr_used": max_hr_used,
    }
