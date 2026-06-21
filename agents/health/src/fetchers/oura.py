"""Oura Ring API v2 — 7-day window aggregation.

Returns metrics suitable for weekly state classification:
  sleep_avg, readiness_avg, hrv_avg, recovery_avg, days_covered.

Source token: settings.oura_access_token (Personal Access Token from cloud.ouraring.com).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from statistics import mean
from typing import Any

import requests

from ..config import settings

logger = logging.getLogger(__name__)

OURA_BASE = "https://api.ouraring.com/v2/usercollection"


def _fetch_range(endpoint: str, start: str, end: str) -> list[dict[str, Any]]:
    if not settings.oura_access_token:
        return []
    try:
        r = requests.get(
            f"{OURA_BASE}/{endpoint}",
            headers={"Authorization": f"Bearer {settings.oura_access_token}"},
            params={"start_date": start, "end_date": end},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data", []) or []
    except Exception:
        logger.exception("Oura fetch failed: %s %s..%s", endpoint, start, end)
        return []


def _safe_avg(values: list[float | int | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(mean(clean), 1) if clean else None


def fetch_week(end_date: date | None = None) -> dict[str, Any]:
    """7-day window ending at end_date (default today). Empty dict on failure."""
    end_date = end_date or date.today()
    start_date = end_date - timedelta(days=6)
    start, end = start_date.isoformat(), end_date.isoformat()

    readiness_items = _fetch_range("daily_readiness", start, end)
    sleep_items = _fetch_range("daily_sleep", start, end)

    if not readiness_items and not sleep_items:
        return {}

    readiness_scores = [it.get("score") for it in readiness_items]
    hrv_balance = [(it.get("contributors") or {}).get("hrv_balance") for it in readiness_items]
    recovery = [(it.get("contributors") or {}).get("recovery_index") for it in readiness_items]
    sleep_scores = [it.get("score") for it in sleep_items]

    return {
        "start": start,
        "end": end,
        "days_covered": max(len(readiness_items), len(sleep_items)),
        "readiness_avg": _safe_avg(readiness_scores),
        "sleep_avg": _safe_avg(sleep_scores),
        "hrv_balance_avg": _safe_avg(hrv_balance),
        "recovery_index_avg": _safe_avg(recovery),
    }
