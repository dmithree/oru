"""self-reflection agent — IFS analyzer over therapy + coaching transcripts.

Cron: Sun 19:00 -> analyzer.run() emits homework as tasks-hub tasks
and updates personal-context.json#self with active_parts/weekly_themes.

HTTP:
  GET  /healthz
  GET  /state         — last run report
  POST /run           — trigger analyzer (body: {dry_run?})
"""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from pydantic import BaseModel

from . import analyzer, telegram
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("self-reflection")

app = FastAPI(title="self-reflection", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "agent": "self-reflection"}


@app.get("/state")
def get_state() -> dict:
    p = Path(settings.state_file)
    if not p.exists():
        return {"status": "no_run_yet"}
    return json.loads(p.read_text(encoding="utf-8"))


class RunRequest(BaseModel):
    dry_run: bool = False
    notify: bool = True


@app.post("/run")
async def run_now(body: RunRequest = RunRequest()) -> dict:
    result = await asyncio.to_thread(analyzer.run, dry_run=body.dry_run)
    if body.notify and not body.dry_run:
        await asyncio.to_thread(_notify, result)
    return result


def _notify(result: dict[str, Any]) -> None:
    parsed = result.get("parsed") or {}
    if not parsed:
        return
    msg = parsed.get("summary_for_telegram")
    if not msg:
        themes = parsed.get("weekly_themes") or []
        parts = [p.get("name") for p in (parsed.get("active_parts") or []) if p.get("name")]
        msg = (
            f"_Self-reflection_\n\n"
            f"Темы: {', '.join(themes) if themes else '(нет)'}\n"
            f"Активные части: {', '.join(parts) if parts else '(нет)'}\n"
            f"Новых домашек: {len(parsed.get('homework') or [])}"
        )
    telegram.send(msg)


def _scheduled_run() -> None:
    try:
        result = analyzer.run()
        if result.get("transcripts", 0) > 0:
            _notify(result)
    except Exception:
        logger.exception("scheduled run failed")


def _parse_cron(expr: str) -> CronTrigger:
    m, h, d, mo, dw = expr.split()
    return CronTrigger(minute=m, hour=h, day=d, month=mo, day_of_week=dw)


async def main() -> None:
    logger.info("self-reflection starting (port %d)", settings.http_port)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: asyncio.create_task(asyncio.to_thread(_scheduled_run)),
        _parse_cron(settings.analyze_cron),
        id="self-reflection",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("scheduler started: cron=%s", settings.analyze_cron)

    config = uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
