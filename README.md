# macwhspr — Mac voice-to-text setup

This is the macOS counterpart to
[`scdenney/hyperwhspr`](https://github.com/scdenney/hyperwhspr) (the Linux
setup). It is not an application or package — it is a reproducible
configuration pattern: Globe-key triggered voice recording, OpenAI
transcription, post-transcription LLM cleanup, and a calibration loop for
vocabulary, style, and written prosody. Same pipeline as the Linux setup,
system glue swapped for Mac-native pieces (Karabiner-Elements, Hammerspoon,
launchd, sox).

## Pipeline

```
Globe tap
   │  (Karabiner-Elements: Fn tap → F18)
   ▼
Hammerspoon  ◄────────────────────────────┐
   │  (kill -USR1 <daemon pid>)           │ overlay updates
   ▼                                      │ via `hs -c` IPC
macwhspr daemon  ──►  sox records to wav  │
       │
       │   persistent httpx.Client(http2=True), reused for both calls
       ▼
                 ──►  OpenAI gpt-4o-transcribe
                 ──►  (cleanup inline; skipped for short, well-formed text)
                 ──►  pbcopy → osascript paste
                 ──►  notify_overlay() ───┘
```

Tap once: start recording. Tap again: stop, transcribe, clean (or skip), paste
at the cursor. A small floating pill (rendered by Hammerspoon's `hs.canvas`)
shows the current state — red pulsing **Recording**, blue **Transcribing**
spinner, green **Done** check.

Latency choices live in `PERFORMANCE.md`. The short version: cleanup runs in
the same Python process as the daemon, and both API calls share a single
HTTP/2 connection that stays open between recordings.

## Why this setup over an existing app

Mac alternatives like VoiceInk, Whispo, SuperWhisper handle the recording and
hotkey for you, but each ships its own vocabulary and prompt system. This setup
keeps `cleanup.py` and `vocab.md` in plain files that mirror the Linux side, so
the `/hypr-calibrate` calibration loop and any vocabulary work transfer across
machines.

## What gets installed

| File on disk | Source in this repo |
| --- | --- |
| `~/.config/macwhspr/daemon.py` | `config/daemon.py` |
| `~/.config/macwhspr/cleanup.py` | `config/cleanup.py` |
| `~/.config/macwhspr/config.json` | `config/config.json` |
| `~/.config/macwhspr/vocab.md` | `config/vocab.md` (only if missing) |
| `~/.local/share/macwhspr/venv/` | Python 3 venv with `httpx` |
| `~/.local/share/macwhspr/credentials` | Created by hand, contains OpenAI key |
| `~/Library/LaunchAgents/com.macwhspr.daemon.plist` | `config/com.macwhspr.daemon.plist` |
| `~/.config/karabiner/assets/complex_modifications/macwhspr.json` | `config/karabiner.json` |
| `~/.hammerspoon/macwhspr.lua` | `config/hammerspoon_macwhspr.lua` |
| `~/.hammerspoon/init.lua` (bootstrap snippet, BEGIN/END markers) | `config/hammerspoon.lua` |
| `~/.claude/commands/hypr-calibrate.md` | `claude/commands/hypr-calibrate.md` |

## Prerequisites

```bash
# Homebrew packages
brew install sox
brew install --cask karabiner-elements
brew install --cask hammerspoon
```

Python 3.9+ from the system or Homebrew is fine. `setup.sh` creates its own
venv so the system Python stays clean.

## Install

```bash
cd o_macos/macwhspr
./setup.sh
```

Then the manual steps the script prints at the end. In order:

### 1. Store your OpenAI API key

Recommended: macOS Keychain.

```bash
security add-generic-password -U -s macwhspr -a openai -w 'sk-...'
```

`-U` makes it idempotent (updates if the entry already exists). The first
time the daemon reads it, macOS shows a Keychain prompt — click **Always
Allow** so subsequent calls run silently.

Alternative paths (used as fallbacks in this order: env → Keychain → file):

```bash
# Option B: env var in the launchd plist's EnvironmentVariables dict
# Option C: file (Linux-compatible format)
printf '{"openai":"sk-..."}\n' > ~/.local/share/macwhspr/credentials
chmod 600 ~/.local/share/macwhspr/credentials
```

### 2. Tell macOS to stop intercepting the Globe key

System Settings → Keyboard → "Press 🌐 key to:" → **Do Nothing**.

Without this, macOS opens the emoji picker, switches input source, or starts
Apple Dictation before Karabiner sees the keypress.

### 3. Enable the Karabiner rule

Open Karabiner-Elements → Complex Modifications → Add rule → enable
**"macwhspr: Fn/Globe tap → F18"**.

The first time you launch Karabiner it will ask for accessibility and input
monitoring permissions, plus a kernel extension allow in System Settings →
Privacy & Security. Grant all three, then re-open Karabiner.

### 4. Wire up Hammerspoon

`setup.sh` writes `~/.hammerspoon/macwhspr.lua` and appends (or refreshes) a
small bootstrap block in `~/.hammerspoon/init.lua` that loads it. After
running the installer, click the Hammerspoon menu bar icon → **Reload Config**
so the new module and the `hs.ipc` CLI bridge come up.

Grant Hammerspoon Accessibility permission when prompted (System Settings →
Privacy & Security → Accessibility). The bootstrap calls `require("hs.ipc")`
so the daemon can drive the overlay via `hs -c "macwhspr.show('...')"`.

### 5. Start the daemon

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.macwhspr.daemon.plist
launchctl print gui/$UID/com.macwhspr.daemon | head
```

The first recording will trigger the macOS microphone permission prompt for
the Python interpreter inside the venv. Allow it.

## Daily use

- **Tap Globe** — start recording. You'll hear a soft `Tink` and a red pulsing
  **Recording** pill appears near the top of the screen.
- **Tap Globe again** — stop, transcribe, clean, paste at the cursor. You'll
  hear a short `Morse` beep, the pill switches to a blue **Transcribing**
  spinner, then a brief green **Done** check before fading. If anything fails,
  you'll hear `Funk`, the pill flashes a red **Error**, and the error will be
  in the log.
- **Ctrl-Cmd-V** — open a chooser of the last ~20 transcripts (read from
  `cleanup_log.jsonl`). Pick one to paste it into the focused field; Esc to
  cancel. Useful when the original paste landed in the wrong window, or when
  you want to reach back further than the system clipboard remembers.

To turn the overlay off (audio cues only), set `"overlay": false` in
`~/.config/macwhspr/config.json` and restart the daemon
(`launchctl kickstart -k gui/$UID/com.macwhspr.daemon`).

The three cues are configurable too: set `"start_sound"`, `"stop_sound"`, or
`"error_sound"` to any name from `/System/Library/Sounds` (e.g. `Tink`,
`Morse`, `Pop`, `Bottle`, `Glass`, `Submarine`), or `"audio_feedback": false`
to mute them entirely. The stop cue defaults to the crisp, short `Morse`
rather than `Pop`: on Bluetooth headphones macOS plays a device mode-switch
tone as the mic releases, and the softer `Pop` blended with it into what
sounded like a doubled beep.

Tail the log:

```bash
tail -f ~/Library/Logs/macwhspr.log
```

Restart the daemon after editing `daemon.py` or `config.json`:

```bash
launchctl kickstart -k gui/$UID/com.macwhspr.daemon
```

Disable temporarily without uninstalling:

```bash
launchctl bootout gui/$UID/com.macwhspr.daemon
# or, to keep the daemon but pause the hotkey, in Karabiner-Elements:
# Misc → set variable macwhspr_disabled = 1
```

## OpenAI API setup

The example config uses OpenAI's speech-to-text endpoint with
`gpt-4o-transcribe`. OpenAI's audio docs describe the endpoint and supported
models:
<https://platform.openai.com/docs/guides/speech-to-text>.

The relevant config block in `~/.config/macwhspr/config.json`:

```json
{
  "transcription_url": "https://api.openai.com/v1/audio/transcriptions",
  "transcription_model": "gpt-4o-transcribe",
  "whisper_prompt": "Transcribe accurately. ..."
}
```

Cleanup defaults to `gpt-4.1-mini` via the OpenAI Chat Completions API. The
model and endpoint are set as environment variables in the launchd plist:

```xml
<key>MACWHSPR_CLEANUP_MODEL</key><string>gpt-4.1-mini</string>
<key>MACWHSPR_LLM_API_URL</key><string>https://api.openai.com/v1/chat/completions</string>
```

Point either at a local OpenAI-compatible endpoint to run cleanup locally.

## Cleanup prompt

`config/cleanup.py` is the same constrained reformatter prompt as the Linux
version. It tells the model to behave as a text reformatter, not as an
assistant — never to answer, summarize, or acknowledge.

What it fixes:

- punctuation
- capitalization
- grammar
- filler words
- false starts
- speech disfluencies
- paragraph breaks
- list formatting when content clearly calls for it
- professional register

Change global behavior in `SYSTEM_PROMPT` inside `config/cleanup.py`. Change
recurring vocabulary, formatting, and prosody preferences in `config/vocab.md`.

## Calibration loop

Every cleanup is logged to:

```text
~/.config/macwhspr/cleanup_log.jsonl
```

In Claude Code on the Mac, run:

```text
/hypr-calibrate
```

The command reviews recent log entries, identifies repeated vocabulary and
style patterns, and proposes edits to `~/.config/macwhspr/vocab.md`.

## Hotkey alternative without Karabiner

If you don't want to install Karabiner-Elements, skip steps 2 and 3 above and
change the Hammerspoon binding to a regular shortcut. Edit the `hs.hotkey.bind`
line in `~/.hammerspoon/macwhspr.lua`:

```lua
-- was: hs.hotkey.bind({}, "F18", toggleRecording)
hs.hotkey.bind({"cmd", "alt"}, "d", toggleRecording)
```

This mirrors the Linux `SUPER+ALT+D` default. The trade-off is you lose the
single-key Globe trigger; everything else (including the overlay) still works.

Note that this edit is to the installed module, not the repo. Re-running
`setup.sh` will overwrite it. To make the change permanent, edit
`config/hammerspoon_macwhspr.lua` in the repo too.

## Local transcription option (Apple Silicon)

The daemon currently calls the OpenAI transcription API. For local
transcription on Apple Silicon, the simplest swap is `whisper.cpp` with its
Core ML / Metal build. The daemon's `transcribe()` function would shell out to
`./main -m models/ggml-base.en.bin -f recording.wav -nt` instead of POSTing to
OpenAI. Not wired up here; flagged as a known follow-up.

## Known issues and fixes

| Date | Issue | Fix |
| --- | --- | --- |
| 2026-05-20 | Initial Mac port | — |
| 2026-05-20 | Added `hs.canvas` recording overlay; daemon drives state via `hs -c` IPC | Bootstrap now `require("hs.ipc")`; module split into `~/.hammerspoon/macwhspr.lua` |
| 2026-05-20 | Latency tuning pass 1: cleanup ran as a subprocess and each API call opened a fresh TCP+TLS connection | Persistent `httpx.Client(http2=True)`, inline cleanup, skip-cleanup heuristic for short well-formed transcripts. Details in `PERFORMANCE.md` |
| 2026-05-21 | No way to re-paste a previous transcript when the original paste landed in the wrong window | Added `Ctrl-Cmd-V` chooser in `hammerspoon_macwhspr.lua` over the last 20 entries in `cleanup_log.jsonl` |
| 2026-05-31 | Toggling recording on without saying anything pasted the prompt text back (`gpt-4o-transcribe` hallucinates the `whisper_prompt` on silence) | Daemon drops silent clips (RMS below `silence_rms_threshold`, default `0.0025`) before transcribing, and discards any transcript that is empty or just echoes the prompt or one of its sentences. Tune via `silence_rms_threshold` in `config.json`; the measured RMS is logged each recording |
| 2026-05-31 | Stop/transcribe tap sounded like a doubled, overlapping beep (most noticeable on AirPods/Beats) | Not a macwhspr bug — macOS plays a Bluetooth mic→output mode-switch tone as the mic releases, layering on the soft `Pop`. Changed the default `stop_sound` to the crisper, shorter `Morse` so it no longer blends, and made `start_sound`/`stop_sound`/`error_sound` configurable in `config.json` |
| 2026-05-31 | Silence guard clipped quiet real speech as "silent" (nothing pasted); long email dictations pasted as one unformatted blob | `silence_rms_threshold` of `0.01` sat inside real speech levels — quiet dictation logged 0.006–0.009 RMS and got dropped. Lowered the default to `0.0025` (true silence ≤0.0012, speech 0.006–0.02). Separately, the 4 s cleanup timeout fell back to raw text on long dictations: raised it to 12 s and `max_tokens` 512→2048 so long emails get formatted instead of blobbed |

## Relationship to the Linux setup

| Piece | Linux ([`scdenney/hyperwhspr`](https://github.com/scdenney/hyperwhspr)) | Mac (this repo) |
| --- | --- | --- |
| Voice daemon | `hyprwhspr` (upstream binary) | `daemon.py` in this repo |
| Service manager | systemd user service | launchd LaunchAgent |
| Hotkey | Hyprland `bind = SUPER ALT, D` | Karabiner Fn→F18 + Hammerspoon |
| Recording | hyprwhspr-bundled audio | `sox -d` |
| Clipboard / paste | `wl-copy` + virtual keyboard | `pbcopy` + `osascript` |
| Recording overlay | hyprwhspr-bundled OSD | `hs.canvas` pill via `hs.ipc` |
| Cleanup hook | `cleanup.py` (OpenAI) | `cleanup.py` (OpenAI, identical prompt) |
| Vocabulary | `~/.config/hyprwhspr/vocab.md` | `~/.config/macwhspr/vocab.md` |
| Calibration | `/hypr-calibrate` | `/hypr-calibrate` (same logic, Mac paths) |

`cleanup.py` and `vocab.md` follow the same format on both sides. Copy or
git-sync between machines as needed.
