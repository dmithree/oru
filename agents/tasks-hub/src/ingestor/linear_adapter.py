"""Linear adapter — placeholder for Phase 1.

Phase 1 ships a no-op adapter that returns zero tasks. Full Linear
integration (read open / in-progress issues, bidirectional sync,
hardcoded whitelist enforcement to mirror personal-agent's
LINEAR_ALLOWED_PATHS) lands in Phase 1.5 once we settle on:

  - Read direction: pull only stuck (in-progress > 5d) like daily-briefing,
    or pull all open issues filtered by team/project?
  - Write direction: when an ingested task transitions to done in the
    store, propagate to Linear? Only for tasks that originated there?

For now the adapter is wired so the runner can list it as "linear: 0"
without crashing, and the schema/store already accepts ext_id="ISS-NNN"
when we turn it on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator

from .base import RawTask

logger = logging.getLogger(__name__)


@dataclass
class LinearAdapter:
    name: str = "linear"

    def read(self) -> Iterator[RawTask]:
        logger.info("linear adapter: stub, 0 tasks (full impl in Phase 1.5)")
        return iter(())
