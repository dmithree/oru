"""Thoughts adapter — file-queue ingest for voice / Fireflies thoughts (idea 6).

Voice thoughts from yumiru (Supabase) and meeting/therapy transcripts
from Fireflies don't live in the tasks-hub container's network. They
live in personal-agent's Supabase tables / Fireflies API. Rather than
pull from those services directly (which would need three more sets of
credentials + token rotation), this adapter reads a *queue file*:

    state/thoughts-queue.jsonl

Each line is a candidate task:

    {
      "ext_id": "thought-<supabase-id>",
      "source_kind": "voice" | "meeting" | "therapy",
      "text": "позвонить врачу записаться на анализы",
      "due_at": "2026-06-24" (optional),
      "context_tags": ["@phone"] (optional),
      "owner_agent": "thoughts-capture" | "self-reflection" | "meetings",
      "raw": {...original payload...}
    }

The queue is populated by:
  - personal-agent's /thoughts skill after it processes a yumiru note
    and detects an actionable task (writes a line, then deletes the
    transcript per the canonical "two-base" rule)
  - personal-agent's /fireflies skill on action-item extraction
  - the self-reflection / thoughts-capture agents once they land

All thoughts arrive as `status=inbox` so they never auto-pollute the
brief — the user triages them via `POST /inbox/triage` or the
`/inbox` Hermes skill.

The queue file lives next to other state. Lines are *consumed*: after
ingestion the adapter rewrites the queue with the unprocessed tail
(matched ext_ids removed) so re-runs are idempotent. Atomic rewrite
via temp + os.replace.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .base import RawTask

logger = logging.getLogger(__name__)


@dataclass
class ThoughtsAdapter:
    name: str = "thoughts"
    queue_path: Path = field(default_factory=lambda: Path("/opt/state/thoughts-queue.jsonl"))
    consume: bool = True       # rewrite the queue file without consumed lines

    def read(self) -> Iterator[RawTask]:
        if not self.queue_path.exists():
            logger.info("thoughts adapter: queue file missing at %s", self.queue_path)
            return

        lines: list[str] = []
        try:
            lines = self.queue_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            logger.exception("thoughts adapter: queue read failed")
            return

        consumed_ext_ids: set[str] = set()

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("thoughts adapter: skipping malformed line %r", line[:80])
                continue

            text = (entry.get("text") or "").strip()
            if not text:
                continue

            ext_id = entry.get("ext_id") or f"thought-{hash(text) & 0xffffffff:x}"
            source_kind = entry.get("source_kind") or "voice"

            overrides: dict[str, Any] = {}
            if entry.get("due_at"):
                overrides["due_at"] = entry["due_at"]
                overrides["due_precision"] = "day"
            if entry.get("context_tags"):
                overrides["context_tags"] = list(entry["context_tags"])
            if entry.get("priority"):
                overrides["priority"] = entry["priority"]

            yield RawTask(
                text=text,
                source=f"thoughts:{source_kind}",
                status="inbox",
                ext_id=ext_id,
                owner_agent=entry.get("owner_agent"),
                raw=entry.get("raw") or entry,
                overrides=overrides,
            )
            consumed_ext_ids.add(ext_id)

        if self.consume and consumed_ext_ids:
            self._rewrite_tail(lines, consumed_ext_ids)

    def _rewrite_tail(self, lines: list[str], consumed: set[str]) -> None:
        """Drop consumed lines from the queue. Atomic via temp + replace."""
        keep: list[str] = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            try:
                entry = json.loads(s)
            except json.JSONDecodeError:
                # preserve unparseable lines so a future fix-up can recover them
                keep.append(s)
                continue
            ext_id = entry.get("ext_id") or f"thought-{hash((entry.get('text') or '')) & 0xffffffff:x}"
            if ext_id not in consumed:
                keep.append(s)

        try:
            self.queue_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8",
                dir=self.queue_path.parent,
                delete=False,
                suffix=".tmp",
            ) as tmp:
                for s in keep:
                    tmp.write(s + "\n")
                tmp_path = tmp.name
            os.replace(tmp_path, self.queue_path)
            logger.info("thoughts adapter: consumed %d lines, %d remain",
                        len(consumed), len(keep))
        except Exception:
            logger.exception("thoughts adapter: queue rewrite failed; queue may double-ingest")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = Path(os.environ.get("THOUGHTS_QUEUE", "/tmp/thoughts-queue.jsonl"))
    adapter = ThoughtsAdapter(queue_path=p, consume=False)
    n = 0
    for rt in adapter.read():
        n += 1
        print(rt.source, "::", rt.text[:60], "::", rt.overrides)
    print(f"total: {n}")
