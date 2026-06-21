"""Load user travel preferences from mounted .claude/memory/ files."""
from __future__ import annotations
import logging
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

PREF_FILES = {
    "packing_standard": "user_packing_standard.md",
    "muji_interest": "user_muji_travel_interest.md",
    "walking": "user_travel_walking.md",
    "passports": "user_travel_passports.md",
}


def load_all() -> dict[str, str]:
    base = Path(settings.memory_dir)
    out: dict[str, str] = {}
    if not base.exists():
        logger.warning("Memory dir not found: %s", base)
        return out
    for key, fname in PREF_FILES.items():
        p = base / fname
        if p.exists():
            try:
                out[key] = p.read_text(encoding="utf-8")
            except Exception:
                logger.exception("read pref failed: %s", p)
    return out
