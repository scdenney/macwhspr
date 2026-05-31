# macwhspr performance notes

A running log of latency work on the Mac pipeline. New entries go at the top.

The pipeline has four moving parts per recording: capturing audio (sox), the
OpenAI transcription roundtrip, the OpenAI cleanup roundtrip, and the
`pbcopy + osascript` paste. Audio capture is bounded by the user's speech
duration; the only knobs we control are network/API overhead and how we shell
out around them.

## How to measure

The daemon emits a structured timing line per recording, format:

```
[ts] Timing: audio=A.AAs (K.K KB) | transcribe=T.TTs | cleanup=C.CCs | paste=P.PPPs | post-stop=S.SSs
```

- `audio` — wall time from start_recording until stop (≈ how long the user spoke + ~0.1 s sox shutdown)
- `transcribe` — full OpenAI `gpt-4o-transcribe` POST roundtrip
- `cleanup` — full OpenAI `gpt-4.1-mini` POST roundtrip (or the inline-skip path; near-zero when skipped)
- `paste` — `pbcopy` write + `osascript` keystroke (~5–20 ms)
- `post-stop` — total user-perceived latency from the second Globe tap to text on screen

To collect a fresh sample:

```bash
tail -f ~/Library/Logs/macwhspr.log | grep --line-buffered "Timing:"
```

Then dictate a few short, medium, and long utterances and read off the deltas.

## 2026-05-31 — No-speech guard (silence gate before transcribe)

Not a latency optimization per se, but it touches the hot path, so it's logged
here. Recording toggled on but left silent used to run the full
transcribe + cleanup + paste pipeline and paste back a hallucinated prompt
echo. The daemon now measures the recording's RMS amplitude with
`sox … -n stat` right after the size guard and bails before the transcription
call when it falls below `silence_rms_threshold` (default `0.01`).

- **Cost on real recordings:** one extra `sox … stat` pass, measured at
  **~5 ms** on a 3 s / 96 KB clip (reads the file, no network). Negligible
  next to the ~1.2 s transcribe floor.
- **Saving on silent recordings:** the entire transcribe + cleanup roundtrip
  (~1–2.5 s) plus the wasted API spend, now skipped outright.
- Measured separation (synthetic clips): digital silence RMS ≈ 0.00002,
  near-silent ambient ≈ 0.0017, a noisy room ≈ 0.011, speech-level ≈ 0.18 —
  so `0.01` drops silence/quiet ambient with a wide margin under real speech.
  The measured RMS is logged every recording (`audio RMS … (silence threshold …)`)
  for tuning.
- Backstop: a transcript that still comes back empty or equal to the
  `whisper_prompt` (or one of its sentences) is discarded after transcribe,
  before cleanup/paste.

## 2026-05-20 — Optimization pass 1 (client reuse + inline cleanup + skip heuristic)

### Baseline (pre-change)

Rough numbers from the first day's use, before timing instrumentation. The
log only had second-precision timestamps, so these include the user's speech
duration:

| Recording | start → Raw | Raw → Cleaned | Notes |
| --- | --- | --- | --- |
| 22:24:51–22:25:02 | ~10 s | ~1 s | Long phrase |
| 22:25:16–22:25:21 | ~4 s | ~1 s | "Testing testing testing" |
| 22:27:26–22:27:32 | ~4 s | ~2 s | Short sentence |

Then a single instrumented baseline data point on the unoptimized daemon
(timing patch only, before client-reuse / inline cleanup landed):

```
[22:56:25] Timing: audio=2.63s (76.5 KB) | transcribe=6.44s | cleanup=1.83s | paste=0.406s | post-stop=8.69s
```

That `transcribe=6.44s` is on the high end — typical roundtrips have looked
closer to 2–3 s. Could be a network blip or OpenAI-side congestion. Take it
as the upper edge of normal, not the mean.

