#!/usr/bin/env bash
# macwhspr setup. Idempotent. Run from the repo root: ./setup.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_DIR="$HOME/.config/macwhspr"
DATA_DIR="$HOME/.local/share/macwhspr"
VENV_DIR="$DATA_DIR/venv"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
KARABINER_ASSETS="$HOME/.config/karabiner/assets/complex_modifications"
HAMMERSPOON_DIR="$HOME/.hammerspoon"
PLIST_DEST="$LAUNCH_AGENT_DIR/com.macwhspr.daemon.plist"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
note() { printf "  %s\n" "$1"; }

bold "1/7  Create directories"
mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$LAUNCH_AGENT_DIR" "$KARABINER_ASSETS" "$HAMMERSPOON_DIR"

bold "2/7  Copy scripts and config"
cp "$REPO_DIR/config/daemon.py"          "$CONFIG_DIR/daemon.py"
cp "$REPO_DIR/config/cleanup.py"         "$CONFIG_DIR/cleanup.py"
cp "$REPO_DIR/config/realtime_client.py" "$CONFIG_DIR/realtime_client.py"
chmod +x "$CONFIG_DIR/daemon.py" "$CONFIG_DIR/cleanup.py"
if [ ! -f "$CONFIG_DIR/vocab.md" ]; then
    cp "$REPO_DIR/config/vocab.md" "$CONFIG_DIR/vocab.md"
    note "Seeded $CONFIG_DIR/vocab.md"
else
    note "Kept existing $CONFIG_DIR/vocab.md"
fi
if [ ! -f "$CONFIG_DIR/config.json" ]; then
    sed "s|/Users/YOUR_USER|$HOME|g" "$REPO_DIR/config/config.json" > "$CONFIG_DIR/config.json"
    note "Seeded $CONFIG_DIR/config.json"
else
    note "Kept existing $CONFIG_DIR/config.json"
fi

bold "3/7  Python venv with httpx[http2] + websocket-client"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
# httpx[http2] pulls in h2 so the daemon can multiplex transcription + cleanup
# over a single persistent connection (latency optimization, see PERFORMANCE.md).
# websocket-client backs realtime_client.py (transcription_backend: realtime-ws).
"$VENV_DIR/bin/pip" install --quiet 'httpx[http2]' 'websocket-client>=1.6.0'

bold "4/7  launchd plist"
sed "s|/Users/YOUR_USER|$HOME|g" "$REPO_DIR/config/com.macwhspr.daemon.plist" > "$PLIST_DEST"
note "Wrote $PLIST_DEST"

bold "5/7  Karabiner rule"
cp "$REPO_DIR/config/karabiner.json" "$KARABINER_ASSETS/macwhspr.json"
note "Wrote $KARABINER_ASSETS/macwhspr.json"

bold "6/7  Hammerspoon module"
cp "$REPO_DIR/config/hammerspoon_macwhspr.lua" "$HAMMERSPOON_DIR/macwhspr.lua"
note "Wrote $HAMMERSPOON_DIR/macwhspr.lua"
touch "$HAMMERSPOON_DIR/init.lua"
if grep -q "BEGIN macwhspr (managed)" "$HAMMERSPOON_DIR/init.lua"; then
    python3 - "$HAMMERSPOON_DIR/init.lua" "$REPO_DIR/config/hammerspoon.lua" <<'PY'
import sys, pathlib
init_path = pathlib.Path(sys.argv[1])
snippet = pathlib.Path(sys.argv[2]).read_text()
text = init_path.read_text()
begin = "-- BEGIN macwhspr (managed) --"
end = "-- END macwhspr (managed) --"
pre = text.split(begin, 1)[0].rstrip() + "\n\n"
post = text.split(end, 1)[1].lstrip("\n")
init_path.write_text(pre + snippet.strip() + "\n" + (post and "\n" + post or ""))
PY
    note "Refreshed managed block in $HAMMERSPOON_DIR/init.lua"
elif grep -q "macwhspr" "$HAMMERSPOON_DIR/init.lua"; then
    # Legacy single-marker block from earlier installs: strip everything from the first
    # `-- macwhspr` line to end-of-file (the old appender just concatenated), then append fresh.
    python3 - "$HAMMERSPOON_DIR/init.lua" "$REPO_DIR/config/hammerspoon.lua" <<'PY'
import sys, pathlib
init_path = pathlib.Path(sys.argv[1])
snippet = pathlib.Path(sys.argv[2]).read_text()
lines = init_path.read_text().splitlines(keepends=True)
cut = next((i for i, line in enumerate(lines) if "macwhspr" in line), None)
head = "".join(lines[:cut]) if cut is not None else "".join(lines)
init_path.write_text(head.rstrip() + "\n\n" + snippet.strip() + "\n")
PY
    note "Migrated legacy macwhspr block in $HAMMERSPOON_DIR/init.lua"
else
    printf '\n' >> "$HAMMERSPOON_DIR/init.lua"
    cat "$REPO_DIR/config/hammerspoon.lua" >> "$HAMMERSPOON_DIR/init.lua"
    note "Appended bootstrap to $HAMMERSPOON_DIR/init.lua"
fi

bold "7/7  Check sox"
if ! command -v sox >/dev/null 2>&1; then
    note "sox not found. Install it: brew install sox"
else
    note "sox found at $(command -v sox)"
fi

echo
bold "Manual steps remaining:"
note "1. Store your OpenAI key in the login Keychain (recommended):"
note "     security add-generic-password -U -s macwhspr -a openai -w 'sk-...'"
note "   (alternative: file at $DATA_DIR/credentials with {\"openai\":\"sk-...\"})"
note "2. Open Karabiner-Elements → Complex Modifications → Add rule"
note "   → enable 'macwhspr: Fn/Globe tap → F18'"
note "3. System Settings → Keyboard → 'Press 🌐 key to:' → 'Do Nothing'"
note "   (otherwise macOS swallows the keypress before Karabiner sees it)"
note "4. Reload Hammerspoon (menu bar hammer → Reload Config) so the new"
note "   ~/.hammerspoon/macwhspr.lua module and IPC bridge are loaded."
note "5. Grant Hammerspoon Accessibility access (System Settings → Privacy & Security)"
note "6. Start the daemon:"
note "     launchctl bootstrap gui/\$UID $PLIST_DEST"
note "     launchctl print gui/\$UID/com.macwhspr.daemon"
note "7. First run will prompt for Microphone access — allow it."
echo
bold "Tap the Globe key to record. Tap again to transcribe + paste."
