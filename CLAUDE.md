# macwhspr context for Claude

This folder is the macOS counterpart to
[`scdenney/hyperwhspr`](https://github.com/scdenney/hyperwhspr) (Linux). It
documents a voice-to-text setup that triggers on the Globe/Fn key and feeds
OpenAI `gpt-4o-transcribe` → `gpt-4.1-mini` cleanup. Same prompt, same vocab
format, same `/hypr-calibrate` loop as the Linux setup — only the system glue
differs.

## What lives here

- `README.md` - public setup overview, hotkey wiring, known issues log
- `PERFORMANCE.md` - running log of latency work: baselines, optimizations applied, how to re-measure
- `setup.sh` - idempotent installer (also writes the Hammerspoon module + refreshes init.lua's managed block)
- `config/daemon.py` - SIGUSR1-toggled recording/transcription daemon; drives the overlay via `hs -c`. Imports `cleanup` inline and reuses a persistent HTTP/2 `httpx.Client` across calls
- `config/cleanup.py` - cleanup LLM module. `clean(raw, http_client=)` is importable from the daemon (for inline use) and `main()` still works standalone (for `/hypr-calibrate` testing)
- `config/config.json` - daemon configuration example; `"overlay": true/false` toggles the floating pill
- `config/vocab.md` - vocabulary, style, and written-prosody preferences
- `config/com.macwhspr.daemon.plist` - reusable launchd LaunchAgent
- `config/karabiner.json` - Fn/Globe tap → F18 complex modification
- `config/hammerspoon.lua` - bootstrap snippet for `~/.hammerspoon/init.lua` (BEGIN/END markers; setup.sh refreshes it idempotently)
- `config/hammerspoon_macwhspr.lua` - the real module; installed to `~/.hammerspoon/macwhspr.lua`. F18 hotkey, Ctrl-Cmd-V history chooser, and `macwhspr.show(state)` overlay
- `claude/commands/hypr-calibrate.md` - calibration command (paths point at `~/.config/macwhspr/`)

## Key facts

- Active config lives at `~/.config/macwhspr/`
- Credentials live at `~/.local/share/macwhspr/credentials` (JSON, `{"openai":"sk-..."}`)
- Launch agent installed at `~/Library/LaunchAgents/com.macwhspr.daemon.plist`
- Karabiner asset installed at `~/.config/karabiner/assets/complex_modifications/macwhspr.json`
- Hotkey path: Globe tap → Karabiner emits F18 → Hammerspoon catches F18 → `kill -USR1 <daemon-pid>`
- History chooser path: Ctrl-Cmd-V → Hammerspoon `tail -n 20 cleanup_log.jsonl` → `hs.chooser` → on selection, `hs.pasteboard.setContents` + Cmd-V via `hs.eventtap.keyStroke` (50 ms delay so focus returns to the prior app first)
- Overlay path: daemon shells out to `/opt/homebrew/bin/hs -c "macwhspr.show('state')"` (requires `require("hs.ipc")` in init.lua, which the bootstrap snippet provides)
- Overlay states: `recording`, `transcribing`, `done`, `error`, `hide`. Rendered by `hs.canvas` in `~/.hammerspoon/macwhspr.lua`
- Latency path: persistent `httpx.Client(http2=True)` reused across transcribe + cleanup calls; cleanup runs inline (no subprocess). See PERFORMANCE.md for baselines and the optimization log.
- Skip-cleanup heuristic: short (<80 chars), well-formed transcripts bypass the cleanup API and are still logged to `cleanup_log.jsonl` with `"skipped": true`. Toggle via `"skip_short_cleanup": false` in `config.json`.
- No-speech guard: a recording toggled on but left silent is dropped before transcription if its RMS amplitude (measured with `sox … stat`) falls below `"silence_rms_threshold"` (default `0.01`; set `0` to disable). As a backstop, any transcript that comes back empty or just echoes the `whisper_prompt` (or one of its sentences) is also discarded — `gpt-4o-transcribe` parrots the prompt on near-silent audio. Both paths log to `macwhspr.log`, paste nothing, and skip `cleanup_log.jsonl`. The measured RMS is logged every recording so the threshold can be tuned.
- Recording uses `sox -d -r 16000 -c 1 -b 16` (requires `brew install sox`)
- Audio cues: `start_sound`/`stop_sound`/`error_sound` in `config.json` (any `/System/Library/Sounds` name; `audio_feedback:false` mutes). Default stop is `Morse`, not `Pop`, because `Pop` is a soft bloop that blended with the Bluetooth mic→output mode-switch tone (mic released on stop) into an apparent double-beep. `play_sound()` logs and skips if the named file is missing.
- Paste uses `pbcopy` + `osascript` keystroke (needs Accessibility permission)
- When making changes to the daemon: `launchctl kickstart -k gui/$UID/com.macwhspr.daemon`
- After editing `~/.hammerspoon/macwhspr.lua`: menu bar hammer → Reload Config (the bootstrap in init.lua re-requires the module on each reload)
- Logs: `~/Library/Logs/macwhspr.log`

---

# Setup runbook — execute this when the user asks to install on a fresh Mac

Triggers: "set this up", "install macwhspr", "run the setup", "go", or any
similar instruction while in this directory on a Mac.

Walk the user through the runbook below in order. **Pause at the marked
checkpoints and wait for confirmation** before continuing — they cover GUI
interactions, system permission prompts, and an API key paste. Don't batch
steps past a checkpoint.

If anything fails, look at `~/Library/Logs/macwhspr.log` first, then check
the Known issues table in `README.md`. Update that table after fixing
anything new.

## Step 0 — Sanity checks (no user action needed)

Run these in parallel:

- `uname -s` — must print `Darwin`. If it prints `Linux`, stop and tell the user they're on Linux, point them at [`scdenney/hyperwhspr`](https://github.com/scdenney/hyperwhspr).
- `pwd` — must end in `o_macos/macwhspr`
- `command -v brew` — Homebrew must be installed
- `command -v python3`
- `sw_vers -productVersion` — log it for diagnostics

If brew is missing: stop and tell the user to install it from <https://brew.sh>.

## Step 1 — Install Homebrew packages

```bash
brew list sox >/dev/null 2>&1 || brew install sox
brew list --cask karabiner-elements >/dev/null 2>&1 || brew install --cask karabiner-elements
brew list --cask hammerspoon >/dev/null 2>&1 || brew install --cask hammerspoon
```

Run them sequentially (cask installs can prompt).

### ⏸ Checkpoint A — Karabiner kernel extension approval

The first time Karabiner-Elements is installed it requires a kernel extension
allow in **System Settings → Privacy & Security**. Tell the user:

> macOS will show a "System Extension Blocked" warning. Open System Settings
> → Privacy & Security, scroll to the bottom, click **Allow** next to the
> Karabiner-Elements entry, then reboot if prompted.

**Wait for the user to confirm Karabiner is allowed and (if needed) the
machine has rebooted before continuing.**

## Step 2 — Run the installer

```bash
./setup.sh
```

This copies files, creates the venv, installs httpx, drops the plist into
`~/Library/LaunchAgents/`, installs the Karabiner rule asset, writes the
Hammerspoon module to `~/.hammerspoon/macwhspr.lua`, and adds (or refreshes)
the BEGIN/END managed block in `~/.hammerspoon/init.lua`. It is idempotent —
safe to re-run.

Confirm the script prints "Manual steps remaining" at the end. If it errored,
read the output and stop.

## Step 3 — OpenAI API key (macOS Keychain)

### ⏸ Checkpoint B — Ask the user for the key

Ask the user to paste their OpenAI API key (`sk-...`). Once you have it, store
it in the login Keychain — **do not log or echo the key**:

```bash
security add-generic-password -U -s macwhspr -a openai -w '<KEY>'
```

(`-U` updates the entry if it already exists, so this is idempotent.)

Verify the entry exists without printing the value:

```bash
security find-generic-password -s macwhspr -a openai >/dev/null && echo "Keychain entry present"
```

`daemon.py` and `cleanup.py` resolve the key in this order:

1. `OPENAI_API_KEY` env var
2. macOS Keychain entry above
3. `~/.local/share/macwhspr/credentials` (file fallback)

If the user prefers an env var or file instead of Keychain, either path still
works. The file format is `{"openai":"sk-..."}` at mode 600.

The first time the daemon hits the Keychain, macOS will prompt: *"`python`
wants to use your confidential information stored in 'macwhspr' in your
keychain."* The user clicks **Always Allow** so subsequent calls go through
silently. Flag this in Checkpoint F so they don't miss the prompt.

## Step 4 — Disable the macOS default Globe-key action

### ⏸ Checkpoint C — System Settings change

Tell the user:

> Open **System Settings → Keyboard**. Find "Press 🌐 key to:" and set it to
> **Do Nothing**. Otherwise macOS will open the emoji picker or start
> Dictation before Karabiner sees the tap.

Wait for confirmation.

## Step 5 — Enable the Karabiner rule

### ⏸ Checkpoint D — Karabiner GUI

Tell the user:

> Open Karabiner-Elements → **Complex Modifications** → **Add rule**. Find
> "macwhspr: Fn/Globe tap → F18" and click **Enable**.
>
> The first launch will also ask for Input Monitoring and Accessibility
> permissions. Grant both (System Settings → Privacy & Security).

Verify the rule is loaded by reading
`~/.config/karabiner/karabiner.json` and grepping for `macwhspr`:

```bash
grep -c "macwhspr" ~/.config/karabiner/karabiner.json
```

A count of 1+ means the rule is wired up.

Wait for the user to confirm permissions are granted.

## Step 6 — Reload Hammerspoon

`setup.sh` (step 2) already installed `~/.hammerspoon/macwhspr.lua` and
refreshed the BEGIN/END managed block in `~/.hammerspoon/init.lua`. All that's
left is to reload Hammerspoon so the new module + `hs.ipc` listener come up.

### ⏸ Checkpoint E — Reload Hammerspoon and grant Accessibility

Tell the user:

> Click the Hammerspoon menu bar icon (the hammer) → **Reload Config**.
> Grant Accessibility permission when prompted (System Settings → Privacy &
> Security → Accessibility).

Wait for confirmation, then verify the IPC bridge is live:

```bash
/opt/homebrew/bin/hs -c "return type(macwhspr)" 2>&1
```

Expected output: `table`. If it errors with *"can't access Hammerspoon message
port"*, Hammerspoon hasn't reloaded with `require("hs.ipc")` in init.lua —
have the user click Reload Config again, or check `~/.hammerspoon/init.lua`
for the BEGIN/END managed block.

## Step 7 — Start the daemon

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.macwhspr.daemon.plist
sleep 1
launchctl print gui/$UID/com.macwhspr.daemon | head -20
```

Check:
- `state = running` in the `launchctl print` output
- `~/.config/macwhspr/daemon.pid` exists and contains a positive integer
- `~/Library/Logs/macwhspr.log` shows a line like `macwhspr daemon ready (PID …)`

If the daemon is crash-looping, `launchctl print` will show `last exit
reason = …`. Tail the log:

```bash
tail -30 ~/Library/Logs/macwhspr.log
```

## Step 8 — Test the pipeline

### ⏸ Checkpoint F — Live test

Tell the user:

> Open any text field — a Notes window, a browser address bar, anywhere.
> Tap the Globe key. You should hear a `Tink` and see a red pulsing
> **Recording** pill appear near the top of the screen. Say a short sentence.
> Tap Globe again. You should hear a `Pop`, the pill should switch to a blue
> **Transcribing** spinner, then a brief green **Done** check, and within a
> second or two the cleaned transcript pasted at the cursor.
>
> Expect three first-run prompts during this test:
>
> 1. **Keychain access** — *"python wants to use your confidential information…"*
>    Click **Always Allow**.
> 2. **Microphone permission** for the venv Python interpreter. Allow it.
> 3. **Accessibility prompt** for the same process (for the paste keystroke).
>    Allow it.
>
> After approving each, tap Globe twice again to re-test from a clean state.

If transcription succeeds but paste fails: it's almost always Accessibility
permission on the daemon process. Check System Settings → Privacy & Security
→ Accessibility and confirm `python` (the one inside
`~/.local/share/macwhspr/venv/bin/`) is allowed.

If the Globe tap does nothing: in Karabiner-Elements → Event Viewer, watch
what the Globe key emits when tapped. If it shows `keyboard_fn` but not
`f18`, the complex modification isn't active.

## Step 9 — Confirm everything is healthy

Final verification:

```bash
launchctl print gui/$UID/com.macwhspr.daemon | grep state
ls -l ~/.config/macwhspr/daemon.pid
tail -5 ~/Library/Logs/macwhspr.log
```

Report success to the user with: PID, log path, hotkey reminder, and the
location of `vocab.md` for future calibration.

---

## When helping with macwhspr after install

- Check README.md known issues table before diagnosing recurring problems
- Update the known issues table after resolving anything new
- Keep config snapshots in sync with live files after any changes
- `cleanup.py` and `vocab.md` mirror the Linux setup intentionally — keep them aligned
- After editing `daemon.py` or `cleanup.py` in `~/.config/macwhspr/`, restart with `launchctl kickstart -k gui/$UID/com.macwhspr.daemon`
- After editing `~/.hammerspoon/macwhspr.lua`, click the menu bar hammer → Reload Config (no daemon restart needed; the daemon just calls into Hammerspoon)
- After editing files in *this repo*, re-run `./setup.sh` to push changes to `~/.config/macwhspr/` and `~/.hammerspoon/`

## Common failure modes

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Globe tap shows emoji picker | System Settings → Keyboard not set to "Do Nothing" | Checkpoint C |
| Globe tap does nothing in any app | Karabiner rule not enabled, or kext blocked | Checkpoint A, Checkpoint D |
| F18 fires but daemon doesn't start recording | `~/.config/macwhspr/daemon.pid` missing or stale | `launchctl kickstart -k gui/$UID/com.macwhspr.daemon` |
| Recording works, paste fails | Accessibility not granted to venv Python | System Settings → Privacy & Security → Accessibility |
| Audio cues work, no overlay pill appears | `hs.ipc` not loaded OR overlay disabled in config | Reload Hammerspoon; verify with `hs -c "return type(macwhspr)"` (should print `table`). Check `"overlay"` in `~/.config/macwhspr/config.json`. |
| Pill stuck on "Recording" or "Transcribing" | Daemon crashed mid-pipeline before sending `done`/`error`/`hide` | `tail ~/Library/Logs/macwhspr.log`; `launchctl kickstart -k gui/$UID/com.macwhspr.daemon`; `hs -c "macwhspr.show('hide')"` to clear the pill |
| `sox: command not found` in log | sox not installed or PATH wrong | `brew install sox`; confirm `/opt/homebrew/bin` is in the plist's `PATH` env var |
| Transcription error: 401 | Bad/missing OpenAI key | `security find-generic-password -s macwhspr -a openai -w` should print the key. If empty/missing, re-run the `security add-generic-password` from Checkpoint B. |
| First record hangs, no prompts | Keychain access denied silently | Open Keychain Access app → login keychain → search `macwhspr` → double-click → Access Control tab → allow `python` |
| Daemon crash-loops on launch | Usually missing httpx | `~/.local/share/macwhspr/venv/bin/pip install httpx` and `launchctl kickstart -k …` |
