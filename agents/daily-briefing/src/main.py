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


TASKS_HUB_URL = __import__("os").environ.get("TASKS_HUB_URL", "http://oru-tasks-hub:8004")


def _forward_to_tasks_hub(user_text: str) -> dict | None:
    """Send the debrief to tasks-hub for LLM-driven event ingestion
    (idea 14). Returns the upstream report dict, or None on failure
    so the caller can fall back to the local parser."""
    import urllib.error
    import urllib.request
    body = json.dumps({"user_text": user_text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        TASKS_HUB_URL.rstrip("/") + "/debrief/ingest",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.warning("tasks-hub /debrief/ingest unreachable (%s); falling back", e)
        return None
    except Exception:
        logger.exception("tasks-hub /debrief/ingest call failed")
        return None


@app.post("/save-debrief")
async def save_debrief(user_text: str = Body(..., embed=True)) -> dict:
    logger.info("Saving debrief response (%d chars)", len(user_text))

    # Phase 2.5b: prefer tasks-hub event ingestion so the debrief
    # actually mutates the store (closes/defers/blocks/creates tasks)
    # instead of just writing a parsed JSON file no consumer reads.
    upstream = await asyncio.to_thread(_forward_to_tasks_hub, user_text)
    if upstream and upstream.get("ok"):
        if PENDING_DEBRIEF_FILE.exists():
            PENDING_DEBRIEF_FILE.unlink()
        applied = upstream.get("applied") or []
        summary = upstream.get("summary") or {}
        if settings.telegram_chat_id:
            done = sum(1 for a in applied if a.get("kind") == "completed")
            deferred = sum(1 for a in applied if a.get("kind") == "deferred")
            blocked = sum(1 for a in applied if a.get("kind") == "blocked")
            created = sum(1 for a in applied if a.get("kind") == "created")
            send(
                "Debrief обработан. "
                f"Закрыто {done}, перенесено {deferred}, заблокировано {blocked}, новых {created}."
            )
        return {"ok": True, "via": "tasks-hub", "summary": summary, "applied": applied}

    # Fallback: local parser writes a JSON file. Kept for resilience
    # if tasks-hub is down — but the store stays unchanged.
    parsed = await asyncio.to_thread(parse_evening_debrief, user_text)
    p = Path(settings.state_file).parent / "daily-briefing-evening.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    if PENDING_DEBRIEF_FILE.exists():
        PENDING_DEBRIEF_FILE.unlink()
    if settings.telegram_chat_id:
        send("Debrief сохранён (tasks-hub недоступен, store не обновлён).")
    return {"ok": True, "via": "fallback", "parsed": parsed["parsed"]}


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


async def _morning_job() -> None:
    await asyncio.to_thread(do_morning, True)


async def _evening_job() -> None:
    await asyncio.to_thread(do_evening_prompt, True)


async def main() -> None:
    logger.info("Starting daily-briefing (morning=%s, evening=%s)", settings.morning_cron, settings.evening_cron)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_morning_job, _parse_cron(settings.morning_cron), id="morning")
    scheduler.add_job(_evening_job, _parse_cron(settings.evening_cron), id="evening")
    scheduler.start()
    config = uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
