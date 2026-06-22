"""Minimal Telegram sendMessage helper (stdlib only).

Used by the scheduled backpressure job to nudge Дима about stale tasks.
Per-agent credentials live in secrets/tasks-hub.env. Returns False on
any failure rather than raising — telegram outages must not crash the
scheduler.
"""
from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings

logger = logging.getLogger(__name__)


def send(text: str, *, parse_mode: str | None = "Markdown") -> bool:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        logger.info("telegram: token/chat_id not configured; skipping send")
        return False
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
        with urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (HTTPError, URLError, TimeoutError) as e:
        logger.warning("telegram send failed: %s", e)
        return False
