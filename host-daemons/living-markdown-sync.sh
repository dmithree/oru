#!/bin/bash
# Sync tasks-hub living markdown view → personal-agent everything/all-tasks.md.
# Container can't write to personal-agent (mounted read-only); this host script
# does the writeback. Runs every hour via launchd.

set -e

ENDPOINT="${TASKS_HUB_URL:-http://127.0.0.1:8004}/render/living?format=markdown"
# Default destination is TCC-friendly. To sync into personal-agent's
# everything/all-tasks.md, set LIVING_DEST in the launchd plist AND
# grant Full Disk Access to /bin/bash in System Settings -> Privacy &
# Security -> Full Disk Access (otherwise writing into ~/Documents is
# blocked for launchd-spawned processes on macOS Sonoma+).
DEST="${LIVING_DEST:-$HOME/Library/Application Support/oru-host-state/all-tasks.md}"
LOG="${LIVING_LOG:-$HOME/Library/Logs/oru-living-markdown-sync.log}"

mkdir -p "$(dirname "$LOG")"

# Hit tasks-hub; only commit the file if we got real content. Empty/error
# responses preserve the previous all-tasks.md so a momentary container
# blip never blanks the file.
TMP=$(mktemp)
HTTP_CODE=$(curl -sS -o "$TMP" -w '%{http_code}' --max-time 15 \
    -H 'Accept: application/json' "$ENDPOINT" || echo "000")

if [ "$HTTP_CODE" != "200" ]; then
    echo "$(date) ERROR: tasks-hub returned HTTP $HTTP_CODE — preserving $DEST" >> "$LOG"
    rm -f "$TMP"
    exit 1
fi

# Extract .markdown out of the JSON envelope. Python is always there on macOS
# (no jq dependency).
MD=$(python3 -c "
import json, sys
try:
    d = json.load(open('$TMP'))
    md = d.get('markdown', '')
    sys.stdout.write(md)
except Exception as e:
    sys.stderr.write(f'parse error: {e}')
    sys.exit(2)
")
RC=$?
rm -f "$TMP"

if [ $RC -ne 0 ] || [ -z "$MD" ]; then
    echo "$(date) ERROR: empty/invalid markdown — preserving $DEST" >> "$LOG"
    exit 2
fi

# Atomic write so a reader never sees a half-written file.
TMP_OUT=$(mktemp)
printf '%s' "$MD" > "$TMP_OUT"
mv "$TMP_OUT" "$DEST"

echo "$(date) wrote $(wc -c < "$DEST") bytes to $DEST" >> "$LOG"
