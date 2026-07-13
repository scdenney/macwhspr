#!/usr/bin/env python3
"""
macwhspr daemon: SIGUSR1 toggles recording.

State machine:
  idle      -- SIGUSR1 -->  recording  (spawns sox)
  recording -- SIGUSR1 -->  processing (stops sox, transcribes, cleans, pastes)
  processing - done -->     idle

PID is written to ~/.config/macwhspr/daemon.pid. Hammerspoon (or any other
trigger) signals the PID with SIGUSR1 to toggle.

Two transcription backends (config "transcription_backend"):
  "realtime-ws" (default) - streams audio to OpenAI's Realtime WebSocket API
      (gpt-realtime-whisper) as sox captures it, so transcription is mostly
      done by the time the user stops talking. See realtime_client.py.
  "rest-api" - the original batch path: record a full WAV, then POST it to
      /v1/audio/transcriptions (gpt-4o-transcribe) after recording stops.
"""

import array
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

CONFIG_DIR = Path.home() / ".config/macwhspr"
DATA_DIR = Path.home() / ".local/share/macwhspr"
LOG_DIR = Path.home() / "Library/Logs"
PID_FILE = CONFIG_DIR / "daemon.pid"
CONFIG_FILE = CONFIG_DIR / "config.json"
CREDENTIALS_FILE = DATA_DIR / "credentials"
RECORDING_FILE = Path(tempfile.gettempdir()) / "macwhspr_recording.wav"
HS_CLI = Path("/opt/homebrew/bin/hs")
REALTIME_SAMPLE_RATE = 24000  # required by OpenAI's Realtime transcription API
REALTIME_CHUNK_BYTES = 4800  # ~100ms at 24kHz/16-bit/mono

# cleanup.py and realtime_client.py live next to this file in the install
# directory. Add the install dir to sys.path so we can import them inline
# (no subprocess).
sys.path.insert(0, str(CONFIG_DIR.resolve()))
try:
    import cleanup as cleanup_mod  # type: ignore
except Exception as _cleanup_import_exc:
    cleanup_mod = None
    _cleanup_import_error = _cleanup_import_exc
else:
    _cleanup_import_error = None

try:
    import realtime_client as realtime_client_mod  # type: ignore
except Exception as _realtime_import_exc:
    realtime_client_mod = None
    _realtime_import_error = _realtime_import_exc
else:
    _realtime_import_error = None

DEFAULT_CONFIG = {
    "transcription_backend": "realtime-ws",  # "realtime-ws" or "rest-api"
    "transcription_url": "https://api.openai.com/v1/audio/transcriptions",
    "transcription_model": "gpt-4o-transcribe",
    "realtime_url": "wss://api.openai.com/v1/realtime?intent=transcription",
    "realtime_model": "gpt-realtime-whisper",
    "realtime_timeout": 30,
    "realtime_buffer_max_seconds": 5,
    "whisper_prompt": "Transcribe accurately. The speaker is an assistant professor in programming research and computer science.",
    "language": None,
    "paste_after_copy": True,
    "audio_feedback": True,
    "start_sound": "Tink",
    "stop_sound": "Morse",
    "error_sound": "Funk",
    "overlay": True,
    "skip_short_cleanup": True,
    "silence_rms_threshold": 0.0025,
    "sample_rate": 16000,
    "rest_timeout": 60,
}

state = "idle"
recording_proc = None
recording_started_at = 0.0
config = {}
_http_client: "httpx.Client | None" = None

# Realtime backend state
_realtime_client = None  # realtime_client_mod.RealtimeClient instance, connected once at startup
_realtime_reader_thread = None
_realtime_pcm_chunks: list = []  # accumulated raw PCM16 bytes for the in-progress recording
_realtime_pcm_lock = threading.Lock()

# Whole-word filler tokens that disqualify a transcript from the skip-cleanup
# heuristic (matches the Linux setup's filler_words list).
_FILLER_RE = re.compile(
    r"\b(uh|um|er|ah|eh|hmm|hm|mm|mhm|uh huh)\b",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] {msg}\n")
    sys.stderr.flush()


