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

# JXA (JavaScript for Automation) returns clean JSON natively.
TMP=$(mktemp)
osascript -l JavaScript <<'EOF' > "$TMP" 2>/dev/null
const Reminders = Application("Reminders");
const result = {generated_at: new Date().toISOString(), reminders: []};

try {
  const lists = Reminders.lists();
  for (let i = 0; i < lists.length; i++) {
    const listName = lists[i].name();
    const rems = lists[i].reminders.whose({completed: false})();
    for (let j = 0; j < rems.length; j++) {
      const r = rems[j];
      let dueDate = null;
      try { const d = r.dueDate(); if (d) dueDate = d.toISOString(); } catch (e) {}
      let body = "";
      try { body = r.body() || ""; } catch (e) {}
      result.reminders.push({
        name: r.name(),
        list: listName,
        due: dueDate,
        body: body,
        priority: r.priority(),
        flagged: r.flagged()
      });
    }
  }
} catch (e) {
  result.error = e.toString();
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
