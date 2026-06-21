"""Metadata extractors for task text — pure regex.

Implements ideas 7 (deadlines), 8 (context tags), 10 (effort), 11 (cog
type) and priority. Each extractor is independent and returns a tuple
of (extracted_value, cleaned_text) so an ingestor can chain them and
end up with text that's stripped of metadata tokens (or keep them; both
are valid choices).

Sticking to inline tokens — no natural-language parsing here. NL-style
deadlines ("до пятницы", "завтра") are deferred to Phase 1.5 where an
LLM normalizes them at ingest time.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

# === Deadline ========================================================
# Supported inline tokens:
#   "— due 2026-07-15"  / "-- due 2026-07-15"  / "— due 2026-07"
#   "due:2026-07-15"
#   "[due:2026-07-15]"
#   "(due 2026-07-15)"
#
# Day precision: full YYYY-MM-DD. Month precision: YYYY-MM (mapped to
# the 1st of the month so date arithmetic still works, with
# due_precision='month' preserved).

_DUE_DAY_RE = re.compile(
    r"""
    (?:[—–\-]{1,2}\s*)?       # optional em/en/double-hyphen lead-in
    \b(?:due)\s*[:\s]\s*                # "due" with : or space
    (\d{4}-\d{2}-\d{2})                 # YYYY-MM-DD
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_DUE_MONTH_RE = re.compile(
    r"""
    (?:[—–\-]{1,2}\s*)?
    \b(?:due)\s*[:\s]\s*
    (\d{4}-\d{2})                       # YYYY-MM only
    (?![\d-])                           # not followed by another -DD
    """,
    re.IGNORECASE | re.VERBOSE,
)


def extract_deadline(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return (due_at_iso, due_precision). None if no deadline found."""
    if not text:
        return None, None
    m = _DUE_DAY_RE.search(text)
    if m:
        return m.group(1), "day"
    m = _DUE_MONTH_RE.search(text)
    if m:
        return f"{m.group(1)}-01", "month"
    return None, None


# === Context tags ====================================================
# @tag or @tag:value. Allowed chars in tag name: [A-Za-z0-9_-]. Value
# part can be a person name etc., so we allow any non-space char.

_TAG_RE = re.compile(r"@([A-Za-z][\w-]*)(?::(\S+))?")


def extract_context_tags(text: str) -> list[str]:
    """Return ordered list of @-tags. Preserves @key:value form."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _TAG_RE.finditer(text):
        tag = f"@{m.group(1)}"
        if m.group(2):
            tag = f"{tag}:{m.group(2)}"
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


# === Effort ==========================================================
# ~15m, ~30m, ~2h, ~deep. "~deep" maps to 90 (convention: deep work
# blocks are >= 90 minutes).

_EFFORT_NUMERIC_RE = re.compile(r"~(\d+)\s*(m|h)\b", re.IGNORECASE)
_EFFORT_DEEP_RE = re.compile(r"~deep\b", re.IGNORECASE)


def extract_effort_min(text: str) -> Optional[int]:
    """Return effort estimate in minutes, or None if not specified."""
    if not text:
        return None
    m = _EFFORT_NUMERIC_RE.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        return n * (60 if unit == "h" else 1)
    if _EFFORT_DEEP_RE.search(text):
        return 90
    return None


# === Cognitive type ==================================================
# cog:deep | cog:short | cog:ai | cog:admin | cog:social

_COG_RE = re.compile(r"\bcog:(deep|short|ai|admin|social)\b", re.IGNORECASE)


def extract_cog_type(text: str) -> Optional[str]:
    if not text:
        return None
    m = _COG_RE.search(text)
    return m.group(1).lower() if m else None


# === Priority ========================================================
# !P0, !P1, !P2, !P3 — P0 = drop everything, P3 = nice to have.

_PRIORITY_RE = re.compile(r"!(P[0-3])\b", re.IGNORECASE)


def extract_priority(text: str) -> Optional[str]:
    if not text:
        return None
    m = _PRIORITY_RE.search(text)
    return m.group(1).upper() if m else None


# === Recurrence (idea 9, parser only; spawn logic lands in Phase 3) ==
# every:7d, every:1m, every:3m, every:mon, every:tue, ...

_RECURRENCE_RE = re.compile(
    r"\bevery:(\d+(?:d|w|m|y)|mon|tue|wed|thu|fri|sat|sun)\b",
    re.IGNORECASE,
)


def extract_recurrence(text: str) -> Optional[str]:
    if not text:
        return None
    m = _RECURRENCE_RE.search(text)
    return f"every:{m.group(1).lower()}" if m else None


# === Combined =========================================================


_LEADING_DASHES = re.compile(r"^[\s\-—–]+|[\s\-—–]+$")


def clean_text(text: str) -> str:
    """Strip all metadata tokens (deadlines, tags, effort, cog, priority,
    recurrence) and surrounding punctuation. Returns the human display
    form of the task — the tokens are still kept as structured fields."""
    if not text:
        return ""
    t = text
    t = _DUE_DAY_RE.sub("", t)
    t = _DUE_MONTH_RE.sub("", t)
    t = _TAG_RE.sub("", t)
    t = _EFFORT_NUMERIC_RE.sub("", t)
    t = _EFFORT_DEEP_RE.sub("", t)
    t = _COG_RE.sub("", t)
    t = _PRIORITY_RE.sub("", t)
    t = _RECURRENCE_RE.sub("", t)
    # collapse whitespace and trim trailing em-dashes left behind by
    # stripping "— due ..." mid-sentence.
    t = re.sub(r"\s+", " ", t).strip()
    t = _LEADING_DASHES.sub("", t)
    return t


def parse_metadata(text: str) -> dict:
    """One-shot extractor. Returns a dict of all detected fields. Fields
    that aren't found are absent (not None) so the dict can be splatted
    into store.create_task() without forcing nulls."""
    out: dict = {}

    due_at, due_precision = extract_deadline(text)
    if due_at:
        out["due_at"] = due_at
        out["due_precision"] = due_precision

    tags = extract_context_tags(text)
    if tags:
        out["context_tags"] = tags

    effort = extract_effort_min(text)
    if effort is not None:
        out["effort_min"] = effort

    cog = extract_cog_type(text)
    if cog:
        out["cog_type"] = cog

    priority = extract_priority(text)
    if priority:
        out["priority"] = priority

    rec = extract_recurrence(text)
    if rec:
        out["recurrence"] = rec

    return out


# === Smoke test ======================================================
if __name__ == "__main__":
    samples = [
        "Доделать onboarding @laptop ~2h !P1 — due 2026-07-15",
        "Позвонить родителям @phone every:1w cog:social ~30m",
        "Анализы крови every:3m — due 2026-09",
        "Купить молоко @phone @home",
        "Trivial task",
    ]
    for s in samples:
        print(f"INPUT:  {s}")
        print(f"  clean: {clean_text(s)!r}")
        print(f"  meta:  {parse_metadata(s)}")
