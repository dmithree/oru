"""Publish trip plan to posmotri (Vercel KV store) via HTTP POST.

posmotri is a Next.js app at posmotri-eight.vercel.app backed by Vercel KV.
Source: personal-agent/everything/personal/projects/posmotri/app/api/share/route.ts

API:
  POST /api/share
  body: {content: string (≤1MB), filename?: string, expiresIn?: number, sanitize?: bool}
  201: {url, slug, expiresAt, redactions}

Slugs are server-generated random 6-char ids. We pass the trip filename as
`filename` so the doc has metadata. Returns the share URL for the user.
"""
from __future__ import annotations
import logging
import os
import re
from datetime import date
from typing import Any

import requests

logger = logging.getLogger(__name__)

POSMOTRI_BASE_URL = os.environ.get(
    "POSMOTRI_BASE_URL",
    "https://posmotri-eight.vercel.app",
)
POSMOTRI_SHARE_ENDPOINT = f"{POSMOTRI_BASE_URL}/api/share"


def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9а-яё\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")[:60]


def make_trip_slug(destination: str, start: date, end: date) -> str:
    """Used as the suggested filename hint sent to the API (server still mints its own slug)."""
    if start.month == end.month and start.year == end.year:
        date_part = f"{start.year}-{start.month:02d}-{start.day:02d}-to-{end.day:02d}"
    else:
        date_part = f"{start.year}-{start.strftime('%m-%d')}-to-{end.strftime('%m-%d')}"
    return _slugify(f"{destination}-{date_part}")


def publish_markdown(
    filename: str,
    markdown: str,
    expires_in: int | None = None,
    sanitize: bool = False,
) -> dict[str, Any]:
    """POST the markdown to posmotri /api/share. Returns {ok, url, slug, ...} or {ok: False, error}.

    Args:
      filename: human-readable doc title stored as metadata
      markdown: full content (≤1MB)
      expires_in: seconds until expiry (None = permanent)
      sanitize: ask the server to redact names/emails/phones/medical codes before storing
    """
    if not markdown.strip():
        return {"ok": False, "error": "empty content"}
    if len(markdown) > 1_000_000:
        return {"ok": False, "error": f"content too large ({len(markdown)} > 1_000_000)"}

    body: dict[str, Any] = {"content": markdown, "filename": filename}
    if expires_in:
        body["expiresIn"] = int(expires_in)
    if sanitize:
        body["sanitize"] = True

    try:
        r = requests.post(POSMOTRI_SHARE_ENDPOINT, json=body, timeout=30)
    except Exception as e:
        logger.exception("posmotri POST failed")
        return {"ok": False, "error": f"network: {e}"}

    if r.status_code not in (200, 201):
        return {
            "ok": False,
            "error": f"HTTP {r.status_code}",
            "body": r.text[:400],
        }

    try:
        data = r.json()
    except Exception:
        return {"ok": False, "error": "non-JSON response", "body": r.text[:400]}

    return {
        "ok": True,
        "url": data.get("url"),
        "slug": data.get("slug"),
        "expires_at": data.get("expiresAt"),
        "redactions": data.get("redactions"),
    }