def load_config() -> None:
    global config
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            config.update(json.loads(CONFIG_FILE.read_text()))
        except json.JSONDecodeError as exc:
            log(f"Bad config.json, using defaults: {exc}")


def keychain_key() -> str | None:
    """Read OpenAI key from macOS Keychain (service=macwhspr, account=openai)."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", "macwhspr", "-a", "openai", "-w"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    key = result.stdout.strip()
    return key or None


def api_key() -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    key = keychain_key()
    if key:
        return key
    if CREDENTIALS_FILE.exists():
        return json.loads(CREDENTIALS_FILE.read_text())["openai"]
    raise RuntimeError(
        "OpenAI key not found. Add to Keychain (recommended):\n"
        "  security add-generic-password -s macwhspr -a openai -w 'sk-...'\n"
        f"or create {CREDENTIALS_FILE} with {{\"openai\":\"sk-...\"}}, "
        "or set OPENAI_API_KEY."
    )


def play_sound(name: str) -> None:
    if not config.get("audio_feedback", True):
        return
    path = f"/System/Library/Sounds/{name}.aiff"
    if not os.path.exists(path):
        log(f"sound not found: {path} (check *_sound in config.json)")
        return
    subprocess.Popen(
        ["afplay", "-v", "0.3", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def get_http_client() -> httpx.Client:
    """Lazy-init a single persistent httpx.Client.

    HTTP/2 lets transcription and cleanup share a multiplexed connection, and
    a long-lived client avoids paying TCP+TLS handshake per call (was ~200-500
    ms each before this change).
    """
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            http2=True,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
    return _http_client


def close_http_client() -> None:
    global _http_client
    if _http_client is not None:
        try:
            _http_client.close()
        finally:
            _http_client = None


def using_realtime_backend() -> bool:
    return config.get("transcription_backend", "realtime-ws") == "realtime-ws"


def connect_realtime_client() -> None:
    """Connect the persistent Realtime WebSocket client. Called once at startup.

    Reconnection on unexpected drops is handled inside RealtimeClient; this
    is only the initial connect. If it fails, recordings will error out
    until the daemon is restarted (matches hyprwhspr's behavior on Linux).
    """
    global _realtime_client
    if realtime_client_mod is None:
        log(
            "WARNING: realtime_client module not importable "
            f"({_realtime_import_error}); transcription_backend=realtime-ws will fail. "
            "Falling back is not automatic -- set transcription_backend to rest-api."
        )
        return
    _realtime_client = realtime_client_mod.RealtimeClient(
        sample_rate=REALTIME_SAMPLE_RATE,
        max_buffer_seconds=config.get("realtime_buffer_max_seconds", 5),
    )
    ok = _realtime_client.connect(
        config["realtime_url"], api_key(), config["realtime_model"]
    )
    if not ok:
        log("ERROR: failed to connect Realtime WebSocket at startup")


def should_skip_cleanup(raw: str) -> bool:
    """True if the raw transcript looks already-clean enough to skip cleanup.

    Conditions (all required):
      * skip_short_cleanup config flag is on,
      * non-empty,
      * shorter than 80 characters,
      * starts with an uppercase letter,
      * ends in `.`, `!`, or `?`,
      * contains no whole-word filler tokens.
    """
    if not config.get("skip_short_cleanup", True):
        return False
    if not raw:
        return False
    if len(raw) >= 80:
        return False
    if not raw[0].isupper():
        return False
    if raw[-1] not in ".!?":
        return False
    if _FILLER_RE.search(raw):
        return False
    return True


def measure_rms(path: Path) -> "float | None":
    """Return the recording's RMS amplitude (0..1) via `sox ... stat`, or None.

    Fail-soft: any sox/parse error returns None so callers treat the level as
    unknown and transcribe anyway rather than dropping audio on a glitch.
    Used only by the rest-api backend, which records to a WAV file.
    """
    try:
        result = subprocess.run(
            ["sox", str(path), "-n", "stat"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log(f"RMS measure skipped: {exc}")
        return None
    for line in result.stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith("RMS") and "amplitude" in stripped:
            try:
                return float(stripped.split()[-1])
            except (ValueError, IndexError):
                return None
    return None


def rms_of_pcm16(chunks: list) -> float:
    """Pure-Python RMS amplitude (0..1) of concatenated PCM16 mono chunks.

    Used by the realtime-ws backend, which streams raw PCM in memory rather
    than writing a WAV file, so there's no on-disk file for `sox ... stat`.
    """
    samples = array.array("h")
    for chunk in chunks:
        samples.frombytes(chunk)
    if not samples:
        return 0.0
    sum_sq = sum(s * s for s in samples)
    return (sum_sq / len(samples)) ** 0.5 / 32768.0


def recording_is_silent_rms(rms: "float | None") -> bool:
    """True if the given RMS amplitude carries no speech-level energy.

    Toggling recording on and saying nothing still yields a clip that clears
    the size guard. Fed near-silence, transcription models tend to
    hallucinate, so we drop the clip before spending the transcription call.
    The threshold is configurable (silence_rms_threshold); set it to 0 to
    disable.
    """
    threshold = config.get("silence_rms_threshold", 0.01)
    if not threshold or threshold <= 0:
        return False
    if rms is None:
        return False  # couldn't measure -> don't block transcription
    log(f"audio RMS {rms:.5f} (silence threshold {threshold})")
    return rms < threshold


def _normalize_for_match(text: str) -> str:
    """Lowercase, collapse runs of non-alphanumerics to single spaces, strip."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def is_prompt_echo(raw: str) -> bool:
    """True if the transcript is empty or just echoes the whisper prompt.

    On near-silent audio, batch transcription (gpt-4o-transcribe) sometimes
    returns the prompt it was handed instead of empty text. Observed forms
    (see cleanup_log.jsonl history) are the full prompt and either of its
    sentences alone. We drop the transcript when, normalized, it is empty or
    exactly equals the whole prompt or any one of its sentences. Exact-match
    only, so genuine dictation -- even sentences that reuse prompt
    vocabulary -- is never affected.

    Note: whisper_prompt is not sent at all under the realtime-ws backend
    (gpt-realtime-whisper doesn't support prompt/vocabulary steering in GA
    Realtime sessions), so this specific hallucination mode shouldn't occur
    there, but the empty-transcript check still applies.
    """
    n_raw = _normalize_for_match(raw)
    if not n_raw:
        return True
    prompt = (config.get("whisper_prompt") or "").strip()
    if not prompt:
        return False
    candidates = {_normalize_for_match(prompt)}
    candidates.update(_normalize_for_match(s) for s in re.split(r"[.!?]+", prompt))
    candidates.discard("")
    return n_raw in candidates


def notify_overlay(state_name: str) -> None:
    if not config.get("overlay", True) or not HS_CLI.exists():
        return
    snippet = (
        f"if macwhspr and macwhspr.show then macwhspr.show('{state_name}') end"
    )
    try:
        subprocess.Popen(
            [str(HS_CLI), "-c", snippet],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log(f"overlay notify failed: {exc}")


def _realtime_reader_loop(proc: subprocess.Popen) -> None:
    """Background thread: read raw PCM16 from sox's stdout, stream + buffer it.

    Runs for the lifetime of one recording. Exits when sox's stdout closes
    (on terminate/kill in stop_and_process).
    """
    while True:
        chunk = proc.stdout.read(REALTIME_CHUNK_BYTES)
        if not chunk:
            return
        with _realtime_pcm_lock:
            _realtime_pcm_chunks.append(chunk)
        if _realtime_client is not None:
            _realtime_client.append_audio(chunk)


def start_recording() -> None:
    global recording_proc, state, recording_started_at, _realtime_reader_thread
    rate = str(config.get("sample_rate", 16000))
    if using_realtime_backend():
        with _realtime_pcm_lock:
            _realtime_pcm_chunks.clear()
        if _realtime_client is not None:
            _realtime_client.clear_audio_buffer()
        try:
            recording_proc = subprocess.Popen(
                [
                    "sox", "-d",
                    "-r", str(REALTIME_SAMPLE_RATE),
                    "-c", "1",
                    "-b", "16",
                    "-e", "signed-integer",
                    "-t", "raw", "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log("sox not found. brew install sox")
            play_sound(config.get("error_sound", "Funk"))
            return
        _realtime_reader_thread = threading.Thread(
            target=_realtime_reader_loop, args=(recording_proc,), daemon=True
        )
        _realtime_reader_thread.start()
    else:
        RECORDING_FILE.unlink(missing_ok=True)
        try:
            recording_proc = subprocess.Popen(
                [
                    "sox", "-d",
                    "-r", rate,
                    "-c", "1",
                    "-b", "16",
                    "-e", "signed-integer",
                    str(RECORDING_FILE),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log("sox not found. brew install sox")
            play_sound(config.get("error_sound", "Funk"))
            return
    state = "recording"
    recording_started_at = time.perf_counter()
    log(f"Recording ({config.get('transcription_backend', 'realtime-ws')})")
    play_sound(config.get("start_sound", "Tink"))
    notify_overlay("recording")


def stop_and_process() -> None:
    global recording_proc, state
    state = "processing"
    if recording_proc:
        recording_proc.terminate()
        try:
            recording_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            recording_proc.kill()
            recording_proc.wait()
        recording_proc = None
    if _realtime_reader_thread is not None:
        _realtime_reader_thread.join(timeout=2.0)
    t_rec_end = time.perf_counter()
    play_sound(config.get("stop_sound", "Morse"))
    notify_overlay("transcribing")

    realtime = using_realtime_backend()

    if realtime:
        with _realtime_pcm_lock:
            total_bytes = sum(len(c) for c in _realtime_pcm_chunks)
            chunks_snapshot = list(_realtime_pcm_chunks)
        if total_bytes < 1000:
            log("Recording too short, skipping")
            notify_overlay("hide")
            state = "idle"
            return
        if recording_is_silent_rms(rms_of_pcm16(chunks_snapshot)):
            log("No speech detected (silent recording), skipping")
            notify_overlay("hide")
            state = "idle"
            return
        audio_kb = total_bytes / 1024.0
    else:
        if not RECORDING_FILE.exists() or RECORDING_FILE.stat().st_size < 1000:
            log("Recording too short, skipping")
            notify_overlay("hide")
            state = "idle"
            return
        if recording_is_silent_rms(measure_rms(RECORDING_FILE)):
            log("No speech detected (silent recording), skipping")
            notify_overlay("hide")
            state = "idle"
            return
        audio_kb = RECORDING_FILE.stat().st_size / 1024.0

    rec_duration = t_rec_end - recording_started_at

    try:
        t0 = time.perf_counter()
        raw = transcribe_realtime() if realtime else transcribe_rest()
        t1 = time.perf_counter()
        if not raw or is_prompt_echo(raw):
            log(f"No speech detected (empty or prompt echo): {raw[:80]!r}")
            notify_overlay("hide")
            return
        log(f"Raw: {raw[:120]}")
        cleaned = run_cleanup(raw)
        t2 = time.perf_counter()
        log(f"Cleaned: {cleaned[:120]}")
        if cleaned:
            paste(cleaned)
        t3 = time.perf_counter()
        log(
            f"Timing: audio={rec_duration:.2f}s ({audio_kb:.1f} KB) | "
            f"transcribe={t1 - t0:.2f}s | cleanup={t2 - t1:.2f}s | "
            f"paste={t3 - t2:.3f}s | post-stop={t3 - t_rec_end:.2f}s"
        )
        notify_overlay("done")
    except Exception as exc:
        log(f"Processing failed: {exc}")
        play_sound(config.get("error_sound", "Funk"))
        notify_overlay("error")
    finally:
        state = "idle"


def transcribe_realtime() -> str:
    if _realtime_client is None or not _realtime_client.connected:
        log("Realtime client not connected, attempting reconnect")
        connect_realtime_client()
    if _realtime_client is None or not _realtime_client.connected:
        raise RuntimeError("Realtime WebSocket unavailable")
    return _realtime_client.commit_and_get_text(timeout=config.get("realtime_timeout", 30))


def transcribe_rest() -> str:
    key = api_key()
    client = get_http_client()
    with open(RECORDING_FILE, "rb") as fh:
        files = {"file": ("audio.wav", fh, "audio/wav")}
        data = {"model": config["transcription_model"]}
        if config.get("whisper_prompt"):
            data["prompt"] = config["whisper_prompt"]
        if config.get("language"):
            data["language"] = config["language"]
        resp = client.post(
            config["transcription_url"],
            files=files,
            data=data,
            headers={"Authorization": f"Bearer {key}"},
            timeout=config.get("rest_timeout", 60),
        )
    resp.raise_for_status()
    return resp.json()["text"].strip()


def run_cleanup(raw: str) -> str:
    """Run cleanup inline via the imported cleanup module.

    Falls back to returning the raw transcript untouched if cleanup is
    unavailable or fails. Honors the skip_short_cleanup heuristic and still
    appends to cleanup_log.jsonl so /hypr-calibrate sees the case.
    """
    if cleanup_mod is None:
        return raw
    if should_skip_cleanup(raw):
        try:
            cleanup_mod.log_pair(raw, raw, skipped=True)
        except Exception as exc:
            log(f"cleanup log_pair failed: {exc}")
        return raw
    try:
        cleaned = cleanup_mod.clean(raw, http_client=get_http_client())
    except Exception as exc:
        log(f"Cleanup call failed: {exc}")
        return raw
    try:
        cleanup_mod.log_pair(raw, cleaned)
    except Exception as exc:
        log(f"cleanup log_pair failed: {exc}")
    return cleaned


def paste(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
    if not config.get("paste_after_copy", True):
        return
    # Fast path: Hammerspoon's eventtap. Bare `hs -c` overhead is ~8 ms vs.
    # osascript's ~30 ms, but the real saving comes from overriding
    # keyStroke's default key-down/key-up delay (200 ms) to 10 ms — still
    # generous for any app to register the chord.
    if HS_CLI.exists():
        result = subprocess.run(
            [str(HS_CLI), "-c", 'hs.eventtap.keyStroke({"cmd"}, "v", 10000)'],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return
        log(f"hs paste failed (rc={result.returncode}): {result.stderr.strip()}")
    # Fallback: osascript. Reached only if Hammerspoon is missing or IPC fails.
    time.sleep(0.05)
    subprocess.run(
        [
            "osascript", "-e",
            'tell application "System Events" to keystroke "v" using command down',
        ],
        check=False,
    )


def handle_toggle(signum, frame) -> None:
    if state == "idle":
        start_recording()
    elif state == "recording":
        stop_and_process()
    else:
        log(f"Toggle ignored in state={state}")


def handle_shutdown(signum, frame) -> None:
    global recording_proc
    log("Shutting down")
    if recording_proc:
        recording_proc.terminate()
    close_http_client()
    if _realtime_client is not None:
        _realtime_client.close()
    PID_FILE.unlink(missing_ok=True)
    sys.exit(0)


def main() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    load_config()
    if cleanup_mod is None:
        log(
            "WARNING: cleanup module not importable "
            f"({_cleanup_import_error}); transcripts will be pasted raw."
        )
    if using_realtime_backend():
        connect_realtime_client()
    signal.signal(signal.SIGUSR1, handle_toggle)
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    log(f"macwhspr daemon ready (PID {os.getpid()}). kill -USR1 {os.getpid()} to toggle.")
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
