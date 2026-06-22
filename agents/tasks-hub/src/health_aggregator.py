"""Aggregated health/status across the whole Oru stack.

tasks-hub is the natural central place to host this: it already knows
every other container by service name (Docker DNS) and reads the host
state files via /opt/state.

Result shape:

  {
    "generated_at": "...",
    "ok": true,
    "containers": {
      "<name>": {
        "ok": bool, "port": int, "http_ms": int|null,
        "error": str|null,
        "extra": {...}             # schema_version / digest / etc.
      },
      ...
    },
    "host_watchers": {
      "reminders-bridge":           {"ok": bool, "age_minutes": int, "stale_threshold_min": int, ...},
      "reminders-commands-watcher": {"ok": bool, ...},
      "living-markdown-sync":       {"ok": bool, ...}
    },
    "issues": ["short human-readable lines"]
  }
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


CONTAINERS = [
    ("health", "http://oru-health:8001/healthz"),
    ("daily-briefing", "http://oru-daily-briefing:8002/healthz"),
    ("travel-continuity", "http://oru-travel-continuity:8003/healthz"),
    ("tasks-hub", "http://oru-tasks-hub:8004/healthz"),
    ("self-reflection", "http://oru-self-reflection:8005/healthz"),
]

HTTP_TIMEOUT = 4
STATE_DIR = Path("/opt/state")

# How old (minutes) makes a watcher output "stale". Tuned against each
# watcher's cron interval with ~3x slack so a single missed tick is
# still ok.
WATCHER_BUDGETS_MIN = {
    "reminders-bridge": 60,             # cron 15 min
    "reminders-commands-watcher": 10,   # cron 1 min — silence is fine, but log file should exist
    "living-markdown-sync": 180,        # cron 1 h
    "skill-cache-invalidator": 10,      # cron 1 min; hash file should refresh whenever skills change
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _check_one(name: str, url: str) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            try:
                payload = r.json()
            except ValueError:
                payload = {}
            extra = {k: v for k, v in payload.items() if k not in ("ok", "agent")}
            return {"ok": bool(payload.get("ok", True)), "http_ms": ms, "error": None, "extra": extra}
        return {"ok": False, "http_ms": ms, "error": f"HTTP {r.status_code}", "extra": {}}
    except requests.RequestException as e:
        return {"ok": False, "http_ms": None, "error": str(e)[:200], "extra": {}}


def _check_containers() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    # ThreadPool so one stuck container can't block the others.
    with ThreadPoolExecutor(max_workers=len(CONTAINERS)) as ex:
        futures = {ex.submit(_check_one, name, url): name for name, url in CONTAINERS}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                out[name] = fut.result()
            except Exception as e:  # noqa: BLE001
                out[name] = {"ok": False, "http_ms": None, "error": f"unexpected: {e}", "extra": {}}
    return out


def _file_age_minutes(path: Path) -> Optional[float]:
    try:
        st = path.stat()
    except (FileNotFoundError, PermissionError):
        return None
    return (time.time() - st.st_mtime) / 60.0


def _last_jsonl_record(path: Path) -> Optional[dict[str, Any]]:
    """Read last non-empty line of a JSONL file. Cheap enough for our
    log sizes; we don't need a tail-from-end scan."""
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            data = fh.read()
        for line in reversed(data.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    except OSError:
        return None
    return None


def _check_host_watchers() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    # reminders-bridge — writes reminders.json every 15 min.
    bridge_file = STATE_DIR / "reminders.json"
    age = _file_age_minutes(bridge_file)
    budget = WATCHER_BUDGETS_MIN["reminders-bridge"]
    entry: dict[str, Any] = {
        "file": str(bridge_file),
        "age_minutes": round(age, 1) if age is not None else None,
        "stale_threshold_min": budget,
    }
    if age is None:
        entry["ok"] = False
        entry["error"] = "file missing"
    else:
        entry["ok"] = age <= budget
        try:
            payload = json.loads(bridge_file.read_text(encoding="utf-8"))
            entry["reminders_count"] = len(payload.get("reminders") or [])
            entry["partial_lists"] = len(payload.get("partial_lists") or [])
        except (OSError, json.JSONDecodeError) as e:
            entry["parse_error"] = str(e)[:200]
    out["reminders-bridge"] = entry

    # reminders-commands-watcher — applies queued commands; its evidence
    # is the log + cursor catching up with the queue. The watcher runs
    # every minute even with empty input, so the log file mtime alone
    # is a poor signal (it doesn't move when nothing arrives). Instead
    # we look at queue lag: how many bytes the cursor is behind.
    queue = STATE_DIR / "reminders-commands.jsonl"
    cursor = STATE_DIR / "reminders-commands-cursor"
    log = STATE_DIR / "reminders-commands-log.jsonl"
    entry = {
        "queue": str(queue),
        "log": str(log),
        "stale_threshold_min": WATCHER_BUDGETS_MIN["reminders-commands-watcher"],
    }
    try:
        queue_size = queue.stat().st_size if queue.exists() else 0
        cursor_val = 0
        if cursor.exists():
            raw = cursor.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                cursor_val = int(raw)
        lag = max(0, queue_size - cursor_val)
        entry["queue_bytes"] = queue_size
        entry["cursor_bytes"] = cursor_val
        entry["queue_lag_bytes"] = lag
        last = _last_jsonl_record(log)
        if last:
            entry["last_applied_at"] = last.get("applied_at")
            entry["last_result_ok"] = bool(last.get("result", {}).get("ok"))
        # ok unless we have backlog older than the budget. Without a
        # timestamp on each queue entry we can't measure backlog age
        # directly; instead we treat any lag of more than 256 bytes as
        # a soft warning (≈2 commands waiting).
        entry["ok"] = lag <= 256
        if lag > 256:
            entry["error"] = f"queue lag {lag} bytes (cursor not advancing)"
    except OSError as e:
        entry["ok"] = False
        entry["error"] = str(e)[:200]
    out["reminders-commands-watcher"] = entry

    # skill-cache-invalidator — hashes ~/.hermes/skills/*.SKILL.md mtimes
    # every minute and removes the hermes prompt snapshot on change. The
    # hash file is the only persistent breadcrumb we have. We don't fail
    # if it's "stale": when no skills change for hours, the watcher just
    # exits without writing. So absence = first run not happened yet.
    skill_hash = STATE_DIR / "skill-cache-manifest-hash"
    budget = WATCHER_BUDGETS_MIN["skill-cache-invalidator"]
    if not skill_hash.exists():
        out["skill-cache-invalidator"] = {
            "ok": False,
            "error": "manifest hash file not yet created",
            "file": str(skill_hash),
            "stale_threshold_min": budget,
        }
    else:
        age = _file_age_minutes(skill_hash)
        # No staleness check — a long quiet period is normal. We only
        # care the watcher's hash file is present and readable.
        out["skill-cache-invalidator"] = {
            "file": str(skill_hash),
            "age_minutes": round(age, 1) if age is not None else None,
            "ok": True,
        }

    # living-markdown-sync — overwrites the canonical living markdown
    # hourly. The watcher writes to LIVING_DEST (default
    # ~/Library/Application Support/oru-host-state/all-tasks.md) which
    # is bind-mounted into the container as /opt/state/all-tasks.md.
    # The personal-agent/everything/all-tasks.md path is TCC-blocked
    # for launchd-spawned bash on macOS Sonoma+ — not a valid signal.
    md_candidates = [
        STATE_DIR / "all-tasks.md",
    ]
    md_file = next((p for p in md_candidates if p.exists()), None)
    budget = WATCHER_BUDGETS_MIN["living-markdown-sync"]
    if md_file is None:
        out["living-markdown-sync"] = {
            "ok": False,
            "error": "all-tasks.md not visible to container",
            "stale_threshold_min": budget,
            "candidates": [str(p) for p in md_candidates],
        }
    else:
        age = _file_age_minutes(md_file)
        out["living-markdown-sync"] = {
            "file": str(md_file),
            "age_minutes": round(age, 1) if age is not None else None,
            "stale_threshold_min": budget,
            "ok": age is not None and age <= budget,
        }

    return out


def aggregate() -> dict[str, Any]:
    containers = _check_containers()
    host_watchers = _check_host_watchers()

    issues: list[str] = []
    for name, c in containers.items():
        if not c["ok"]:
            issues.append(f"container {name}: {c.get('error') or 'down'}")
    for name, w in host_watchers.items():
        if not w.get("ok"):
            note = w.get("error") or f"stale ({w.get('age_minutes')} min > {w.get('stale_threshold_min')} min)"
            issues.append(f"host {name}: {note}")

    return {
        "generated_at": _now_utc().isoformat(timespec="seconds"),
        "ok": not issues,
        "containers": containers,
        "host_watchers": host_watchers,
        "issues": issues,
    }
