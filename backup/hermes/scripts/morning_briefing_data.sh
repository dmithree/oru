#!/bin/bash
# Data layer for the Hermes-owned morning briefing cron job (31bafc8d623c).
#
# The BRIEF belongs entirely to the Hermes agent: it voices the data (SOUL.md)
# and owns delivery. This script is ONLY the data layer — it pulls the already
# aggregated brief from the oru-daily-briefing container (Oura/Strava/tasks/
# Linear/reminders/transcripts) WITHOUT sending anything (notify=false), and
# prints the summary to stdout so Hermes injects it into the cron prompt.
#
# Lives in the oru repo (oru/hermes/briefing/), deployed as a real copy into
# ~/.hermes/scripts/ (see oru/hermes/install.sh — the cron sandbox rejects
# symlinks that resolve outside ~/.hermes/scripts). It MUST NOT depend on the
# personal-agent repo — personal-agent is data-only. The previous version
# shelled into an archived personal-agent python file and failed with exit 2.

set -euo pipefail

BRIEFING_URL="${DAILY_BRIEFING_URL:-http://127.0.0.1:8002}"

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

HTTP_CODE=$(curl -sS -o "$TMP" -w '%{http_code}' --max-time 60 \
    -X POST "${BRIEFING_URL%/}/run-morning?notify=false" || echo "000")

if [ "$HTTP_CODE" != "200" ]; then
    echo "ERROR: daily-briefing container returned HTTP $HTTP_CODE" >&2
    head -c 300 "$TMP" >&2
    exit 1
fi

# Extract brief.summary; a non-empty summary is required.
python3 -c "
import json, sys
d = json.load(open('$TMP'))
summary = (d.get('brief') or {}).get('summary', '').strip()
if not summary:
    sys.stderr.write('empty brief summary')
    sys.exit(1)
sys.stdout.write(summary)
"
