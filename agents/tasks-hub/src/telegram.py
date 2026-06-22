"""Minimal Telegram sendMessage helper (stdlib only).

Used by the scheduled backpressure job to nudge Дима about stale tasks.
Per-agent credentials live in secrets/tasks-hub.env. Returns False on
any failure rather than raising — telegram outages must not crash the
scheduler.

LLM-generated bodies use full Markdown (#, ---, **bold**) which the legacy
Telegram "Markdown" parser rejects with 400 on any unpaired char. We try
once with parse_mode, and on 400 retry as plain text so the user never
silently loses the message.
"""
from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings

logger = logging.getLogger(__name__)

API_TIMEOUT = 10


def _post(text: str, parse_mode: str | None) -> tuple[int, str]:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    body: dict = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    req = Request(
        url, method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=API_TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)


def send(text: str, *, parse_mode: str | None = "Markdown") -> bool:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        logger.info("telegram: token/chat_id not configured; skipping send")
        return False
    try:
        status, body = _post(text, parse_mode)
        if status == 400 and parse_mode:
            logger.warning("Telegram 400 with parse_mode=%s: %s; retrying plain", parse_mode, body[:300])
            status, body = _post(text, None)
        if 200 <= status < 300:
            return True
        logger.error("telegram send failed: status=%s body=%s", status, body[:300])
        return False
    except (URLError, TimeoutError) as e:
        logger.warning("telegram send failed: %s", e)
        return False
