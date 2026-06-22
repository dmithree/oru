"""Telegram sendMessage helper.

LLM-generated bodies use full Markdown (#, ---, **bold**) which the legacy
Telegram "Markdown" parser rejects with 400 on any unpaired char. We try
once with parse_mode, and on 400 retry as plain text so the user never
silently loses the message.
"""
import logging
import requests
from .config import settings

logger = logging.getLogger(__name__)

API_TIMEOUT = 10


def _post(text: str, parse_mode: str | None) -> requests.Response:
    payload = {"chat_id": settings.telegram_chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return requests.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        json=payload,
        timeout=API_TIMEOUT,
    )


def send(text: str, parse_mode: str = "Markdown") -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram not configured")
        return False
    try:
        r = _post(text, parse_mode)
        if r.status_code == 400 and parse_mode:
            logger.warning("Telegram 400 with parse_mode=%s: %s; retrying plain", parse_mode, r.text[:300])
            r = _post(text, None)
        r.raise_for_status()
        return True
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", "")
        logger.error("Telegram send failed: %s body=%s", e, body[:300])
        return False
