#!/bin/bash
# Apple Reminders command queue applier (idea 5 — bidirectional bridge).
#
# tasks-hub container appends JSON commands to
# state/reminders-commands.jsonl. This script runs on host every minute
# via launchd, pops new lines from the cursor onward, applies them via
# JXA (osascript), and writes results to
# state/reminders-commands-log.jsonl so the container can confirm.
#
# Supported actions:
#   create               — make a new reminder in target list (auto-creating list if needed)
#   complete_by_match    — find by (name, list) and mark completed
#   snooze               — push due date forward
#
# Cursor is a single integer (byte offset into the queue file) stored
# in state/reminders-commands-cursor so we never re-apply commands
# across restarts.

set -e

STATE_DIR="/Users/dmitry/Documents/GitHub/oru/state"
QUEUE="${STATE_DIR}/reminders-commands.jsonl"
CURSOR="${STATE_DIR}/reminders-commands-cursor"
LOG="${STATE_DIR}/reminders-commands-log.jsonl"

mkdir -p "$STATE_DIR"
touch "$LOG"

if [ ! -f "$QUEUE" ]; then
    exit 0
fi

# Read cursor (default 0). Guard against missing/empty/non-numeric.
OFFSET=0
if [ -s "$CURSOR" ]; then
    raw=$(cat "$CURSOR" 2>/dev/null | tr -d '[:space:]')
    case "$raw" in
        ''|*[!0-9]*) OFFSET=0 ;;
        *) OFFSET="$raw" ;;
    esac
fi

QUEUE_SIZE=$(stat -f%z "$QUEUE")
if [ "$OFFSET" -ge "$QUEUE_SIZE" ]; then
    # nothing new
    exit 0
fi

# Pop new lines into a temp file. tail -c starts at OFFSET+1 (1-based).
TAIL_START=$((OFFSET + 1))
TMP=$(mktemp)
tail -c +"$TAIL_START" "$QUEUE" > "$TMP"

PROCESSED_BYTES=0

apply_one() {
    local cmd_json="$1"

    # JXA script applies the command and prints {ok,result} JSON.
    local result
    result=$(osascript -l JavaScript <<EOF 2>&1
const Reminders = Application("Reminders");
Reminders.includeStandardAdditions = true;

const cmd = $cmd_json;
const out = {cmd_id: cmd.cmd_id || null, action: cmd.action, ok: false};

try {
  if (cmd.action === "create") {
    const listName = cmd.list || "AI";
    let targetList = null;
    const lists = Reminders.lists();
    for (let i = 0; i < lists.length; i++) {
      if (lists[i].name() === listName) { targetList = lists[i]; break; }
    }
    if (!targetList) {
      targetList = Reminders.Reminder().make({new: "list", at: Reminders, withProperties: {name: listName}});
    }
    const props = {name: cmd.name};
    if (cmd.body) props.body = cmd.body;
    if (cmd.due)  props.dueDate = new Date(cmd.due);
    const newRem = Reminders.Reminder(props);
    targetList.reminders.push(newRem);
    out.ok = true;
    out.created_name = cmd.name;
    out.list = listName;
  } else if (cmd.action === "complete_by_match") {
    const listName = cmd.list || "";
    const name = cmd.name || "";
    let matched = 0;
    const lists = Reminders.lists();
    for (let i = 0; i < lists.length; i++) {
      if (listName && lists[i].name() !== listName) continue;
      const rems = lists[i].reminders.whose({completed: false})();
      for (let j = 0; j < rems.length; j++) {
        if (rems[j].name() === name) {
          rems[j].completed = true;
          matched++;
        }
      }
    }
    out.ok = matched > 0;
    out.matched = matched;
  } else if (cmd.action === "complete") {
    // Without an ext_id matcher on the host side, fall back to
    // (name,list) embedded in payload. If absent, mark unsupported.
    if (cmd.name && cmd.list) {
      const lists = Reminders.lists();
      let matched = 0;
      for (let i = 0; i < lists.length; i++) {
        if (lists[i].name() !== cmd.list) continue;
        const rems = lists[i].reminders.whose({completed: false})();
        for (let j = 0; j < rems.length; j++) {
          if (rems[j].name() === cmd.name) {
            rems[j].completed = true;
            matched++;
          }
        }
      }
      out.ok = matched > 0;
      out.matched = matched;
    } else {
      out.ok = false;
      out.error = "complete requires name+list (or use complete_by_match)";
    }
  } else if (cmd.action === "snooze") {
    if (cmd.name && cmd.list && cmd.until) {
      const lists = Reminders.lists();
      let touched = 0;
      for (let i = 0; i < lists.length; i++) {
        if (lists[i].name() !== cmd.list) continue;
        const rems = lists[i].reminders.whose({completed: false})();
        for (let j = 0; j < rems.length; j++) {
          if (rems[j].name() === cmd.name) {
            rems[j].dueDate = new Date(cmd.until);
            touched++;
          }
        }
      }
      out.ok = touched > 0;
      out.touched = touched;
    } else {
      out.ok = false;
      out.error = "snooze requires name+list+until";
    }
  } else {
    out.error = "unknown action: " + cmd.action;
  }
} catch (e) {
  out.error = e.toString();
}

JSON.stringify(out);
EOF
)
    # Strip any AppleEvent warnings that bleed into stderr-on-stdout
    result=$(echo "$result" | tail -n 1)
    if [ -z "$result" ]; then
        result='{"ok":false,"error":"empty JXA output"}'
    fi
    local stamp
    stamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "{\"applied_at\":\"$stamp\",\"input\":$cmd_json,\"result\":$result}" >> "$LOG"
}

# Iterate over each new command line. Track processed bytes (= line
# length + newline) so we can advance the cursor exactly.
while IFS= read -r line; do
    line_len=${#line}
    if [ -z "$line" ]; then
        PROCESSED_BYTES=$((PROCESSED_BYTES + 1))
        continue
    fi
    apply_one "$line"
    # +1 for the trailing newline that read consumed
    PROCESSED_BYTES=$((PROCESSED_BYTES + line_len + 1))
done < "$TMP"

rm -f "$TMP"

NEW_CURSOR=$((OFFSET + PROCESSED_BYTES))
# Clamp cursor to file size in case we wrote bytes past EOF
if [ "$NEW_CURSOR" -gt "$QUEUE_SIZE" ]; then
    NEW_CURSOR=$QUEUE_SIZE
fi
echo -n "$NEW_CURSOR" > "$CURSOR"
echo "$(date) processed $PROCESSED_BYTES bytes, cursor now $NEW_CURSOR/$QUEUE_SIZE"
