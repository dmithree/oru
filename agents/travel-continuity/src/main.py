"""Travel continuity agent.

Schedules:
  - phase-check tick every 30 min — evaluates trip phase, runs morning/evening if local time matches
  - pre-trip-daily 19:00 Europe/Belgrade — gentle pre-trip reminder during T-7 window
  - post-trip-check 09:00 Europe/Belgrade — runs recap on T+1

HTTP:
  - GET  /healthz
  - GET  /status        — active trip + phase + days_left
  - GET  /raw           — debug data
  - POST /start-trip    — set active_trip in personal-context
  - POST /end-trip      — clear active_trip
  - POST /preflight?notify=...     — manual pre-trip generation
  - POST /run-morning?notify=...   — manual active morning
  - POST /run-evening?notify=...   — manual evening check-in prompt
  - POST /save-checkin              — save user's evening response
  - POST /recap?notify=...          — manual post-trip recap
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytz
import uvicorn  # noqa: F401
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Body

from . import state_bus, orchestrator, planner, publisher
from .config import settings
from .telegram import send

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("travel-continuity")

app = FastAPI(title="travel-continuity", version="0.2.0")
PENDING_CHECKIN_FILE = Path(settings.state_file).parent / "travel-continuity-pending.json"


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "agent": "travel-continuity"}


@app.get("/status")
def status() -> dict:
    phase = orchestrator.detect_phase()
    return phase


@app.get("/pending-checkin")
def pending_checkin() -> dict:
    if not PENDING_CHECKIN_FILE.exists():
        return {"pending": False}
    return {"pending": True, **json.loads(PENDING_CHECKIN_FILE.read_text(encoding="utf-8"))}


@app.get("/raw")
def raw() -> dict:
    phase = orchestrator.detect_phase()
    last = {}
    p = Path(settings.state_file)
    if p.exists():
        last = json.loads(p.read_text(encoding="utf-8"))
    return {"phase": phase, "last": last}


@app.post("/start-trip")
async def start_trip(
    destination: str = Body(...),
    start_date: str = Body(...),
    end_date: str = Body(...),
    timezone_str: str = Body("UTC", alias="timezone"),
    plan_file: str | None = Body(None),
    slug: str | None = Body(None),
) -> dict:
    trip = {
        "destination": destination,
        "start_date": start_date,
        "end_date": end_date,
        "timezone": timezone_str,
        "plan_file": plan_file,
        "slug": slug or f"{destination.lower().replace(' ', '-')}-{start_date}",
    }
    state_bus.set_active_trip(trip)
    logger.info("Active trip set: %s", trip)
    return {"ok": True, "trip": trip}


@app.post("/end-trip")
async def end_trip() -> dict:
    state_bus.set_active_trip(None)
    logger.info("Active trip cleared")
    return {"ok": True}


@app.post("/preflight")
async def preflight(notify: bool = True) -> dict:
    trip = state_bus.get_active_trip()
    if not trip:
        return {"error": "no active trip"}
    result = await asyncio.to_thread(orchestrator.build_pretrip, trip)
    _save_state(result)
    if notify:
        send(f"*Pre-trip: {trip['destination']}*\n\n{result.get('summary', '')}")
    return {"ok": True, "result": result}


@app.post("/run-morning")
async def run_morning(notify: bool = True) -> dict:
    trip = state_bus.get_active_trip()
    if not trip:
        return {"error": "no active trip"}
    result = await asyncio.to_thread(orchestrator.build_active_morning, trip)
    _save_state(result)
    if notify:
        send(f"*Travel — утро*\n\n{result.get('summary', '')}")
    return {"ok": True, "result": result}


@app.post("/run-evening")
async def run_evening(notify: bool = True) -> dict:
    trip = state_bus.get_active_trip()
    if not trip:
        return {"error": "no active trip"}
    result = orchestrator.build_active_evening_prompt(trip)
    PENDING_CHECKIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_CHECKIN_FILE.write_text(
        json.dumps({"started_at": datetime.now().isoformat(), "trip": trip, "question": result["question"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    if notify:
        send(result["question"])
    return {"ok": True, "result": result}


@app.post("/save-checkin")
async def save_checkin(user_text: str = Body(..., embed=True)) -> dict:
    trip = state_bus.get_active_trip()
    if not trip:
        return {"error": "no active trip"}
    result = await asyncio.to_thread(orchestrator.save_evening_checkin, trip, user_text)
    if PENDING_CHECKIN_FILE.exists():
        PENDING_CHECKIN_FILE.unlink()
    return {"ok": True, "saved": result}


@app.post("/recap")
async def recap(notify: bool = True) -> dict:
    trip = state_bus.get_active_trip()
    if not trip:
        return {"error": "no active trip"}
    result = await asyncio.to_thread(orchestrator.build_recap, trip)
    _save_state(result)
    if notify:
        send(f"*Recap: {trip['destination']}*\n\n{result.get('summary', '')}")
    state_bus.set_active_trip(None)
    return {"ok": True, "result": result}


# ---------- Full planning + publishing ----------

@app.post("/plan")
async def plan_trip(
    destination: str = Body(...),
    start_date: str = Body(...),
    end_date: str = Body(...),
    budget: str | None = Body(None),
    pace: str | None = Body(None),
    interests: list[str] | None = Body(None),
    purpose: str | None = Body(None),
    country_hint: str | None = Body(None),
    must_see: list[str] | None = Body(None),
    save_to_disk: bool = Body(True),
) -> dict:
    """Generate full trip plan markdown with three-tier recs, golden hour, walking routes, packing."""
    result = await asyncio.to_thread(
        planner.build_full_plan,
        destination, start_date, end_date,
        budget=budget, pace=pace, interests=interests,
        purpose=purpose, country_hint=country_hint, must_see=must_see,
    )
    if save_to_disk:
        year = result["start_date"][:4]
        plan_dir = Path(settings.travel_dir) / year
        plan_dir.mkdir(parents=True, exist_ok=True)
        slug = publisher.make_trip_slug(destination, datetime.fromisoformat(result["start_date"]).date(), datetime.fromisoformat(result["end_date"]).date())
        plan_file = plan_dir / f"{slug}.md"
        plan_file.write_text(result["markdown"], encoding="utf-8")
        result["plan_file"] = str(plan_file)
        result["slug"] = slug
    _save_state(result)
    return {"ok": True, "result": result}


@app.post("/publish")
async def publish(
    destination: str = Body(...),
    start_date: str = Body(...),
    end_date: str = Body(...),
    filename: str | None = Body(None),
    markdown: str | None = Body(None),
    expires_in_days: int | None = Body(None),
    sanitize: bool = Body(False),
) -> dict:
    """Publish trip plan to posmotri (Vercel KV store via POST /api/share)."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    slug_hint = publisher.make_trip_slug(destination, start, end)

    if not markdown:
        year = start_date[:4]
        plan_file = Path(settings.travel_dir) / year / f"{slug_hint}.md"
        if not plan_file.exists():
            return {"error": f"no plan file at {plan_file}; pass `markdown` or call /plan first"}
        markdown = plan_file.read_text(encoding="utf-8")

    if not filename:
        filename = f"{destination} ({start_date} → {end_date})"

    expires_in_sec = int(expires_in_days * 86400) if expires_in_days else None
    result = await asyncio.to_thread(publisher.publish_markdown, filename, markdown, expires_in_sec, sanitize)
    return {**result, "filename": filename, "plan_slug_hint": slug_hint}


