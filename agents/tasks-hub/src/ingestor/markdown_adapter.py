"""Markdown adapter — port of personal-agent scripts/tasks_loader.py.

Reads `- [ ]` and numbered tasks from a known list of markdown files,
respecting YAML frontmatter that controls inclusion. The frontmatter
schema is preserved from the original so legacy files keep working
during cutover:

    ---
    briefing: true              # include in morning brief
    tasks_index: true           # include in everything/all-tasks.md
    parse: both                 # "checkbox" | "numbered" | "both"
    sections_include: []
    sections_exclude:
      - Ideas Backlog
      - Blockers
    top_n: null
    category_hint: "Work"
    ---

Differences vs tasks_loader.py:
  - Returns RawTask records (not the legacy Task dataclass) so the
    runner can enrich/dedup uniformly across adapters.
  - Done tasks (`[x]`) are emitted with `status="done"` instead of
    being dropped, so the migration log retains the recent history.
  - File paths are stored relative to a configurable repo root so the
    same source field works from inside and outside the container.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .base import RawTask

logger = logging.getLogger(__name__)


# --- Constants from the legacy loader -------------------------------

GLOBAL_SECTION_BLACKLIST = {
    "ideas backlog", "blockers", "archive", "архив",
    "completed", "завершено", "done",
    "связанные файлы", "источники", "history", "история изменений",
}

DEFAULT_TASK_FILES: list[str] = [
    "everything/personal/notes/all-tasks-simple.md",
    "everything/personal/projects/to-do.md",
    "everything/personal/projects/curio/BACKLOG.md",
    "everything/personal/projects/recipo/docs/backlog.md",
    "pfm/continuity/open-actions.md",
    "everything/personal/publicity/channels/telegram-cursor-assistant/TODO.md",
]

_DONE_RE = re.compile(r"\[\s*[xX]\s*\]")
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[\s*([ xX])\s*\]\s*(.*)$")
_NUMBERED_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_FM_FENCE = "---"

FALLBACK_DEFAULTS: dict[str, object] = {
    "briefing": True,
    "tasks_index": True,
    "parse": "both",
    "sections_include": [],
    "sections_exclude": [],
    "top_n": None,
    "category_hint": None,
}


# --- Helpers --------------------------------------------------------


def _is_done(line: str) -> bool:
    return bool(_DONE_RE.search(line))


def _strip_done(text: str) -> str:
    return _DONE_RE.sub("", text, count=1).strip()


def _normalize_header(header: str) -> str:
    cleaned = "".join(
        ch for ch in unicodedata.normalize("NFKD", header)
        if not unicodedata.combining(ch)
    )
    cleaned = re.sub(r"[^\w\s&-]+", " ", cleaned, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def _is_placeholder(text: str) -> bool:
    if not text:
        return True
    if text.startswith("[Description]") or text.startswith("[description]"):
        return True
    if re.match(r"^\*[^*]+\*$", text):
        return True
    return False


# --- Frontmatter parser (minimal, no PyYAML dep) --------------------


def _coerce_scalar(raw: str):
    s = raw.strip()
    if s == "" or s.lower() in ("null", "~"):
        return None
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_coerce_scalar(p) for p in _split_commas(inner)]
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _split_commas(s: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts]


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FM_FENCE:
        return {}, text
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == _FM_FENCE:
            end_idx = i
            break
    if end_idx is None:
        return {}, text

    meta: dict[str, object] = {}
    current_list_key: Optional[str] = None
    for raw in lines[1:end_idx]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            current_list_key = None
            continue
        m_item = re.match(r"^\s+-\s+(.*)$", raw)
        if m_item and current_list_key is not None:
            lst = meta.setdefault(current_list_key, [])
            if isinstance(lst, list):
                lst.append(_coerce_scalar(m_item.group(1)))
            continue
        m_kv = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", raw)
        if not m_kv:
            current_list_key = None
            continue
        key, val = m_kv.group(1), m_kv.group(2)
        if val.strip() == "":
            meta[key] = []
            current_list_key = key
        else:
            meta[key] = _coerce_scalar(val)
            current_list_key = None

    body = "\n".join(lines[end_idx + 1:])
    return meta, body


def _meta_with_defaults(meta: dict[str, object]) -> dict[str, object]:
    merged = dict(FALLBACK_DEFAULTS)
    merged.update(meta or {})
    return merged


# --- Adapter --------------------------------------------------------


@dataclass
class MarkdownAdapter:
    """Reads markdown task files into RawTasks."""

    name: str = "markdown"
    repo_root: Path = field(default_factory=lambda: Path("/opt/data/personal-agent"))
    files: list[str] = field(default_factory=lambda: list(DEFAULT_TASK_FILES))
    include_done: bool = True       # carry recent [x] for history
    only_for: Optional[str] = None  # "briefing" | "tasks_index" | None

    def read(self) -> Iterator[RawTask]:
        for rel in self.files:
            path = self.repo_root / rel
            if not path.exists():
                logger.info("markdown adapter: skip missing %s", rel)
                continue
            try:
                yield from self._read_file(path, rel)
            except Exception as e:  # noqa: BLE001
                logger.exception("markdown adapter: error reading %s: %s", rel, e)

    def _read_file(self, path: Path, rel: str) -> Iterator[RawTask]:
        text = path.read_text(encoding="utf-8")
        meta_raw, body = parse_frontmatter(text)
        meta = _meta_with_defaults(meta_raw)

        if self.only_for == "briefing" and not meta.get("briefing", True):
            return
        if self.only_for == "tasks_index" and not meta.get("tasks_index", True):
            return

        parse_mode = (meta.get("parse") or "both").lower()
        sections_include = {_normalize_header(s) for s in (meta.get("sections_include") or [])}
        sections_exclude = {_normalize_header(s) for s in (meta.get("sections_exclude") or [])}
        sections_exclude |= GLOBAL_SECTION_BLACKLIST
        category_hint = meta.get("category_hint")

        current_section: Optional[str] = None
        current_subsection: Optional[str] = None
        kept_active = 0
        top_n = meta.get("top_n") if isinstance(meta.get("top_n"), int) else None

        for idx, raw in enumerate(body.split("\n"), start=1):
            line = raw.rstrip("\r")

            if line.startswith("### "):
                current_subsection = line[4:].strip()
                continue
            if line.startswith("## ") and not line.startswith("### "):
                current_section = line[3:].strip()
                current_subsection = None
                continue
            if line.startswith("# "):
                continue

            if current_subsection:
                sub_norm = _normalize_header(current_subsection)
                if sub_norm in sections_exclude or any(kw in sub_norm for kw in sections_exclude):
                    continue
                if sections_include and sub_norm not in sections_include and not any(
                    kw in sub_norm for kw in sections_include
                ):
                    continue

            task_text: Optional[str] = None
            done: bool = False

            if parse_mode in ("checkbox", "both"):
                m = _CHECKBOX_RE.match(line)
                if m:
                    mark, text = m.group(1), m.group(2).strip()
                    if _is_placeholder(text):
                        continue
                    done = mark.lower() == "x" or _is_done(line)
                    task_text = _strip_done(text) or text

            if task_text is None and parse_mode in ("numbered", "both"):
                m = _NUMBERED_RE.match(line)
                if m:
                    text = m.group(2).strip()
                    if _is_placeholder(text) or len(text) < 5:
                        continue
                    done = _is_done(line)
                    task_text = _strip_done(text) or text

            if task_text is None:
                continue

            if not done:
                if top_n is not None and kept_active >= top_n:
                    continue
                kept_active += 1
            elif not self.include_done:
                continue

            yield RawTask(
                text=task_text,
                source=f"markdown:{rel}:L{idx}",
                status="done" if done else "open",
                category_hint=category_hint if isinstance(category_hint, str) else None,
                raw={
                    "file": rel,
                    "line": idx,
                    "section": current_section,
                    "subsection": current_subsection,
                },
            )


# --- Path helpers ---------------------------------------------------


def detect_repo_root(default: str = "/opt/data/personal-agent") -> Path:
    """Resolve where personal-agent lives.

    Container default is /opt/data/personal-agent (mounted in
    docker-compose). When running on the host directly, we fall back to
    /Users/dmitry/Documents/GitHub/personal-agent so smoke tests and
    migration dry-runs work without docker."""
    env = os.environ.get("PERSONAL_AGENT_ROOT")
    if env:
        return Path(env)
    p = Path(default)
    if (p / "everything").exists():
        return p
    host = Path("/Users/dmitry/Documents/GitHub/personal-agent")
    if (host / "everything").exists():
        return host
    return p


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    adapter = MarkdownAdapter(repo_root=detect_repo_root())
    n = 0
    by_status: dict[str, int] = {}
    for rt in adapter.read():
        n += 1
        by_status[rt.status] = by_status.get(rt.status, 0) + 1
    print(f"total: {n}, by status: {by_status}")
