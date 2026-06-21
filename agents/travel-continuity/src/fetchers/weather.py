"""Open-Meteo weather forecast — free, no key."""
from __future__ import annotations
import logging
from datetime import date, timedelta
from typing import Any

import requests

logger = logging.getLogger(__name__)


def forecast(latitude: float, longitude: float, timezone_str: str, days: int = 7) -> dict[str, Any]:
    end = (date.today() + timedelta(days=days)).isoformat()
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "timezone": timezone_str,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode,windspeed_10m_max",
                "current_weather": "true",
                "forecast_days": days,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        days_out = []
        for i, d in enumerate(dates):
            days_out.append({
                "date": d,
                "t_max": daily.get("temperature_2m_max", [None])[i],
                "t_min": daily.get("temperature_2m_min", [None])[i],
                "precip_mm": daily.get("precipitation_sum", [None])[i],
                "wind_max": daily.get("windspeed_10m_max", [None])[i],
                "weathercode": daily.get("weathercode", [None])[i],
            })
        return {"current": data.get("current_weather"), "daily": days_out}
    except Exception:
        logger.exception("Weather fetch failed")
        return {}


def _candidate_queries(destination: str) -> list[str]:
    """Geocoding candidates, most specific first.

    A multi-city string ('Phuket Hong Kong China') or 'City, Country' won't
    resolve as one name, so we also try the first separator-delimited chunk and
    progressively shorter prefixes — landing on the primary city.
    """
    seen: list[str] = []

    def add(q: str) -> None:
        q = q.strip(" ,/-—–·;|")
        if q and q.lower() not in {s.lower() for s in seen}:
            seen.append(q)

    add(destination)
    for sep in (",", "/", ";", "|", "·", "—", "–", " - "):
        if sep in destination:
            add(destination.split(sep)[0])
    # Trailing-token drop: "Phuket Hong Kong China" -> "Phuket Hong Kong" -> ... -> "Phuket"
    tokens = destination.replace(",", " ").split()
    for n in range(len(tokens) - 1, 0, -1):
        add(" ".join(tokens[:n]))
    return seen


def _geocode_one(name: str) -> tuple[float, float, str] | None:
    try:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 1, "language": "en", "format": "json"},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if results:
            res = results[0]
            return res["latitude"], res["longitude"], res.get("timezone", "UTC")
    except Exception:
        logger.exception("Geocoding failed for %s", name)
    return None


def lookup_coords(destination: str) -> tuple[float, float, str] | None:
    """Resolve a destination -> (lat, lon, timezone) via Open-Meteo geocoding.

    Tries the full string, then separator-split and shorter prefixes, so
    multi-city trips and 'City, Country' strings resolve to their primary city.
    """
    for candidate in _candidate_queries(destination):
        coords = _geocode_one(candidate)
        if coords:
            if candidate != destination:
                logger.info("Geocoded %r via fallback candidate %r", destination, candidate)
            return coords
    return None
