#!/bin/bash
# Apple Reminders -> JSON bridge for Docker-isolated agents.
# Container can't reach AppleScript; this script runs on host every 15 min via launchd
# and dumps active reminders to a shared volume that containers mount read-only.

set -e

# Use the canonical (non-symlink) state path so launchd-spawned bash
# doesn't trip macOS TCC on the Documents folder. The same dir is
# symlinked into oru/state so containers see it under the bind mount.
OUT_DIR="${ORU_STATE_DIR:-$HOME/Library/Application Support/oru-host-state}"
OUT_FILE="${OUT_DIR}/reminders.json"
mkdir -p "$OUT_DIR"

# JXA bulk-access dump. Per-reminder property access (rems[j].completed())
# triggers one AppleEvent per call which slows to a crawl past a few
# dozen reminders — 130+ reminders timed out at 60s with the old style.
#
# Bulk pattern: `rems.completed()` returns an ARRAY in a single
# AppleEvent. We pull all properties as arrays then zip in JS. Cuts
# runtime from "times out" to ~16s on a 130-reminder account.
#
# `partial_lists` records which lists failed so the adapter knows the
# snapshot is incomplete and skips pull-direction sync (else missing
# reminders would be wrongly marked done in store).
TMP=$(mktemp)
osascript -l JavaScript <<'EOF' > "$TMP" 2>/dev/null
const Reminders = Application("Reminders");
const result = {
  generated_at: new Date().toISOString(),
  reminders: [],
  partial_lists: [],
};

let lists;
try { lists = Reminders.lists(); }
catch (e) {
  result.error = "lists() failed: " + e.toString();
}

if (lists) {
  for (let i = 0; i < lists.length; i++) {
    let listName = "";
    try { listName = lists[i].name(); } catch (e) { continue; }
    try {
      const rems      = lists[i].reminders;
      const names     = rems.name();
      const completed = rems.completed();
      const dueDates  = rems.dueDate();
      const bodies    = rems.body();
      const prios     = rems.priority();
      const flags     = rems.flagged();
      const n = names.length;
      for (let j = 0; j < n; j++) {
        if (completed[j]) continue;
        let due = null;
        try {
          const d = dueDates[j];
          if (d) due = d.toISOString();
        } catch (e) {}
        result.reminders.push({
          name: names[j],
          list: listName,
          due: due,
          body: bodies[j] || "",
          priority: prios[j] || 0,
          flagged: !!flags[j],
        });
      }
    } catch (e) {
      result.partial_lists.push({ list: listName, error: e.toString() });
    }
  }
}

JSON.stringify(result);
EOF

if [ -s "$TMP" ]; then
  mv "$TMP" "$OUT_FILE"
  chmod 644 "$OUT_FILE"
  echo "$(date) wrote $(wc -c < "$OUT_FILE") bytes to $OUT_FILE"
else
  rm -f "$TMP"
  echo "$(date) JXA returned empty — preserving previous $OUT_FILE"
  exit 1
fi
