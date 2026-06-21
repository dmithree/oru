"""Telegram sendMessage helper. Send-only — no polling, no conflict with main Hermes."""
import logging

import requests

from .config import settings

logger = logging.getLogger(__name__)


def send(text: str, parse_mode: str = "Markdown") -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram not configured — skipping send")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": parse_mode,
            },
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception:
        logger.exception("Telegram send failed")
        return False
