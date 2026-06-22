#!/bin/bash
# Auto-invalidate the hermes skills prompt snapshot when SKILL.md or
# DESCRIPTION.md files change.
#
# Why this exists:
# hermes builds an LRU cache of the skills-system-prompt at first call
# and persists it to ~/.hermes/.skills_prompt_snapshot.json. The
# snapshot is only invalidated when (skill_name, mtime, size) of any
# tracked SKILL.md/DESCRIPTION.md changes, AND only on the cold-path
# check inside the running process. When a user edits a SKILL.md
# directly (vs. via `hermes skills` CRUD), the running hermes process
# keeps serving the stale in-memory copy until restart.
#
# This watcher closes the gap on the disk side: rm the snapshot file
# so the NEXT hermes restart definitely rebuilds. The running process
# still needs `hermes` restart for changes to apply, but at least the
# user no longer has to remember to rm anything by hand.
#
# Loop: every 60s, build a manifest hash of all skill index files;
# compare to last-seen hash; if changed → rm snapshot + log.

set -e

STATE_DIR="${ORU_STATE_DIR:-$HOME/Library/Application Support/oru-host-state}"
SKILLS_DIR="$HOME/.hermes/skills"
SNAPSHOT="$HOME/.hermes/.skills_prompt_snapshot.json"
HASH_FILE="${STATE_DIR}/skill-cache-manifest-hash"
LOG="${HOME}/Library/Logs/oru-skill-cache-invalidator.log"

mkdir -p "$STATE_DIR" "$(dirname "$LOG")"

if [ ! -d "$SKILLS_DIR" ]; then
    echo "$(date) skills dir missing: $SKILLS_DIR" >> "$LOG"
    exit 0
fi

# Build a stable manifest hash. We use SKILL.md and DESCRIPTION.md files
# only (same set hermes tracks), and stat returns dev/inode/size/mtime
# fields which capture content changes without reading the files.
manifest=$(
    find "$SKILLS_DIR" -type f \( -name SKILL.md -o -name DESCRIPTION.md \) \
        ! -path "*/node_modules/*" \
        ! -path "*/.venv/*" \
        ! -path "*/__pycache__/*" \
        -print0 2>/dev/null \
    | xargs -0 stat -f '%N %z %m' 2>/dev/null \
    | sort
)

current_hash=$(printf '%s' "$manifest" | shasum -a 256 | awk '{print $1}')

prev_hash=""
if [ -s "$HASH_FILE" ]; then
    prev_hash=$(tr -d '[:space:]' < "$HASH_FILE")
fi

if [ "$current_hash" = "$prev_hash" ]; then
    exit 0
fi

# Manifest changed — invalidate. Don't fail if snapshot already gone.
if [ -f "$SNAPSHOT" ]; then
    rm -f "$SNAPSHOT"
    echo "$(date) invalidated $SNAPSHOT (manifest changed)" >> "$LOG"
else
    echo "$(date) manifest changed; snapshot file absent (already cleared)" >> "$LOG"
fi

printf '%s' "$current_hash" > "$HASH_FILE"
