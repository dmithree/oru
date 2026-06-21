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
