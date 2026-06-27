"""Health research — evidence-based lookups against PubMed E-utilities.

Two modes:
  1. on-demand: search(query) — when Дима asks "что говорят исследования про X".
     Returns ranked studies with evidence-tier tags + links. No threshold.
  2. proactive (rare, high bar): high_quality_recent(topics, since_days) — for a
     biweekly cron. Returns ONLY meta-analyses / systematic reviews published in
     the window, across Дима's standing topics. Almost always empty — that's the
     point: it only speaks when something genuinely significant landed.

PubMed E-utilities is free, no API key. We tag evidence tier from the `pubtype`
field so reliability is visible. No medical advice — this surfaces the literature.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_TIMEOUT = 20

# Evidence tiers, strongest first. Used both to tag results and to gate the
# proactive cron (which only accepts the top two tiers).
_TIER_ORDER = [
    ("Meta-Analysis", "meta-analysis"),
    ("Systematic Review", "systematic review"),
    ("Randomized Controlled Trial", "RCT"),
    ("Review", "review"),
    ("Clinical Trial", "clinical trial"),
]
_HIGH_BAR = {"Meta-Analysis", "Systematic Review"}

# Дима's standing health topics for the proactive scan. Narrow on purpose.
STANDING_TOPICS = [
    "heart rate variability training adaptation",
    "sleep quality athletic recovery",
    "zone 2 training mitochondrial endurance",
    "resistance training longevity healthspan",
]


def _get_json(url: str) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "oru-health/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("pubmed fetch failed: %s", exc)
        return None


def _tier(pubtypes: list[str]) -> tuple[str | None, str | None]:
    """Map a study's pubtypes to its strongest evidence tier."""
    if not pubtypes:
        return None, None
    for canonical, label in _TIER_ORDER:
        if canonical in pubtypes:
            return canonical, label
    return None, None


def _esearch(term: str, retmax: int, mindate: str | None = None) -> list[str]:
    params = {
        "db": "pubmed", "term": term, "sort": "date",
        "retmax": str(retmax), "retmode": "json",
    }
    if mindate:
        params["mindate"] = mindate
        params["maxdate"] = date.today().strftime("%Y/%m/%d")
        params["datetype"] = "pdat"
    url = f"{EUTILS}/esearch.fcgi?{urllib.parse.urlencode(params)}"
    d = _get_json(url)
    if not d:
        return []
    return d.get("esearchresult", {}).get("idlist", []) or []


def _esummary(pmids: list[str]) -> list[dict[str, Any]]:
    if not pmids:
        return []
    url = f"{EUTILS}/esummary.fcgi?db=pubmed&id={','.join(pmids)}&retmode=json"
    d = _get_json(url)
    if not d:
        return []
    res = d.get("result", {})
    out = []
    for pid in res.get("uids", []):
        it = res.get(pid, {})
        canonical, label = _tier(it.get("pubtype") or [])
        out.append({
            "pmid": pid,
            "title": (it.get("title") or "").rstrip("."),
            "journal": it.get("fulljournalname") or it.get("source") or "",
            "pubdate": it.get("pubdate") or "",
            "pubtypes": it.get("pubtype") or [],
            "tier": canonical,
            "tier_label": label,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
        })
    return out


def search(query: str, retmax: int = 6) -> dict[str, Any]:
    """On-demand: search PubMed for a query, return ranked studies with tiers.
    Higher evidence tiers float to the top, then by recency (esearch sort=date)."""
    pmids = _esearch(query, retmax=retmax)
    studies = _esummary(pmids)
    tier_rank = {c: i for i, (c, _) in enumerate(_TIER_ORDER)}
    studies.sort(key=lambda s: tier_rank.get(s["tier"], 99))
    return {
        "generated_at": datetime.now().isoformat(),
        "query": query,
        "count": len(studies),
        "studies": studies,
    }


def high_quality_recent(topics: list[str] | None = None, since_days: int = 21) -> dict[str, Any]:
    """Proactive (cron) mode: across standing topics, return ONLY meta-analyses
    and systematic reviews published in the window. High bar by design — usually
    empty, speaks only when something significant lands."""
    topics = topics or STANDING_TOPICS
    mindate = (date.today() - timedelta(days=since_days)).strftime("%Y/%m/%d")
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for topic in topics:
        # bias the query toward high-tier publications at the source
        term = f'({topic}) AND (meta-analysis[pt] OR systematic review[pt])'
        pmids = _esearch(term, retmax=5, mindate=mindate)
        for s in _esummary(pmids):
            if s["pmid"] in seen:
                continue
            seen.add(s["pmid"])
            if s["tier"] in _HIGH_BAR:
                s["matched_topic"] = topic
                hits.append(s)
    return {
        "generated_at": datetime.now().isoformat(),
        "since_days": since_days,
        "topics": topics,
        "count": len(hits),
        "studies": hits,
    }
