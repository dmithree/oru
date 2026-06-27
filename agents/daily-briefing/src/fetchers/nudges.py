"""Cross-domain nudge aggregator.

Every domain agent that wants a voice in the morning brief exposes ONE
proactive question/offer per day behind a uniform contract. The brief polls
them, ranks by urgency, and weaves them in — turning the brief from a passive
dashboard into "what to move today" across fronts.

Uniform contract (per domain):
  POST <base>/<endpoint>?notify=false&mark=true
    -> {"ok": true, "result": {topic, topic_label, question, urgency?, ...}}

Domains register below. Fails soft per-domain: an unreachable agent is simply
skipped, the brief never blocks or blanks.
"""
from __future__ import annotations
import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_TIMEOUT = float(os.environ.get("NUDGE_TIMEOUT", "45"))

# domain -> (base_url, endpoint, default_urgency_when_unscored)
DOMAINS: dict[str, tuple[str, str, int]] = {
    "health": (
        os.environ.get("HEALTH_URL", "http://oru-health:8001"),
        "/daily-nudge",
        50,
    ),
    "travel": (
        os.environ.get("TRAVEL_URL", "http://oru-travel-continuity:8003"),
        "/run-distant",
        45,
    ),
}


def _post(base: str, endpoint: str) -> dict[str, Any] | None:
    url = f"{base}{endpoint}?notify=false&mark=true"
    try:
        req = Request(url, method="POST", data=b"")
        with urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        logger.warning("nudge %s%s unreachable: %s", base, endpoint, exc)
        return None
    except Exception:
        logger.exception("nudge %s%s failed", base, endpoint)
        return None


def fetch_all_nudges() -> list[dict[str, Any]]:
    """Poll every registered domain, return ranked list (highest urgency first).
    Each item: {domain, topic, topic_label, question, urgency, extra}."""
    out: list[dict[str, Any]] = []
    for domain, (base, endpoint, default_urg) in DOMAINS.items():
        payload = _post(base, endpoint)
        if not payload or not payload.get("ok"):
            continue
        result = payload.get("result") or {}
        question = (result.get("question") or "").strip()
        if not question:
            continue
        urgency = result.get("urgency")
        if not isinstance(urgency, (int, float)):
            urgency = default_urg
        out.append({
            "domain": domain,
            "topic": result.get("topic"),
            "topic_label": result.get("topic_label"),
            "question": question,
            "urgency": urgency,
            # domain-specific extras the prompt may use for a header line
            "days_until": result.get("days_until"),
            "destination": (result.get("trip") or {}).get("destination"),
        })
    out.sort(key=lambda x: -x["urgency"])
    return out