@app.post("/plan-and-publish")
async def plan_and_publish(
    destination: str = Body(...),
    start_date: str = Body(...),
    end_date: str = Body(...),
    budget: str | None = Body(None),
    pace: str | None = Body(None),
    interests: list[str] | None = Body(None),
    purpose: str | None = Body(None),
    country_hint: str | None = Body(None),
    must_see: list[str] | None = Body(None),
    set_as_active: bool = Body(False),
    notify: bool = Body(False),
) -> dict:
    """Combined endpoint: plan + publish + (optional) set as active_trip."""
    plan_result = await asyncio.to_thread(
        planner.build_full_plan,
        destination, start_date, end_date,
        budget=budget, pace=pace, interests=interests,
        purpose=purpose, country_hint=country_hint, must_see=must_see,
    )
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    slug = publisher.make_trip_slug(destination, start, end)

    year = start_date[:4]
    plan_dir = Path(settings.travel_dir) / year
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / f"{slug}.md"
    plan_file.write_text(plan_result["markdown"], encoding="utf-8")

    pub_filename = f"{destination} ({start_date} → {end_date})"
    pub_result = await asyncio.to_thread(publisher.publish_markdown, pub_filename, plan_result["markdown"])

    if set_as_active and pub_result.get("ok"):
        coords = await asyncio.to_thread(planner.weather.lookup_coords, destination)
        tz_name = coords[2] if coords else "UTC"
        trip = {
            "destination": destination,
            "start_date": start_date,
            "end_date": end_date,
            "timezone": tz_name,
            "plan_file": str(plan_file),
            "slug": slug,
            "shared_url": pub_result.get("url"),
        }
        state_bus.set_active_trip(trip)

    if notify and pub_result.get("ok"):
        send(f"*Trip plan published*: {destination} ({start_date} → {end_date})\n{pub_result.get('url', '')}")

    return {
        "ok": pub_result.get("ok", False),
        "plan_file": str(plan_file),
        "plan_slug_hint": slug,
        "url": pub_result.get("url"),
        "posmotri_slug": pub_result.get("slug"),
        "publish_error": pub_result.get("error"),
    }