User-reported feel before changes: **5–6 s end-to-end is too slow.** Linux
setup (which uses the same OpenAI endpoints) feels noticeably snappier.

### Diagnosis

The two pipelines are architecturally identical (same model, same endpoint,
same `cleanup.py` prompt). Likely macOS-specific overhead:

1. **No HTTP connection reuse.** `httpx.post()` opens a new TCP + TLS
   connection per call. With two API calls per recording, that's ~400–800 ms
   of pure handshake cost being paid every time. `hyprwhspr` (Linux) is a
   long-lived binary that almost certainly keeps a keep-alive connection open.
2. **Cleanup runs as a subprocess.** `subprocess.run([sys.executable, cleanup.py], ...)`
   pays Python interpreter startup + `httpx` import + Keychain lookup on every
   recording — ~150–250 ms of overhead before the API request even starts.
3. **Cleanup runs even when the transcript is already clean.** Many short
   utterances come back from `gpt-4o-transcribe` already capitalized and
   punctuated; the cleanup call is a near-no-op that still costs ~1 s.

### Changes

1. **Persistent `httpx.Client(http2=True)`** for both transcription and
   cleanup. Lazily created, reused across recordings, closed cleanly on
   daemon shutdown. Adds `httpx[http2]` (pulls in `h2`) to the venv.
2. **Inline cleanup.** `cleanup.py` refactored so its `clean()` function
   accepts an optional `http_client=` argument. Daemon imports `cleanup` as a
   module and calls `cleanup.clean(raw, http_client=_client)` directly.
   `cleanup.py` still works as a standalone script for testing or
   `/hypr-calibrate` flows; nothing else needs to change.
3. **Skip-cleanup heuristic.** Before calling the cleanup API, check if the
   raw transcript:
   - Has fewer than 80 characters,
   - Starts with an uppercase letter,
   - Ends with terminal punctuation (`. ! ?`),
   - Contains no filler tokens (`um`, `uh`, `er`, `ah`, `hmm`, `hm`, `mm`, `mhm`, `uh huh`)
     as whole words.
   If all four hold, treat the raw transcript as already clean. We still log
   the pair to `cleanup_log.jsonl` (marked `skipped: true`) so calibration
   sessions can see how often the heuristic fires.

### Expected payoff

Combined: ~500–1000 ms saved per recording on the connection-reuse + inline
path. For short utterances where cleanup is skipped, an additional
800–1500 ms saved. Realistic target: **2.5–4 s post-stop** for typical
dictation, down from ~5–6 s.

### Post-change

Three recordings after the optimization landed:

```
[23:04:47] Rec 1 — "This is recording one."
           audio=3.10s (89.9 KB) | transcribe=1.25s | cleanup=0.00s | paste=0.344s | post-stop=1.61s
           ↑ skip-cleanup heuristic fired (22 chars, capital start, ends in '.', no fillers)

[23:05:04] Rec 2 — "This is recording two. I'm testing to see how this is. Yeah, that's what I'm doing."
           audio=9.46s (289.6 KB) | transcribe=1.23s | cleanup=1.02s | paste=0.263s | post-stop=2.53s

[23:05:35] Rec 3 — "My name is Pad number one and I am showing my app to Pen number two who is sitting next to me."
           audio=6.86s (207.4 KB) | transcribe=1.17s | cleanup=1.21s | paste=0.268s | post-stop=2.66s
```

User-reported feel: **"much better."**

### Comparison

| Metric | Baseline (instrumented, 22:56) | Post-change median |
| --- | --- | --- |
| transcribe | 6.44 s (outlier; ~2 s typical) | **1.20 s** |
| cleanup (when run) | 1.83 s | **1.10 s** |
| cleanup (when skipped) | n/a | **0.00 s** (heuristic) |
| paste | 0.41 s | 0.29 s |
| **post-stop (user-perceived)** | **8.69 s** (outlier; ~5–6 s typical) | **1.6–2.7 s** |

