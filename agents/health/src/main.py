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
from pathlib import Path

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI

from .analyzer import build_digest
from .config import settings
from .fetchers import oura, strava
from .telegram import send

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
