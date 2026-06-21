"""Golden / blue hour calculation via astral lib."""
from __future__ import annotations
import logging
from datetime import date, datetime
from typing import Any

from astral import LocationInfo
from astral.sun import sun, golden_hour, blue_hour, SunDirection

logger = logging.getLogger(__name__)


def times_for(latitude: float, longitude: float, timezone_str: str, target: date | None = None) -> dict[str, Any]:
    target = target or date.today()
    loc = LocationInfo(latitude=latitude, longitude=longitude, timezone=timezone_str)
    try:
        s = sun(loc.observer, date=target, tzinfo=loc.timezone)
        gh = golden_hour(loc.observer, date=target, direction=SunDirection.SETTING, tzinfo=loc.timezone)
        bh = blue_hour(loc.observer, date=target, direction=SunDirection.SETTING, tzinfo=loc.timezone)
        return {
            "date": target.isoformat(),
            "sunrise": s["sunrise"].strftime("%H:%M"),
            "sunset": s["sunset"].strftime("%H:%M"),
            "golden_hour_evening": [t.strftime("%H:%M") for t in gh],
            "blue_hour_evening": [t.strftime("%H:%M") for t in bh],
        }
    except Exception:
        logger.exception("Golden hour calc failed")
        return {}
