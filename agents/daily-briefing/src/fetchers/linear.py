"""Linear — stuck/in-progress issues assigned to me.

Phase 0 minimal: list in-progress issues older than 5 days.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone

import requests
from ..config import settings

logger = logging.getLogger(__name__)


def fetch_stuck(days: int = 5, limit: int = 10) -> list[dict]:
    if not settings.linear_api_key:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    query = """
    query Stuck($cutoff: DateTime!, $limit: Int!) {
      issues(
        filter: {state: {type: {eq: "started"}}, updatedAt: {lt: $cutoff}, assignee: {isMe: {eq: true}}}
        first: $limit
        orderBy: updatedAt
      ) {
        nodes { id title updatedAt url state { name } }
      }
    }
    """
    try:
        r = requests.post(
            "https://api.linear.app/graphql",
            headers={"Authorization": settings.linear_api_key, "Content-Type": "application/json"},
            json={"query": query, "variables": {"cutoff": cutoff, "limit": limit}},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", {}).get("issues", {}).get("nodes", [])
        return [{"title": i["title"], "url": i["url"], "state": i["state"]["name"], "updated": i["updatedAt"]} for i in data]
    except Exception:
        logger.exception("Linear fetch failed")
        return []
