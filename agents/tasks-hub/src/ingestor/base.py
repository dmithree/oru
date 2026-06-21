"""Adapter interface and shared ingestion logic.

An adapter reads its source (markdown files, Reminders snapshot, Linear
API, ...) and yields RawTask records. The runner then dedups them
against the store and decides: create / update / mark done.

Adapters are pure read — they never call store directly. The runner
does the writes (so dedup and event-emission live in one place).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol


@dataclass
class RawTask:
    """One task as it appears in a source.

    Adapters convert their native format into this shape; the runner
    normalizes further (canonical text-hash, deadlines, tags, effort,
    cog) before insertion.
    """

    text: str
    source: str                          # e.g., "markdown:everything/.../to-do.md:L42"
    status: str = "open"                 # source-side state ("open" or "done")
    ext_id: Optional[str] = None         # Reminders id, Linear id, ...
    owner_agent: Optional[str] = None
    project: Optional[str] = None
    category_hint: Optional[str] = None  # from frontmatter `category_hint`
    raw: dict[str, Any] = field(default_factory=dict)  # original payload

    # Adapter-provided metadata. The runner will run parsers.parse_metadata()
    # on `text` and merge results; explicit values here override the
    # parser (so an adapter that already knows the due-date from a
    # structured source can hand it in directly).
    overrides: dict[str, Any] = field(default_factory=dict)


class Adapter(Protocol):
    """Adapters are protocol-compatible — duck typing, no inheritance.

    name:   short identifier used in the migration report.
    read(): yields RawTasks for the current snapshot of the source.
    """

    name: str

    def read(self) -> Iterable[RawTask]: ...
