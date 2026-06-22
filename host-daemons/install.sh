#!/bin/bash
# Install host-daemon scripts to a TCC-friendly location and load them
# via launchd.
#
# macOS Sonoma+ blocks launchd-spawned processes from executing scripts
# under ~/Documents (TCC restriction on Documents folder). So we COPY
# the scripts into ~/Library/Application Support/oru-host/ which has no
# such restriction, and point the plists there.
#
# Run from anywhere:   bash oru/host-daemons/install.sh
# To uninstall:        bash oru/host-daemons/install.sh uninstall
#
# After install:
#   - First osascript run will trigger TCC prompts for Reminders.app
#     (allow them; you'll do this 1-2 times)
#   - Logs go to ~/Library/Logs/oru-*.log
#   - To re-install after editing scripts in this repo, just run again.

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/oru-host"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(
    com.oru.reminders-bridge
    com.oru.reminders-commands-watcher
    com.oru.living-markdown-sync
    com.oru.skill-cache-invalidator
)

uninstall() {
    for label in "${LABELS[@]}"; do
        if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
            echo "bootout $label"
            launchctl bootout "gui/$UID/$label" 2>/dev/null || true
        fi
        rm -f "$AGENTS/$label.plist"
    done
    rm -rf "$DEST"
    echo "uninstalled. (logs at ~/Library/Logs/oru-*.log preserved)"
}

if [ "$1" = "uninstall" ]; then
    uninstall
    exit 0
fi

mkdir -p "$DEST"
mkdir -p "$AGENTS"
mkdir -p "$HOME/Library/Logs"

for label in "${LABELS[@]}"; do
    if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
        echo "bootout existing $label"
        launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    fi
done

cp "$REPO_DIR/reminders-bridge.sh"            "$DEST/"
cp "$REPO_DIR/reminders-commands-watcher.sh"  "$DEST/"
cp "$REPO_DIR/living-markdown-sync.sh"        "$DEST/"
cp "$REPO_DIR/skill-cache-invalidator.sh"     "$DEST/"
chmod +x "$DEST/"*.sh

for entry in \
    "reminders-bridge:900" \
    "reminders-commands-watcher:60" \
    "living-markdown-sync:3600" \
    "skill-cache-invalidator:60" ; do
    label="${entry%%:*}"
    interval="${entry##*:}"
    plist="$AGENTS/com.oru.$label.plist"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.oru.$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$DEST/$label.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>$interval</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/oru-$label.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/oru-$label.err.log</string>
</dict>
</plist>
EOF
    plutil -lint "$plist" >/dev/null
    echo "wrote $plist"
done

for label in "${LABELS[@]}"; do
    plist="$AGENTS/$label.plist"
    if launchctl bootstrap "gui/$UID" "$plist" 2>/dev/null; then
        echo "bootstrap $label OK"
    else
        echo "bootstrap $label FAILED (already loaded?)"
    fi
done

echo
echo "Installed. Verify:"
echo "  launchctl list | grep com.oru"
echo "  tail -f ~/Library/Logs/oru-reminders-bridge.log"
echo
echo "First osascript run will trigger TCC prompts for Reminders.app —"
echo "allow them. If they don't appear within a few minutes, force one:"
echo "  bash '$DEST/reminders-bridge.sh'"