Improvements broken out:
- **Persistent HTTP/2 client** is doing real work. Transcribe roundtrips
  consistently land at ~1.2 s now, vs. observationally 2–3+ s before. Saved
  ~1 s.
- **Inline cleanup** removed subprocess overhead (Python startup + httpx
  import). Roundtrip went from 1.83 s → 1.0–1.2 s — ~0.7 s saved just from
  losing the fork.
- **Skip heuristic** fired on short Rec 1 and saved the entire cleanup
  roundtrip (~1 s). Did *not* fire on Rec 2/3 (length > 80 chars), as
  intended.

### What's left in the budget

Transcribe is now the floor (~1.2 s). To go lower would mean either:
- `gpt-4o-mini-transcribe` — probably ~700–900 ms, slight quality hit on jargon
- Local `whisper.cpp` Metal — sub-second for short clips, no network roundtrip

Both are flagged in the original menu as options 4 and 5. Not needed yet
given the user's "much better" sign-off.

## 2026-05-20 — Optimization pass 2 (fast paste via Hammerspoon)

### Diagnosis

After pass 1, `paste` was still ~0.27 s, almost as slow as the original
`osascript` route. Direct measurements explained why:

```
osascript -e 'return 1'  → 31 ms cold
hs -c "return 1"          → 8 ms
pbcopy <<< test           → 8 ms
```

`hs -c` is much cheaper to invoke than `osascript`. The hidden cost was
`hs.eventtap.keyStroke(modifiers, character[, delay])`, whose **`delay`
parameter defaults to 200000 microseconds (200 ms)** between key-down and
key-up. That accounted for almost the entire paste budget.

### Change

Override the delay to 10000 µs (10 ms). Still plenty of margin for any
app to register a Cmd-V chord; tested across Notes, the browser address
bar, and other common text fields without misses.

```python
# in daemon.paste()
hs -c 'hs.eventtap.keyStroke({"cmd"}, "v", 10000)'
```

Falls back to `osascript` if Hammerspoon isn't running.

### Post-change

| | Before pass 2 | After pass 2 |
| --- | --- | --- |
| `paste` | 0.26–0.31 s | **0.06 s** |
| Effective improvement per recording | — | **~200 ms saved** |

Sample timing lines:

```
[23:20:54] paste=0.064s | post-stop=1.00s   (skip-cleanup hit)
[23:21:09] paste=0.062s | post-stop=2.27s   (cleanup ran)
[23:21:24] paste=0.059s | post-stop=3.07s   (cleanup ran on long clip)
```

The `~1.0 s` post-stop on a short clip (skip-cleanup + fast paste) is
roughly where transcription latency floors out. Further gains would need a
faster transcription model or local inference.

## Open follow-ups (deferred — come back to these)

Captured for future sessions so we don't lose the thread:

- **Vocabulary calibration.** `~/.config/macwhspr/cleanup_log.jsonl` is
  building up real (raw, cleaned) pairs. Run `/hypr-calibrate` once there
  are enough entries to spot patterns; expect it to propose edits to
  `vocab.md` (capitalization rules, recurring proper nouns like
  "Pad Number One", filler-word handling, etc.).
- **`gpt-4o-mini-transcribe` trial.** One-line config change in
  `~/.config/macwhspr/config.json`. Worth a day's trial to see whether
  the latency drop (~300–500 ms) outweighs the accuracy drop on
  technical/academic vocabulary.
- **Local `whisper.cpp` Metal transcription.** Bigger lift (install,
  download a model, rewrite `daemon.transcribe()` to shell out to
  `whisper-cli`). Pays off if privacy matters or for offline use. See
  README "Local transcription option (Apple Silicon)" for the outline.
- **`vocab.md` sync between Mac and the omarchy/Linux box.** Either
  git-track the file or symlink through iCloud/Dropbox so calibration
  done on one machine carries to the other.
