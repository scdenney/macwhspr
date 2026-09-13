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
- `transcribe` — commit → final transcript on `realtime-ws` (`gpt-live-transcribe`), or the full POST roundtrip on `rest-api` (`gpt-transcribe`)
- `cleanup` — full cleanup POST roundtrip (`gpt-5.4-mini`; near-zero on the inline-skip path)
- `paste` — `pbcopy` write + `osascript` keystroke (~5–20 ms)
- `post-stop` — total user-perceived latency from the second Globe tap to text on screen

To collect a fresh sample:

```bash
tail -f ~/Library/Logs/macwhspr.log | grep --line-buffered "Timing:"
```

Then dictate a few short, medium, and long utterances and read off the deltas.

## 2026-09-13 — Cleanup model correction: nano → mini

Two days of use and a better-designed benchmark reversed the 2026-09-11
cleanup pick. `gpt-5.4-mini` is faster than `gpt-5.4-nano` at every length
and keeps more of what was actually said. Switched.

The 09-11 comparison was run on three transcripts, and the one that decided
it was a 4,943-character outlier — the longest dictation in the whole log.
Median dictation is 322 characters. Choosing a model on the 99th percentile
and shipping it for the median was the mistake.

Re-run on eight real transcripts sampled from `cleanup_log.jsonl`, stratified
short (100–300 ch) / medium (300–800) / long (1,200+), N=3 each, median
latency per bucket:

| Model | short | medium | long | out/in char ratio |
| --- | --- | --- | --- | --- |
| `gpt-5.4-mini` (now) | **0.75 s** | **0.89 s** | **1.85 s** | 0.98 / 0.99 / 0.88 |
| `gpt-5.4-nano` (was) | 1.17 s | 1.05 s | 2.55 s | 0.95 / 0.96 / 0.89 |
| `gpt-4.1-mini` (before that) | 0.79 s | 1.07 s | 2.80 s | 0.97 / 0.96 / 0.85 |

mini was faster in all eight cases, by 25–40%, and preserves more of the
source at short and medium lengths. The move off `gpt-4.1-mini` still stands
on its own: it was slowest on long input here too.

nano's one apparent advantage — more paragraph breaks — turned out to be a
symptom of restructuring rather than better formatting. Reading the outputs:

- Dictated: "The swirl and R programming, that's fine. You can keep that."
  nano returned "The 'Swirl' and R programming **links** are fine—you can
  keep them." The word "links" is not in the transcript. It is inferable
  from an earlier sentence, but it is an insertion.
- A manuscript note dictated as continuous prose came back from nano as a
  four-item bulleted list, with "What I would like to do" changed to "What I
  would like *you* to do". mini kept it as prose in the dictated wording.

The system prompt permits a list "when the content clearly calls for it", so
nano is not disobeying. It interprets further from the source, and for
dictation that is the wrong direction — the job is to capture what was said.

Cost is not a factor at this volume: roughly four cents a day either way.

Caveat, same as before: N=3 per cell, and the fidelity judgment comes from
reading two cases closely rather than a scored metric. The latency gap is
consistent across all eight, which is what the switch rests on.

## 2026-09-11 — Audit pass: model refresh and the hang that bricked the hotkey

Two months of production logs made the shape of the pipeline clear: with
realtime transcription flat at **0.5–0.75 s**, cleanup is now the whole
latency budget. Sampled from `Timing:` lines, 20 consecutive real dictations:

| Stage | Range | Share of post-stop wait |
| --- | --- | --- |
| transcribe (`realtime-ws`) | 0.51–0.75 s | ~25% |
| cleanup | 1.11–4.42 s | ~72% |
| paste | 0.049–0.077 s | ~3% |

So the optimization target moved from transcription to cleanup.

### Cleanup model swap (the speed change)

Benchmarked on three real transcripts pulled from `cleanup_log.jsonl`
(120 / 600 / 4,943 chars), N=3 each, same system prompt, same persistent
HTTP/2 client, min/median/max:

