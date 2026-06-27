"""Travel engagement fetcher — pulls the daily proactive question from
travel-continuity (/run-distant) so the morning brief can weave it in.

Replaces the old passive "поездка ... уже в расписании" line: instead of
dumping raw trip data into the prompt, we ask travel-continuity for ONE
proactive question/offer (rotating topic, urgency-gated) and inject that.

Container-to-container over the default compose network, same pattern as the
tasks-hub fetcher. Fails soft: if travel-continuity is down or there's no
active trip, returns None and the brief simply omits the travel line.
"""
from __future__ import annotations
import json
import logging
import os
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

TRAVEL_URL = os.environ.get("TRAVEL_URL", "http://oru-travel-continuity:8003")
_TRAVEL_TIMEOUT = float(os.environ.get("TRAVEL_TIMEOUT", "45"))


def fetch_daily_engagement() -> Optional[dict[str, Any]]:
    """POST /run-distant?notify=false&mark=true — returns the engagement dict
    {topic, topic_label, days_until, question} or None.

    mark=true advances the rotation tracker exactly once per brief, so the
    brief is the single source that consumes a daily slot."""
    url = f"{TRAVEL_URL}/run-distant?notify=false&mark=true"
    try:
        req = Request(url, method="POST", data=b"")
        with urlopen(req, timeout=_TRAVEL_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        logger.warning("travel-continuity /run-distant unreachable: %s", exc)
        return None
    except Exception:
        logger.exception("travel-continuity /run-distant failed")
        return None

    if not payload.get("ok"):
        return None
    result = payload.get("result") or {}
    question = (result.get("question") or "").strip()
    if not question:
        return None
    return {
        "topic": result.get("topic"),
        "topic_label": result.get("topic_label"),
        "days_until": result.get("days_until"),
        "destination": (result.get("trip") or {}).get("destination"),
        "question": question,
    }