@app.post("/three-recs")
async def three_recs(place: str = Body(...), city: str | None = Body(None)) -> dict:
    """Three-tier recommendations for a single place."""
    result = await asyncio.to_thread(planner.build_three_recs, place, city)
    return {"ok": True, "result": result}


# ---------- Distant-phase engagement ----------

@app.post("/run-distant")
async def run_distant(notify: bool = False, mark: bool = True) -> dict:
    """Generate today's proactive engagement question for the morning brief.
    `mark=True` records the topic as asked (so rotation advances). The morning
    brief script calls this with notify=false and injects the question itself."""
    trip = state_bus.get_active_trip()
    if not trip:
        return {"error": "no active trip"}
    phase = orchestrator.detect_phase()
    if phase["phase"] not in ("scheduled_distant", "pre_trip"):
        return {"ok": True, "skipped": f"phase={phase['phase']}", "question": ""}
    result = await asyncio.to_thread(orchestrator.build_distant_engagement, trip)
    if mark and result.get("topic"):
        slug = trip.get("slug") or trip["destination"]
        state_bus.mark_topic_asked(slug, result["topic"], date.today().isoformat())
    _save_state(result)
    if notify and result.get("question"):
        send(result["question"])
    return {"ok": True, "result": result}


@app.post("/distant-weekly")
async def distant_weekly(notify: bool = True) -> dict:
    """Weekly summary: decided / learned this week / overall picture."""
    trip = state_bus.get_active_trip()
    if not trip:
        return {"error": "no active trip"}
    result = await asyncio.to_thread(orchestrator.build_distant_weekly_summary, trip)
    _save_state(result)
    if notify and result.get("summary"):
        send(f"*Поездка {trip['destination']} — недельная сводка*\n\n{result.get('summary', '')}")
    return {"ok": True, "result": result}


@app.post("/log-decision")
async def log_decision(topic: str = Body(...), note: str = Body(...)) -> dict:
    """Record a decision Дима made (so engagement stops re-asking and the weekly
    summary reflects progress). Called by Oru/Hermes after Дима confirms an action."""
    trip = state_bus.get_active_trip()
    if not trip:
        return {"error": "no active trip"}
    slug = trip.get("slug") or trip["destination"]
    rec = state_bus.add_decision(slug, topic, note, date.today().isoformat())
    return {"ok": True, "decisions": rec.get("decisions", [])}


@app.get("/engagement")
def engagement() -> dict:
    """Debug: current engagement tracker state for the active trip."""
    trip = state_bus.get_active_trip()
    if not trip:
        return {"error": "no active trip"}
    slug = trip.get("slug") or trip["destination"]
    return {"slug": slug, "engagement": state_bus.get_engagement(slug)}


