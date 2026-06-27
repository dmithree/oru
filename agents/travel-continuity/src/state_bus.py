"""personal-context.json shared bus — read/write travel section."""
from __future__ import annotations
import fcntl
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import settings

logger = logging.getLogger(__name__)


def _path() -> Path:
    return Path(settings.personal_context_file)


def read() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                return json.load(fh)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        logger.exception("personal-context read failed")
        return {}


def write_section(section: str, payload: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    current = read()
    current[section] = payload
    tmp = tempfile.NamedTemporaryFile(mode="w", dir=p.parent, delete=False, suffix=".tmp", encoding="utf-8")
    try:
        json.dump(current, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, p)
    except Exception:
        os.unlink(tmp.name)
        raise


def get_active_trip() -> dict[str, Any] | None:
    travel = read().get("travel", {})
    trip = travel.get("active_trip")
    return trip if isinstance(trip, dict) and trip else None


def set_active_trip(trip: dict[str, Any] | None) -> None:
    travel = read().get("travel", {})
    travel["active_trip"] = trip
    write_section("travel", travel)


# ---------- Distant-phase engagement tracker ----------
# Keyed by trip slug so a tracker survives across restarts and resets cleanly
# when the active trip changes. Shape:
#   travel["engagement"][slug] = {
#       "covered": {topic_id: {"asked_on": iso, "count": int}},
#       "last_asked_on": iso_date,
#       "last_topic": topic_id,
#       "decisions": [{"on": iso, "topic": id, "note": str}],
#       "last_weekly_on": iso_date,
#   }

def get_engagement(slug: str) -> dict[str, Any]:
    eng = read().get("travel", {}).get("engagement", {})
    rec = eng.get(slug)
    if not isinstance(rec, dict):
        rec = {"covered": {}, "last_asked_on": None, "last_topic": None,
               "decisions": [], "last_weekly_on": None}
    rec.setdefault("covered", {})
    rec.setdefault("decisions", [])
    rec.setdefault("last_asked_on", None)
    rec.setdefault("last_topic", None)
    rec.setdefault("last_weekly_on", None)
    return rec


def save_engagement(slug: str, rec: dict[str, Any]) -> None:
    travel = read().get("travel", {})
    eng = travel.get("engagement")
    if not isinstance(eng, dict):
        eng = {}
    eng[slug] = rec
    travel["engagement"] = eng
    write_section("travel", travel)


def mark_topic_asked(slug: str, topic_id: str, today_iso: str) -> dict[str, Any]:
    rec = get_engagement(slug)
    cov = rec["covered"].get(topic_id, {"asked_on": today_iso, "count": 0})
    cov["asked_on"] = today_iso
    cov["count"] = int(cov.get("count", 0)) + 1
    rec["covered"][topic_id] = cov
    rec["last_asked_on"] = today_iso
    rec["last_topic"] = topic_id
    save_engagement(slug, rec)
    return rec


def add_decision(slug: str, topic_id: str, note: str, today_iso: str) -> dict[str, Any]:
    rec = get_engagement(slug)
    rec["decisions"].append({"on": today_iso, "topic": topic_id, "note": note})
    save_engagement(slug, rec)
    return rec