| Model | short | medium | long (4,943 ch) | long output |
| --- | --- | --- | --- | --- |
| `gpt-4.1-mini` (was) | 1.06/1.29/1.35 | 2.00/2.42/2.50 | **8.77/8.84/12.13** | 3,936 ch |
| `gpt-4.1-nano` | 0.58/0.60/0.63 | 1.00/1.08/1.51 | 2.55/2.58/3.88 | 1,922 ch |
| `gpt-5.4-nano` (now) | 0.83/0.91/0.94 | 1.38/1.51/1.85 | **5.97/6.33/6.38** | 4,594 ch |
| `gpt-5.4-mini` | 0.92/0.93/1.18 | 1.14/1.17/1.30 | 4.36/4.60/6.29 | 4,686 ch |
| `gpt-5.6-luna` | 0.87/1.19/1.37 | 1.86/2.05/2.09 | 6.40/6.55/6.58 | 3,572 ch |

Latency was not the only thing wrong with the old default. Read the outputs,
not just the clock:

- `gpt-4.1-mini` **paraphrased**. On the long case it returned 20% fewer
  characters than it was given and rewrote the dictated first person ("I want
  you to look at the sort of LaTeX formatting…") into stiffer third-party
  prose ("Please review the TeX document formatting I have used…"). It also
  ran into the 12 s ceiling (12.13 s measured), which falls back to pasting
  the raw transcript as one unformatted blob.
- `gpt-4.1-nano` is the fastest option and was rejected anyway: it compressed
  the long case to 1,922 characters — that is summarizing, not reformatting.
- `gpt-5.4-mini` is faster still on the long tail but under-paragraphs (12
  breaks vs 19) and leaves false starts in. **Superseded 2026-09-13: this
  was the wrong read, and it was the wrong read because the whole comparison
  rested on one 4,943-character outlier. See the entry above.**
- `gpt-5.4-nano` preserves length and voice, paragraphs well, and cuts the
  long case from 8.8–12.1 s to ~6.0 s. Chosen. `reasoning_effort` is set to
  `none`; anything else puts thinking tokens directly into the wait.
  `max_tokens` 2048 → 4096 (and renamed to `max_completion_tokens`, which the
  gpt-5 line requires — as it also rejects `temperature`).

Sampled, not exhaustive: N=3 per cell on one network, and the quality calls
above are from reading a handful of outputs rather than a scored eval.

### Transcription: steering, at no latency cost

`gpt-realtime-whisper` → `gpt-live-transcribe`. The headline is accuracy, not
speed — measured commit→final was unchanged, 0.72 s vs 0.71 s on the same
clip, and both models cost $0.017/min. What changed is that the new model
accepts `prompt`, `keywords`, `languages` and `delay` in the session config.
On a `say`-generated clip naming the project's own vocabulary:

| Session | commit→final | Domain terms |
| --- | --- | --- |
| `gpt-realtime-whisper`, bare | 0.72 s | Maclspyr, HyperLisp, Hyperland, Amarky, carabiner |
| `gpt-live-transcribe`, bare | 0.71 s | Maclisper, Hypersper, Hyperland, Amarki, carabiner |
| `gpt-live-transcribe`, prompt + 6 keywords | 0.77 s | **macwhspr, hyprwhspr, Hyprland, omarchy, Karabiner** |

The win is the steering, not the model swap — but the swap is what makes
steering possible. `delay` was swept at the same time: `minimal` returned in
0.64 s but dropped a sentence-final period, `medium` cost 0.75 s with no
visible gain. `low` stays the default.

### The hang (not latency, but it was costing whole recordings)

Found mid-audit with the daemon live-wedged: `state=processing` for five
minutes, every Globe tap logging `Toggle ignored`. `sample(1)` on the process
showed the sender thread in `SSL_write → sock_write → write` on a half-open
socket and the main thread in an untimed lock acquire *inside the signal
handler* — websocket-client takes a per-socket lock around `send_frame`, so
the stuck sender was holding the lock the pipeline's commit needed.

The pipeline used to run on the SIGUSR1 handler's own stack, which is why one
blocked socket write took the hotkey down with it. It now runs on a worker
thread fed by a `SimpleQueue`, the commit goes through the sender thread
rather than the caller, `SO_SNDTIMEO` bounds every write at 5 s, and
`run_forever` sends keepalive pings. Verified by closing the socket out from
under a live client mid-recording: `commit_and_get_text` returns in 6.01 s
against a 6 s timeout, where it previously never returned.

## 2026-07-13 — Realtime streaming transcription (the flat-latency change)

Switched the default backend from batch REST (`gpt-4o-transcribe`, upload the
finished WAV after stop) to OpenAI's Realtime WebSocket
(`gpt-realtime-whisper`, stream raw PCM while recording, commit on stop). New
`realtime_client.py`; `daemon.py` records at 24 kHz raw for this path and
keeps the WAV/REST path behind `"transcription_backend": "rest-api"`.

Why it matters for latency: the batch roundtrip scales with audio length
(upload + transcribe a whole file), while realtime does almost all the work
during the recording itself. Under `realtime-ws` the `transcribe=` component
of the timing line measures commit → final transcript, which is now the whole
transcription wait.

Measured on identical audio (SSH harness: `say`-generated speech at 22.05 kHz,
resampled to 24 kHz PCM, streamed at 1× realtime pacing to mimic mic capture;
key from stdin; N=1 per cell, same network, same afternoon):

| Audio length | Batch REST roundtrip | Realtime post-stop wait |
| --- | --- | --- |
| 6.5 s | 1.67 s | 0.91 s |
| 28.0 s | 2.38 s | 0.98 s |
| 93.9 s | 5.87 s | 0.93 s |

Production logs agree on the batch side: real 100–153 s dictations logged
`transcribe=` 3.57–5.87 s. The realtime wait is flat at ~0.9–1.0 s regardless
of dictation length — on a two-minute dictation that's ~5 s of perceived
latency gone, and the longer the dictation the bigger the win.

Costs and caveats: $0.017/min vs $0.006/min (≈3× per minute, still pennies per
day at dictation volumes); `whisper_prompt` does not apply (no
prompt/vocabulary steering in GA Realtime sessions — vocab correction stays in
`cleanup.py`); the silence gate on this path computes RMS in pure Python over
the in-memory PCM instead of `sox … stat` (no WAV exists), measured at 16 ms
for a 28 s clip and 51 ms for a 94 s one — same order as the ~5 ms sox pass it
replaces and negligible against the ~0.95 s transcription wait.

## 2026-05-31 (evening) — Retune silence threshold; raise cleanup timeout

Real-world use exposed two regressions from the morning's changes:

1. **`silence_rms_threshold` of 0.01 was too high and dropped quiet real
   speech.** The synthetic-clip analysis (next entry) suggested 0.01 was safe,
   but a quieter evening dictation session logged RMS of **0.006–0.009** and
   got discarded as "silent" — six lost recordings before the user flagged it.
   Daytime speech had measured 0.013–0.023, which masked the problem. Lowered
   the default to **0.0025**: true silence is ≤0.0012 and real speech ran
   0.006–0.023, so 0.0025 sits clearly between. Lesson: a fixed RMS gate is
   mic- and level-dependent, so bias it low — dropping speech is far worse than
   transcribing the occasional silent clip, and the prompt-echo backstop still
   catches the silent-hallucination case.
2. **Cleanup's 4 s timeout fell back to raw on long dictations.** A long email
   hit `Cleanup call failed: The read operation timed out` and pasted the
   unformatted transcript — one big blob, no paragraph breaks, no register
   fixes. Raised the cleanup timeout to **12 s** and `max_tokens` 512 → 2048.
   Short cleanups still return in ~1 s; only the slow/long tail uses the extra
   ceiling. Verified: a 430-char run-on blob now cleans in ~1.4 s into four
   properly-broken paragraphs.

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
  near-silent ambient ≈ 0.0017, a noisy room ≈ 0.011, speech-level ≈ 0.18.
  Initial threshold `0.01` — **superseded: real quiet speech later measured
  0.006–0.009 and got clipped, so it was lowered to 0.0025; see the evening
  entry above.** The measured RMS is logged every recording
  (`audio RMS … (silence threshold …)`) for tuning.
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
