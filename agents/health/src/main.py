"""Health agent entry point.

Two concurrent jobs in one process:
  1. APScheduler cron -> run_weekly_digest() every Sun 18:00 Belgrade
  2. FastAPI on :8001 -> /healthz, /digest, /run, /fetch-test

State persists in settings.state_file (mounted volume).
"""
import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI

from .analyzer import build_digest
from .config import settings
from .fetchers import oura, strava
from .telegram import send
from .workouts import (
    DEFAULT_PROGRAM,
    PROGRAMS,
    advance,
    cycle_week,
    format_session,
    load_program,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("health")


app = FastAPI(title="health-agent", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "agent": "health"}


@app.get("/digest")
def get_latest_digest() -> dict:
    """Return last computed digest from state file."""
    path = Path(settings.state_file)
    if not path.exists():
        return {"status": "no_digest_yet"}
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/run")
async def run_now(notify: bool = True) -> dict:
    """Manual trigger.

    notify=true  (default, cron path) — fetch + analyze + Telegram send + state write
    notify=false (Hermes path) — fetch + analyze + state write, no Telegram (Hermes responds itself)
    Returns the digest in both cases.
    """
    logger.info("Manual digest trigger received (notify=%s)", notify)
    digest = await asyncio.to_thread(run_weekly_digest, notify)
    return {"ok": True, "digest": digest}


@app.get("/fetch-test")
def fetch_test() -> dict:
    """Quick sanity check: are Oura and Strava credentials wired correctly."""
    return {
        "oura": oura.fetch_week(),
        "strava": strava.fetch_week(),
    }


@app.get("/workout")
def get_workout() -> dict:
    """Preview the next training session without sending or advancing."""
    state = _load_workout_state()
    return {
        "ok": True,
        "active_program": state["active_program"],
        "next_day": state.get("next_day", "A"),
        "session": _build_session(state),
    }


@app.post("/workout/run")
async def run_workout(notify: bool = True) -> dict:
    """Send the next session now (notify=false = dry-run) and advance the cycle."""
    session = await asyncio.to_thread(run_workout_reminder, notify)
    return {"ok": True, "session": session}


@app.post("/workout/switch")
def switch_workout(program: str) -> dict:
    """Switch the active program; resets cycle start to today and rotation to A.

    The reminder day-of-week changes with the program — the new schedule takes
    effect on the next container restart.
    """
    if program not in PROGRAMS:
        return {"ok": False, "error": f"unknown program: {program}", "available": list(PROGRAMS)}
    state = _load_workout_state()
    state["active_program"] = program
    state["start_date"] = date.today().isoformat()
    state["next_day"] = "A"
    _write_workout_state(state)
    logger.info("Active workout program switched to %s (restart to apply schedule)", program)
    return {"ok": True, "active_program": program, "dow": PROGRAMS[program]["dow"]}


def _format_telegram(digest: dict) -> str:
    state = digest.get("state", "unknown")
    summary = digest.get("summary", "")
    return f"*Health weekly* — state: `{state}`\n\n{summary}"


def _write_state(digest: dict) -> None:
    path = Path(settings.state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")


def run_weekly_digest(notify: bool = True) -> dict:
    logger.info("Building weekly digest (notify=%s)...", notify)
    digest = build_digest()
    _write_state(digest)
    if notify:
        sent = send(_format_telegram(digest))
        logger.info("Digest done. state=%s telegram=%s", digest.get("state"), sent)
    else:
        logger.info("Digest done. state=%s (telegram skipped)", digest.get("state"))
    return digest


# --- Workout reminders (resistance bands) -------------------------------------


def _load_workout_state() -> dict:
    """Read workout state; initialise with defaults (active=program 1) if absent."""
    path = Path(settings.workouts_state_file)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    state = {
        "active_program": DEFAULT_PROGRAM,
        "start_date": date.today().isoformat(),
        "next_day": "A",
        "history": [],
        "last_sent_at": None,
    }
    _write_workout_state(state)
    return state


def _write_workout_state(state: dict) -> None:
    path = Path(settings.workouts_state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_session(state: dict) -> dict:
    """Render the next session (read-only — does not advance the rotation)."""
    program = load_program(state["active_program"])
    day_label = state.get("next_day", "A")
    week = cycle_week(date.fromisoformat(state["start_date"]), date.today())
    return {
        "program": program["id"],
        "program_title": program["title"],
        "day": day_label,
        "week": week,
        "text": format_session(program, day_label, week),
    }


def run_workout_reminder(notify: bool = True) -> dict:
    """Build the next session, optionally send it, then advance the A→B→C cycle."""
    state = _load_workout_state()
    session = _build_session(state)
    if notify:
        session["telegram"] = send(session["text"])
        state["last_sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state["history"] = (state.get("history") or [])[-49:] + [
            {"date": date.today().isoformat(), "program": session["program"], "day": session["day"]}
        ]
    state["next_day"] = advance(session["day"])
    _write_workout_state(state)
    logger.info(
        "Workout reminder: program=%s day=%s week=%s notify=%s",
        session["program"], session["day"], session["week"], notify,
    )
    return session


def _parse_cron(expr: str) -> CronTrigger:
    minute, hour, day, month, dow = expr.split()
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow)


async def main() -> None:
    logger.info("Starting health agent (cron=%s, tz=%s)", settings.digest_cron, os.environ.get("TZ", "system"))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: asyncio.create_task(asyncio.to_thread(run_weekly_digest, True)),
        _parse_cron(settings.digest_cron),
        id="weekly-digest",
    )

    if settings.workout_enabled:
        active = _load_workout_state()["active_program"]
        dow = PROGRAMS[active]["dow"]
        scheduler.add_job(
            lambda: asyncio.create_task(asyncio.to_thread(run_workout_reminder, True)),
            CronTrigger(hour=settings.workout_hour, minute=0, day_of_week=dow),
            id="workout-reminder",
        )
        logger.info("Workout reminders: program=%s dow=%s hour=%02d:00", active, dow, settings.workout_hour)

    scheduler.start()

    config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
