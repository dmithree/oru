#!/bin/bash
# Deploy the Oru runtime layer from this repo into the Hermes home (~/.hermes).
#
# The oru repo is the single source of truth; this script DEPLOYS COPIES into
# ~/.hermes. We copy (not symlink) because Hermes' cron sandbox rejects scripts
# whose path resolves outside ~/.hermes/scripts (symlink escape is blocked by
# design — see hermes-agent/cron/scheduler.py). Copying keeps the runtime files
# physically inside ~/.hermes while the canonical, version-controlled originals
# live here in the repo.
#
# Re-run after editing repo files (or after `git pull`) to redeploy.
# Existing REAL files (not symlinks) are backed up to <name>.bak.<timestamp>.

set -euo pipefail

REPO_HERMES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # oru/hermes
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
STAMP="$(date +%Y%m%d%H%M%S)"

backup_if_real() {
    local dst="$1"
    if [ -e "$dst" ] && [ ! -L "$dst" ]; then
        mv "$dst" "$dst.bak.$STAMP"
        echo "backed up: $dst -> $dst.bak.$STAMP"
    else
        rm -rf "$dst"   # drop a stale symlink (e.g. from an older install)
    fi
}

deploy_file() {
    local src="$1" dst="$2"
    mkdir -p "$(dirname "$dst")"
    backup_if_real "$dst"
    cp "$src" "$dst"
    echo "deployed file: $dst"
}

deploy_dir() {
    local src="$1" dst="$2"
    mkdir -p "$(dirname "$dst")"
    backup_if_real "$dst"
    mkdir -p "$dst"
    cp -R "$src/." "$dst/"
    echo "deployed dir:  $dst"
}

# --- shared Oru runtime (persona + skills) ---
deploy_file "$REPO_HERMES/SOUL.md"     "$HERMES_HOME/SOUL.md"
deploy_dir  "$REPO_HERMES/skills/oru"  "$HERMES_HOME/skills/oru"

# --- morning briefing feature ---
# The cron job's `script` field is "morning_briefing_data.sh", resolved against
# ~/.hermes/scripts — so the deploy target stays there regardless of repo layout.
deploy_file "$REPO_HERMES/briefing/morning_briefing_data.sh" "$HERMES_HOME/scripts/morning_briefing_data.sh"
chmod +x "$HERMES_HOME/scripts/morning_briefing_data.sh"

# Upsert the cron-job declaration into the live registry, preserving runtime
# fields the scheduler owns (timestamps, status, completed count, created_at).
JOBS_LIVE="$HERMES_HOME/cron/jobs.json"
DECL="$REPO_HERMES/briefing/cron.json"
mkdir -p "$(dirname "$JOBS_LIVE")"
DECL="$DECL" JOBS_LIVE="$JOBS_LIVE" python3 - <<'PY'
import json, os

decl = json.load(open(os.environ["DECL"], encoding="utf-8"))
live_path = os.environ["JOBS_LIVE"]
try:
    live = json.load(open(live_path, encoding="utf-8"))
except FileNotFoundError:
    live = {"jobs": [], "updated_at": None}

jobs = live.setdefault("jobs", [])

# Runtime fields are owned by the scheduler — never overwrite them from the repo.
RUNTIME = {
    "state", "paused_at", "paused_reason", "next_run_at", "last_run_at",
    "last_status", "last_error", "last_delivery_error", "created_at",
}

existing = next((j for j in jobs if j.get("id") == decl["id"]), None)
if existing is None:
    jobs.append(decl)
    action = "inserted"
else:
    # Keep runtime fields from the live entry; overlay everything else from decl.
    preserved = {k: existing[k] for k in RUNTIME if k in existing}
    # repeat.completed is runtime; keep it if present.
    repeat_completed = (existing.get("repeat") or {}).get("completed")
    existing.update(decl)
    existing.update(preserved)
    if repeat_completed is not None:
        existing.setdefault("repeat", {})
        existing["repeat"]["completed"] = repeat_completed
    action = "updated"

json.dump(live, open(live_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"cron job {decl['id']} ({decl['name']}): {action} in {live_path}")
PY

echo
echo "Done. Files deployed as real copies inside ~/.hermes (cron sandbox-safe)."
echo "Canonical source: $REPO_HERMES  — re-run this script after repo edits."
