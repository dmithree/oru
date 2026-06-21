"""Resistance-band workout programs: parsing, rotation, Telegram formatting.

Two programs live as markdown under ``src/programs/`` (single source of truth,
copied verbatim from the user's notes). This module reads a program, splits it
into the three training days (A/B/C), parses the 6-week progression table, and
renders a Telegram-safe message for one session.

State (active program, rotation pointer, cycle start date) is owned by main.py;
the functions here are pure so they can be unit-tested without I/O or network.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

_PROGRAMS_DIR = Path(__file__).parent / "programs"

# id -> source file, cron day-of-week (APScheduler), human title.
# Day-of-week is kept here (not parsed from the Russian prose) on purpose.
PROGRAMS: dict[str, dict[str, str]] = {
    "program_1_cyclist": {
        "file": "program_1_cyclist.md",
        "dow": "mon,wed,sat",
        "title": "Велосипедист",
    },
    "program_2_general": {
        "file": "program_2_general.md",
        "dow": "tue,thu,sat",
        "title": "Общая форма",
    },
}

DEFAULT_PROGRAM = "program_1_cyclist"

_DAY_ORDER = ["A", "B", "C"]
_DAY_RE = re.compile(r"^День\s+([ABC])\b\s*(?:—|-)?\s*(.*)$")
# Table row like:  | 1–2 | Базовый объём, ... |   (en-dash or hyphen in the range)
_PROG_ROW_RE = re.compile(r"^\|\s*(\d+)\s*[–-]\s*\d+\s*\|\s*(.+?)\s*\|\s*$")


def advance(day_label: str) -> str:
    """Next day in the A→B→C→A rotation."""
    idx = _DAY_ORDER.index(day_label)
    return _DAY_ORDER[(idx + 1) % len(_DAY_ORDER)]


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) pairs on level-2 (``## ``) headings."""
    sections: list[tuple[str, str]] = []
    heading: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if heading is not None:
                sections.append((heading, "\n".join(body).strip()))
            heading = line[3:].strip()
            body = []
        elif heading is not None:
            body.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(body).strip()))
    return sections


def _parse_progression(body: str) -> dict[int, str]:
    """Parse the 6-week progression table into {start_week: task}."""
    out: dict[int, str] = {}
    for line in body.splitlines():
        m = _PROG_ROW_RE.match(line.strip())
        if m:
            out[int(m.group(1))] = m.group(2).strip()
    return out


def load_program(program_id: str) -> dict[str, Any]:
    """Read and parse a program markdown file into structured data."""
    if program_id not in PROGRAMS:
        raise ValueError(f"unknown program: {program_id}")
    meta = PROGRAMS[program_id]
    text = (_PROGRAMS_DIR / meta["file"]).read_text(encoding="utf-8")

    days: dict[str, dict[str, str]] = {}
    progression: dict[int, str] = {}
    for heading, body in _split_sections(text):
        day_match = _DAY_RE.match(heading)
        if day_match:
            label, subtitle = day_match.group(1), day_match.group(2).strip()
            days[label] = {"title": subtitle, "body": body}
        elif heading.lower().startswith("прогрессия"):
            progression = _parse_progression(body)

    missing = [d for d in _DAY_ORDER if d not in days]
    if missing:
        raise ValueError(f"{program_id}: missing day sections {missing}")

    return {
        "id": program_id,
        "title": meta["title"],
        "dow": meta["dow"],
        "days": days,
        "progression": progression,
    }


def cycle_week(start_date: date, today: date) -> int:
    """1-based week number within the program cycle."""
    return max(0, (today - start_date).days) // 7 + 1


def week_progression(program: dict[str, Any], week: int) -> str:
    """Progression hint for the given cycle week (buckets 1–2 / 3–4 / 5–6)."""
    table = program.get("progression") or {}
    if not table:
        return ""
    bucket = 1 if week <= 2 else (3 if week <= 4 else 5)
    # Fall back to the closest defined bucket at or below the target.
    for key in (bucket, 3, 1):
        if key in table:
            return table[key]
    return ""


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _md_to_tg(text: str) -> str:
    """Convert source markdown to Telegram legacy-Markdown-safe text.

    Telegram legacy Markdown rejects unbalanced ``*`` with HTTP 400, so balance
    must be guaranteed by construction:
      - drop ``---`` rules;
      - heading lines (``###``/``##``) become a single ``*bold*`` span — any
        inline emphasis inside the heading (e.g. ``*(суперсет)*``) is stripped
        first so wrapping can't produce an odd star count;
      - body ``**bold**`` collapses to ``*bold*`` via regex (always paired).
    Result has an even number of ``*`` and no other Markdown-special chars.
    """
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---" or not stripped:
            if out and out[-1] != "":
                out.append("")
            continue
        if line.startswith("### "):
            line = "*" + line[4:].strip().replace("*", "") + "*"
        elif line.startswith("## "):
            line = "*" + line[3:].strip().replace("*", "") + "*"
        else:
            line = _BOLD_RE.sub(r"*\1*", line)
        out.append(line)
    return "\n".join(out).strip()


# Owner's physical band set, ascending resistance (red < black < purple < green;
# also narrowest → widest). Programs name three load levels; map them to colours.
# Purple sits between black and green and is used for progression weeks.
BANDS: dict[str, tuple[str, str]] = {
    "лёгкая": ("🔴", "красная"),
    "средняя": ("⚫", "чёрная"),
    "тяжёлая": ("🟢", "зелёная"),
}
_BAND_LEGEND = "Ленты по нарастанию: 🔴 красная · ⚫ чёрная · 🟣 фиолетовая · 🟢 зелёная"
# Match the band level right after the "*Лента:*" label (so muscle names like
# "средняя ягодичная" are never touched), plus an optional placement note in
# parentheses that we fold inside the result.
_BAND_RE = re.compile(r"(\*Лента:\*\s*)(лёгкая|средняя|тяжёлая)(?:\s*\(([^)]*)\))?")


def _apply_bands(text: str) -> str:
    """Annotate each "*Лента:* <level>" with the owner's actual band colour."""
    def repl(m: "re.Match[str]") -> str:
        label, level, note = m.group(1), m.group(2), m.group(3)
        emoji, color = BANDS[level]
        inside = f"{level}, {note}" if note else level
        return f"{label}{emoji} {color} ({inside})"
    return _BAND_RE.sub(repl, text)


def format_session(program: dict[str, Any], day_label: str, week: int) -> str:
    """Render one training session as a Telegram message."""
    day = program["days"][day_label]
    header = f"🏋️ *{program['title']} · День {day_label} — {day['title']}*"
    body = _apply_bands(_md_to_tg(day["body"]))
    hint = week_progression(program, week)
    parts = [header, "", body]
    if hint:
        parts += ["", f"*Прогрессия (неделя {week}):* {hint}"]
    parts += ["", _BAND_LEGEND]
    return "\n".join(parts).strip()
