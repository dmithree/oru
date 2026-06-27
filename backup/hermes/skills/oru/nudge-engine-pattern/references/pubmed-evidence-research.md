# PubMed evidence-research pattern (health domain, Jun 2026)

Two-mode evidence-based lookup against PubMed E-utilities. Free, no API key.
Lives in `oru-health` as `agents/health/src/research.py` + two endpoints.
Use this recipe when a domain needs authoritative external facts that do NOT
expire weekly (so on-demand pull, not a daily push cron).

## PubMed E-utilities (no key needed)

Base: `https://eutils.ncbi.nlm.nih.gov/entrez/eutils`

1. esearch — get PMIDs for a query:
```
GET /esearch.fcgi?db=pubmed&term=<urlencoded>&sort=date&retmax=6&retmode=json
# date-window the proactive scan:
#   &mindate=YYYY/MM/DD&maxdate=YYYY/MM/DD&datetype=pdat
# bias toward high-tier at the source:
#   term = "(<topic>) AND (meta-analysis[pt] OR systematic review[pt])"
-> esearchresult.idlist[]  (also .count = total)
```
2. esummary — metadata for PMIDs (batch, comma-joined):
```
GET /esummary.fcgi?db=pubmed&id=PMID1,PMID2&retmode=json
-> result[pmid].{title, fulljournalname/source, pubdate, pubtype[]}
```
Send a `User-Agent` header. Timeout ~20s.

## Evidence tiers (from the `pubtype` field)

Strongest first. Tag every result so reliability is visible; gate the proactive
scan to the top two tiers only.
```
Meta-Analysis  >  Systematic Review  >  Randomized Controlled Trial  >  Review  >  Clinical Trial  > (bare) Journal Article
HIGH_BAR = {"Meta-Analysis", "Systematic Review"}
```
Map a study's `pubtype[]` to its strongest matching tier; sort results by tier rank.

## Two modes

- **On-demand** `GET /research?q=<english>&retmax=6` — any tier, ranked by
  evidence then recency. Trigger when Дима asks "что говорят исследования про X",
  "есть ли данные что Y". Translate his query to English first (PubMed is EN).
  Answer with the tier label per study + links. NO "обратись к врачу" — it's
  about data, not diagnosis. If count==0, say so honestly, don't invent.

- **Proactive high-bar** `POST /research-scan?notify=false&since_days=14` — loops
  STANDING_TOPICS, accepts ONLY HIGH_BAR pubtypes in the window. Usually empty.
  Driven by a biweekly Hermes cron that delivers ONLY when count>0 (silent
  otherwise — no "ничего нового" spam). Standing topics live in
  `research.py:STANDING_TOPICS`; edit there to watch a new topic.

## Verified behavior (Jun 2026)

- on-demand "zone 2 mitochondrial adaptation" → 4 studies, reviews sorted above bare studies.
- high-bar scan, 21d window → exactly 1 systematic review (healthspan interventions). Correct: a trickle, not a feed.
