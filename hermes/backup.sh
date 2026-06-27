#!/bin/bash
# Snapshot the LIVE Oru layer of the Hermes runtime (~/.hermes) into the oru
# repo so a `git push` backs it up off-machine.
#
# The repo already version-controls the Oru SOURCE (agents/, hermes/). This
# captures the DEPLOYED/live state of ~/.hermes (skills, persona, cron jobs,
# scripts, non-secret config) plus a restore manifest — so it also catches any
# drift where something was edited directly in ~/.hermes instead of the repo.
#
# Intended to run as the first step of /save, before staging/committing.
#
# SECURITY: explicit ALLOWLIST — never copies ~/.hermes/.env, auth.json, *.db,
# *.lock, logs/, sessions/, caches, or the 1.7GB engine. A secret-value scan
# runs at the end and ABORTS (exit 1) if anything token-shaped slipped in, so a
# leak can never reach the commit.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # oru repo root
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST="$REPO/backup/hermes"

rm -rf "$DEST"
mkdir -p "$DEST/scripts" "$DEST/cron" "$DEST/skills"

# --- allowlisted Oru-layer (NO secrets) — mirrors ~/.hermes layout ---
cp    "$HERMES_HOME/SOUL.md"                 "$DEST/SOUL.md"                  2>/dev/null || true
cp -R "$HERMES_HOME/skills/oru"              "$DEST/skills/oru"               2>/dev/null || true
find  "$HERMES_HOME/scripts" -maxdepth 1 -name '*.sh' -exec cp {} "$DEST/scripts/" \; 2>/dev/null || true
cp    "$HERMES_HOME/cron/jobs.json"          "$DEST/cron/jobs.json"           2>/dev/null || true
cp    "$HERMES_HOME/config.yaml"             "$DEST/config.yaml"              2>/dev/null || true
cp    "$HERMES_HOME/channel_directory.json"  "$DEST/channel_directory.json"   2>/dev/null || true

# --- restore manifest ---
{
  echo "# Oru/Hermes live backup manifest"
  echo
  echo "- generated:    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "- oru commit:   $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo n/a)"
  echo "- hermes home:  $HERMES_HOME"
  echo "- engine:       $(cat "$HERMES_HOME/.install_method" 2>/dev/null || echo unknown) (not backed up — reinstallable)"
  echo
  echo "## Docker agents (source in oru/agents/)"
  ls "$REPO/agents" 2>/dev/null | grep -v '\.DS_Store' | sed 's/^/- /'
  echo
  echo "## Docker images"
  docker images --format '- {{.Repository}}:{{.Tag}} ({{.Size}})' 2>/dev/null | grep '^- oru-' || echo "- (docker unavailable)"
  echo
  echo "## Oru skills (~/.hermes/skills/oru)"
  ls "$HERMES_HOME/skills/oru" 2>/dev/null | sed 's/^/- /'
  echo
  echo "## Cron jobs (~/.hermes/cron/jobs.json)"
  python3 -c "import json;[print('- %s (%s): %s -> %s' % (j['name'], j['id'], j['schedule']['expr'], j.get('deliver'))) for j in json.load(open('$HERMES_HOME/cron/jobs.json'))['jobs']]" 2>/dev/null || echo "- (none)"
  echo
  echo "## Restore"
  echo "1. clone oru, fill secrets/*.env and ~/.hermes/.env from *.env.template"
  echo "2. \`bash hermes/install.sh\`  (deploys SOUL/skills/script, upserts cron)"
  echo "3. \`docker compose up -d --build\`  (brings up all agents)"
} > "$DEST/MANIFEST.md"

# --- safety net: abort if any secret-shaped VALUE reached the backup ---
if grep -rInE 'sk-ant-[A-Za-z0-9_-]{20}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30}|xox[bpoa]-[A-Za-z0-9-]{10}' "$DEST"; then
  echo >&2
  echo "!! ABORT: secret-shaped value(s) found in backup (see above). Nothing should be committed." >&2
  echo "   Fix the allowlist in hermes/backup.sh or remove the offending file, then re-run." >&2
  exit 1
fi

echo "Oru/Hermes live snapshot written to: $DEST"
echo "(secrets excluded; scanned clean). Commit the repo to back it up to GitHub."
