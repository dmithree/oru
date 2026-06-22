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

# Canonical state path (avoid the ~/Documents symlink so launchd
# doesn't hit TCC). Container reads the same files via the oru/state
# bind mount.
STATE_DIR="${ORU_STATE_DIR:-$HOME/Library/Application Support/oru-host-state}"
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

// Mutations via array-index specifier (rems[j].completed = true) silently
// no-op in JXA. The only reliable write is through the byName-style
// chained specifier: Reminders.lists.byName(L).reminders.byName(N).prop = v.
// We read names from a bulk-fetched array to avoid per-reminder
// AppleEvents (the slow path), then write via byName specifiers.
function collectMatchNames(listName, targetName) {
  const lists = Reminders.lists();
  const matches = [];
  for (let i = 0; i < lists.length; i++) {
    if (listName && lists[i].name() !== listName) continue;
    const list = lists[i];
    // .reminders.whose(...) returns a specifier; calling .name() on it
    // bulk-fetches all matching names in a single AppleEvent. Adding
    // an extra () would materialize specifier objects, and .name() on
    // a JS array is invalid. Keep this exact shape.
    const remsSpec = list.reminders.whose({completed: false});
    const names = remsSpec.name();
    for (let j = 0; j < names.length; j++) {
      if (names[j] === targetName) {
        matches.push({list: list.name(), name: names[j]});
      }
    }
  }
  return matches;
}

function setProp(listName, remName, prop, value) {
  Reminders.lists.byName(listName).reminders.byName(remName)[prop] = value;
}

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
  } else if (cmd.action === "complete_by_match" || cmd.action === "complete") {
    const listName = cmd.list || "";
    const name = cmd.name || "";
    if (cmd.action === "complete" && !(name && listName)) {
      out.ok = false;
      out.error = "complete requires name+list (or use complete_by_match)";
    } else if (!name) {
      out.ok = false;
      out.error = "match requires name";
    } else {
      const targets = collectMatchNames(listName, name);
      let matched = 0;
      for (let k = 0; k < targets.length; k++) {
        try {
          setProp(targets[k].list, targets[k].name, "completed", true);
          matched++;
        } catch (e) {
          // byName resolves to the first match; if there are dups we
          // can only mark the first one. Skip subsequent dup attempts.
          break;
        }
      }
      out.ok = matched > 0;
      out.matched = matched;
      out.candidates = targets.length;
    }
  } else if (cmd.action === "snooze") {
    if (cmd.name && cmd.list && cmd.until) {
      const targets = collectMatchNames(cmd.list, cmd.name);
      let touched = 0;
      for (let k = 0; k < targets.length; k++) {
        try {
          setProp(targets[k].list, targets[k].name, "dueDate", new Date(cmd.until));
          touched++;
        } catch (e) {
          break;
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
