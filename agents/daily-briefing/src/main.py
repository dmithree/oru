"""Daily briefing agent.

Cron:
  - 07:30 Europe/Belgrade -> morning brief sent to Telegram
  - 18:45 Europe/Belgrade -> evening prompt sent to Telegram (sets pending_debrief flag)

HTTP:
  - GET  /healthz
  - GET  /brief        — last morning brief
  - GET  /debrief      — last evening debrief
  - GET  /raw          — debug all source data
  - GET  /pending-debrief  — is there a pending debrief response?
  - POST /run-morning?notify=...
  - POST /run-evening?notify=...
  - POST /save-debrief?user_text=...  — save user's freeform debrief
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Body

from . import state_bus
from .analyzer import build_morning_brief, parse_evening_debrief
from .config import settings
from .fetchers import oura, strava, tasks, linear, reminders, transcripts
from .telegram import send

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("daily-briefing")

app = FastAPI(title="daily-briefing", version="0.2.0")
PENDING_DEBRIEF_FILE = Path(settings.state_file).parent / "daily-briefing-pending.json"


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "agent": "daily-briefing"}


@app.get("/brief")
def get_morning() -> dict:
    p = Path(settings.state_file)
    if not p.exists():
        return {"status": "no_brief_yet"}
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/debrief")
def get_evening() -> dict:
    p = Path(settings.state_file).parent / "daily-briefing-evening.json"
    if not p.exists():
        return {"status": "no_debrief_yet"}
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/pending-debrief")
def pending_debrief() -> dict:
    if not PENDING_DEBRIEF_FILE.exists():
        return {"pending": False}
    return {"pending": True, **json.loads(PENDING_DEBRIEF_FILE.read_text(encoding="utf-8"))}


@app.get("/raw")
def raw_sources() -> dict:
    return {
        "oura": oura.fetch_today(),
        "strava_yesterday": strava.fetch_yesterday(),
        "open_tasks_count": len(tasks.read_open_tasks()),
        "linear_stuck_count": len(linear.fetch_stuck()),
        "reminders": reminders.fetch_today_reminders(),
        "transcripts_24h_count": len(transcripts.fetch_recent_transcripts(["/opt/data/personal/therapy/transcripts", "/opt/data/personal/coach/transcripts"])),
        "personal_context": state_bus.read(),
    }


@app.post("/run-morning")
async def run_morning(notify: bool = True) -> dict:
    logger.info("Manual morning trigger (notify=%s)", notify)
    brief = await asyncio.to_thread(do_morning, notify)
    return {"ok": True, "brief": brief}


@app.post("/run-evening")
async def run_evening(notify: bool = True) -> dict:
    logger.info("Manual evening trigger (notify=%s)", notify)
    result = await asyncio.to_thread(do_evening_prompt, notify)
    return {"ok": True, "evening": result}


@app.post("/save-debrief")
async def save_debrief(user_text: str = Body(..., embed=True)) -> dict:
    logger.info("Saving debrief response (%d chars)", len(user_text))
    parsed = await asyncio.to_thread(parse_evening_debrief, user_text)
    p = Path(settings.state_file).parent / "daily-briefing-evening.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    if PENDING_DEBRIEF_FILE.exists():
        PENDING_DEBRIEF_FILE.unlink()
    if settings.telegram_chat_id:
        send("Debrief сохранён.")
    return {"ok": True, "parsed": parsed["parsed"]}


def do_morning(notify: bool = True) -> dict:
    logger.info("Building morning brief (notify=%s)...", notify)
    brief = build_morning_brief()
    p = Path(settings.state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    if notify:
        send(f"*Утренний бриф*\n\n{brief.get('summary', '')}")
    return brief


def do_evening_prompt(notify: bool = True) -> dict:
    """Send the question and set a pending-debrief flag."""
    question = "Как прошёл день? Что сделал из плана. Что зашло / не зашло. Инсайты."
    PENDING_DEBRIEF_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_DEBRIEF_FILE.write_text(
        json.dumps({"started_at": datetime.now().isoformat(), "question": question}, ensure_ascii=False),
        encoding="utf-8",
    )
    if notify:
        send(f"_Вечерний debrief_\n\n{question}")
    return {"question": question, "pending_set_at": datetime.now().isoformat()}


def _parse_cron(expr: str) -> CronTrigger:
    m, h, d, mo, dw = expr.split()
    return CronTrigger(minute=m, hour=h, day=d, month=mo, day_of_week=dw)


async def main() -> None:
    logger.info("Starting daily-briefing (morning=%s, evening=%s)", settings.morning_cron, settings.evening_cron)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: asyncio.create_task(asyncio.to_thread(do_morning, True)),
        _parse_cron(settings.morning_cron), id="morning",
    )
    scheduler.add_job(
        lambda: asyncio.create_task(asyncio.to_thread(do_evening_prompt, True)),
        _parse_cron(settings.evening_cron), id="evening",
    )
    scheduler.start()
    config = uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