def _save_state(payload: Any) -> None:
    p = Path(settings.state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ---------- Schedulers ----------

def phase_tick() -> None:
    """Every 30 min: check trip phase, run morning/evening if local time matches.

    Active morning fires at 07:00 trip-local-time; evening at 21:00.
    Idempotent guard via state file: don't fire same trigger twice per day.
    """
    phase = orchestrator.detect_phase()
    if phase["phase"] != "active":
        return
    trip = phase["trip"]
    tz_name = trip.get("timezone") or "UTC"
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        logger.warning("Bad trip tz %s, falling back to UTC", tz_name)
        tz = pytz.UTC
    now_local = datetime.now(tz)
    hour = now_local.hour
    today_iso = now_local.date().isoformat()

    fired = _load_fired()
    if hour == 7 and fired.get("morning") != today_iso:
        logger.info("Firing active-trip morning (local hour=7)")
        result = orchestrator.build_active_morning(trip, today=now_local.date())
        _save_state(result)
        send(f"*Travel — утро*\n\n{result.get('summary', '')}")
        fired["morning"] = today_iso
        _save_fired(fired)
    elif hour == 21 and fired.get("evening") != today_iso:
        logger.info("Firing active-trip evening (local hour=21)")
        result = orchestrator.build_active_evening_prompt(trip, today=now_local.date())
        PENDING_CHECKIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        PENDING_CHECKIN_FILE.write_text(
            json.dumps({"started_at": datetime.now().isoformat(), "trip": trip, "question": result["question"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        send(result["question"])
        fired["evening"] = today_iso
        _save_fired(fired)


def pretrip_daily_tick() -> None:
    """19:00 Belgrade: if in pre_trip phase, send brief reminder."""
    phase = orchestrator.detect_phase()
    if phase["phase"] != "pre_trip":
        return
    trip = phase["trip"]
    fired = _load_fired()
    today_iso = date.today().isoformat()
    if fired.get("pretrip") == today_iso:
        return
    result = orchestrator.build_pretrip(trip)
    _save_state(result)
    send(f"*Pre-trip: {trip['destination']}* (через {phase['days_until']} дн.)\n\n{result.get('summary', '')[:1500]}")
    fired["pretrip"] = today_iso
    _save_fired(fired)


def post_trip_tick() -> None:
    """09:00 Belgrade daily: if today == end_date + 1, run recap."""
    phase = orchestrator.detect_phase()
    if phase["phase"] != "post_trip":
        return
    trip = phase["trip"]
    fired = _load_fired()
    today_iso = date.today().isoformat()
    if fired.get("recap") == today_iso:
        return
    result = orchestrator.build_recap(trip)
    _save_state(result)
    send(f"*Recap: {trip['destination']}*\n\n{result.get('summary', '')}")
    state_bus.set_active_trip(None)
    fired["recap"] = today_iso
    _save_fired(fired)


def distant_weekly_tick() -> None:
    """Weekly (Mon 18:00 Belgrade): in scheduled_distant phase, send the
    итоговую недельную сводку. Daily engagement rides the morning brief instead."""
    phase = orchestrator.detect_phase()
    if phase["phase"] != "scheduled_distant":
        return
    trip = phase["trip"]
    fired = _load_fired()
    # guard by ISO week so a restart mid-week doesn't double-fire
    week_tag = date.today().strftime("%G-W%V")
    if fired.get("distant_weekly") == week_tag:
        return
    result = orchestrator.build_distant_weekly_summary(trip)
    _save_state(result)
    if result.get("summary"):
        send(f"*Поездка {trip['destination']} — недельная сводка*\n\n{result.get('summary', '')}")
    fired["distant_weekly"] = week_tag
    _save_fired(fired)


FIRED_FILE = Path(settings.state_file).parent / "travel-continuity-fired.json"


def _load_fired() -> dict:
    if FIRED_FILE.exists():
        try:
            return json.loads(FIRED_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_fired(d: dict) -> None:
    FIRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    FIRED_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_cron(expr: str) -> CronTrigger:
    parts = expr.split()
    return CronTrigger(minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4])


async def main() -> None:
    logger.info("Starting travel-continuity")
    scheduler = AsyncIOScheduler()
    scheduler.add_job(phase_tick, _parse_cron(settings.phase_check_cron), id="phase-check")
    scheduler.add_job(pretrip_daily_tick, _parse_cron(settings.pretrip_daily_cron), id="pretrip-daily")
    scheduler.add_job(post_trip_tick, _parse_cron("0 9 * * *"), id="post-trip-check")
    scheduler.add_job(distant_weekly_tick, _parse_cron("0 18 * * 1"), id="distant-weekly")
    scheduler.start()

    config = uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
