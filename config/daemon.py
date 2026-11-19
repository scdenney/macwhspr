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
      (gpt-live-transcribe) as sox captures it, so transcription is mostly
      done by the time the user stops talking. See realtime_client.py.
  "rest-api" - the original batch path: record a full WAV, then POST it to
      /v1/audio/transcriptions (gpt-transcribe) after recording stops.

The signal handler does no work: it counts the toggle and wakes a worker
thread. Anything slow that runs on the handler's stack (the whole
transcribe/cleanup/paste pipeline used to) blocks signal.pause(), so one
wedged API call took the hotkey down with it.
"""

import array
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
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
    "transcription_model": "gpt-transcribe",
    "realtime_url": "wss://api.openai.com/v1/realtime?intent=transcription",
    "realtime_model": "gpt-live-transcribe",
    "realtime_timeout": 30,
    "realtime_buffer_max_seconds": 5,
    "realtime_delay": "low",
    "realtime_keywords": [],
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
    "dead_mic_rms_threshold": 0.0003,
    # Realtime sometimes returns a complete-looking but partial transcript.
    # Recordings at least this long whose transcript runs below this rate
    # (normal dictation is ~13-18 chars/s) are re-transcribed via REST.
    "short_transcript_min_seconds": 15,
    "short_transcript_min_cps": 8,
    # Debug: keep the last recording as last_recording.wav (+ .json with the
    # transcripts) in ~/.config/macwhspr/ for side-by-side comparison.
    "keep_last_recording": False,
    "sample_rate": 16000,
    "rest_timeout": 60,
}

VOCAB_FILE = CONFIG_DIR / "vocab.md"
MAX_KEYWORDS = 100  # the session config is metadata, not a dictionary dump

state = "idle"
_toggle_queue: "queue.SimpleQueue" = queue.SimpleQueue()
recording_proc = None
recording_started_at = 0.0
config = {}
_http_client: "httpx.Client | None" = None

# Realtime backend state
_realtime_client = None  # realtime_client_mod.RealtimeClient instance, connected once at startup
_realtime_reader_thread = None
_realtime_audio_file = None  # temporary PCM file; bounds RAM use on long recordings
_realtime_audio_bytes = 0
_realtime_pcm_lock = threading.Lock()
# (client, session_id) streaming live when the recording started, or None if
# the socket was down. Compared at stop to decide whether the server heard
# the whole recording or needs it replayed.
_realtime_rec_session = None
_realtime_reconnect_thread = None
_realtime_connect_lock = threading.Lock()

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
    """(Re)connect the persistent Realtime WebSocket client.

    Called at startup, and again at record time whenever the socket is down
    (e.g. the daemon started before the network was up, or the client's own
    reconnect attempts ran out while the Mac was asleep).
    """
    with _realtime_connect_lock:
        _connect_realtime_client_locked()


def _connect_realtime_client_locked() -> None:
    global _realtime_client
    if realtime_client_mod is None:
        log(
            "WARNING: realtime_client module not importable "
            f"({_realtime_import_error}); transcription_backend=realtime-ws will fail. "
            "Falling back is not automatic -- set transcription_backend to rest-api."
        )
        return
    if _realtime_client is not None and _realtime_client.connected:
        return
    if _realtime_client is not None:
        _realtime_client.close()
    keywords = vocab_keywords()
    _realtime_client = realtime_client_mod.RealtimeClient(
        sample_rate=REALTIME_SAMPLE_RATE,
        max_buffer_seconds=config.get("realtime_buffer_max_seconds", 5),
        prompt=config.get("whisper_prompt") or None,
        keywords=keywords,
        languages=[config["language"]] if config.get("language") else None,
        delay=config.get("realtime_delay", "low"),
    )
    if keywords:
        log(f"Realtime keyword steering: {len(keywords)} terms from vocab.md")
    ok = _realtime_client.connect(
        config["realtime_url"], api_key(), config["realtime_model"]
    )
    if not ok:
        log("ERROR: failed to connect Realtime WebSocket")


def vocab_keywords() -> list:
    """Proper nouns from vocab.md, for Realtime `keywords` steering.

    Reads the bullet/backtick terms under the "Proper nouns and spellings"
    heading so the same file that calibrates cleanup also steers the ASR
    stage. Explicit "realtime_keywords" in config.json is used instead when
    set, and both are ignored by models that don't accept steering.
    """
    def valid(term: str) -> bool:
        return bool(term.strip()) and not any(c in term for c in "<>\r\n")

    configured = config.get("realtime_keywords") or []
    if configured:
        return [str(k).strip() for k in configured if valid(str(k))][:MAX_KEYWORDS]
    if not VOCAB_FILE.exists():
        return []
    terms: list = []
    in_section = False
    for line in VOCAB_FILE.read_text().splitlines():
        if line.startswith("#"):
            in_section = "proper noun" in line.lower()
            continue
        if not in_section:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        stripped = stripped.lstrip("-*").strip()
        # Backticked terms if the line uses them, else the whole line.
        ticked = re.findall(r"`([^`]+)`", stripped)
        for term in (ticked or [stripped]):
            term = term.strip()
            if valid(term) and term not in terms:
                terms.append(term)
    return terms[:MAX_KEYWORDS]


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


def pcm_chunks(audio_file):
    audio_file.seek(0)
    while chunk := audio_file.read(REALTIME_CHUNK_BYTES):
        yield chunk


def rms_of_pcm16(audio_file) -> float:
    """Measure PCM16 RMS in chunks without retaining the recording in RAM."""
    sum_sq = count = 0
    for chunk in pcm_chunks(audio_file):
        samples = array.array("h")
        samples.frombytes(chunk)
        sum_sq += sum(s * s for s in samples)
        count += len(samples)
    if not count:
        return 0.0
    return (sum_sq / count) ** 0.5 / 32768.0


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


def skip_silent(rms: "float | None") -> None:
    """Log and clear the overlay for a silent clip.

    Near-zero RMS means the input device delivered nothing (stale default
    device after a headset change, muted mic, missing permission), not that
    the user stayed quiet, so show a warning instead of hiding silently.
    """
    log("No speech detected (silent recording), skipping")
    dead = config.get("dead_mic_rms_threshold", 0.0003)
    if rms is not None and dead and rms < dead:
        log("Input looks dead; check the input device or restart the daemon")
        notify_overlay("mic_silent")
    else:
        notify_overlay("hide")


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

    Note: gpt-live-transcribe does take whisper_prompt as the session prompt,
    but it has not been observed echoing it back on silence the way the batch
    model does. The empty-transcript check carries the realtime path.
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
    global _realtime_audio_bytes
    while True:
        chunk = proc.stdout.read(REALTIME_CHUNK_BYTES)
        if not chunk:
            return
        with _realtime_pcm_lock:
            _realtime_audio_file.write(chunk)
            _realtime_audio_bytes += len(chunk)
        if _realtime_client is not None:
            _realtime_client.append_audio(chunk)


def start_recording() -> None:
    global recording_proc, state, recording_started_at, _realtime_reader_thread
    global _realtime_rec_session, _realtime_reconnect_thread
    global _realtime_audio_file, _realtime_audio_bytes
    rate = str(config.get("sample_rate", 16000))
    if using_realtime_backend():
        with _realtime_pcm_lock:
            if _realtime_audio_file is not None:
                _realtime_audio_file.close()
            _realtime_audio_file = tempfile.TemporaryFile(mode="w+b")
            _realtime_audio_bytes = 0
        client = _realtime_client
        if client is not None and client.connected:
            client.clear_audio_buffer()
            _realtime_rec_session = (client, client.session_id)
        else:
            # Typical after sleep or login: the socket is gone. Record anyway
            # (audio is buffered locally) and reconnect in the background; the
            # full recording is replayed to the new session on stop.
            _realtime_rec_session = None
            log("Realtime client not connected at record start, reconnecting in background")
            _realtime_reconnect_thread = threading.Thread(
                target=connect_realtime_client, daemon=True
            )
            _realtime_reconnect_thread.start()
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
            total_bytes = _realtime_audio_bytes
        if total_bytes < 1000:
            log("Recording too short, skipping")
            notify_overlay("hide")
            _realtime_audio_file.close()
            state = "idle"
            return
        rms = rms_of_pcm16(_realtime_audio_file)
        if recording_is_silent_rms(rms):
            skip_silent(rms)
            _realtime_audio_file.close()
            state = "idle"
            return
        audio_kb = total_bytes / 1024.0
    else:
        if not RECORDING_FILE.exists() or RECORDING_FILE.stat().st_size < 1000:
            log("Recording too short, skipping")
            notify_overlay("hide")
            state = "idle"
            return
        rms = measure_rms(RECORDING_FILE)
        if recording_is_silent_rms(rms):
            skip_silent(rms)
            state = "idle"
            return
        audio_kb = RECORDING_FILE.stat().st_size / 1024.0

    rec_duration = t_rec_end - recording_started_at

    try:
        t0 = time.perf_counter()
        raw = transcribe_realtime(_realtime_audio_file, total_bytes) if realtime else transcribe_rest()
        t1 = time.perf_counter()
        if not raw or is_prompt_echo(raw):
            log(f"No speech detected (empty or prompt echo): {raw[:80]!r}")
            notify_overlay("hide")
            return
        log(f"Raw transcript: {len(raw)} chars")
        cleaned = run_cleanup(raw)
        t2 = time.perf_counter()
        log(f"Cleaned transcript: {len(cleaned)} chars")
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
        if realtime and _realtime_audio_file is not None:
            _realtime_audio_file.close()
        state = "idle"


def transcribe_realtime(audio_file, total_bytes: int) -> str:
    """Transcribe over the WebSocket, falling back to REST on the saved PCM.

    The recording is kept in a temporary PCM file, so a dead or replaced
    socket never loses it. Short recordings can be replayed to Realtime;
    longer ones use chunked REST transcription.
    """
    try:
        raw = _transcribe_realtime_ws(audio_file, total_bytes)
    except Exception as exc:
        log(f"Realtime transcription failed: {exc}")
        raw = ""
    seconds = total_bytes / 2.0 / REALTIME_SAMPLE_RATE
    cps = len(raw) / seconds if seconds else 0.0
    rest_raw = None
    if not raw:
        log("Realtime returned nothing for non-silent audio; retrying via REST")
        rest_raw = _rest_from_pcm(audio_file)
    elif (
        seconds >= config.get("short_transcript_min_seconds", 15)
        and cps < config.get("short_transcript_min_cps", 8)
    ):
        log(
            f"Realtime transcript looks truncated ({len(raw)} chars for "
            f"{seconds:.1f}s = {cps:.1f} chars/s); retrying via REST"
        )
        try:
            rest_raw = _rest_from_pcm(audio_file)
        except Exception as exc:
            log(f"REST retry failed, keeping realtime transcript: {exc}")
        else:
            log(f"REST retry: {len(rest_raw)} chars (realtime had {len(raw)})")
    if config.get("keep_last_recording", False):
        save_last_recording(audio_file, raw, rest_raw, seconds)
    if rest_raw and len(rest_raw) > len(raw):
        return rest_raw
    return raw


def _rest_from_pcm(audio_file) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav") as wav_file:
        write_pcm_wav(audio_file, Path(wav_file.name), REALTIME_SAMPLE_RATE)
        return transcribe_rest(Path(wav_file.name))


def save_last_recording(audio_file, realtime_raw: str, rest_raw, seconds: float) -> None:
    """Debug aid: keep the latest recording and both transcripts on disk.

    If REST wasn't already run, it runs in a background thread on the saved
    WAV so the comparison never delays the paste.
    """
    wav_path = CONFIG_DIR / "last_recording.wav"
    json_path = CONFIG_DIR / "last_recording.json"
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": round(seconds, 2),
        "realtime": realtime_raw,
        "rest": rest_raw,
    }

    def write_json():
        json_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))

    def rest_in_background():
        try:
            record["rest"] = transcribe_rest(wav_path)
        except Exception as exc:
            record["rest"] = f"<REST failed: {exc}>"
        write_json()

    try:
        write_pcm_wav(audio_file, wav_path, REALTIME_SAMPLE_RATE)
        write_json()
        if rest_raw is None:
            threading.Thread(target=rest_in_background, daemon=True).start()
        log("Saved last_recording.wav/.json")
    except Exception as exc:
        log(f"Could not save last recording: {exc}")


def _transcribe_realtime_ws(audio_file, total_bytes: int) -> str:
    thread = _realtime_reconnect_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=15)
    if _realtime_client is None or not _realtime_client.connected:
        log("Realtime client not connected, attempting reconnect")
        connect_realtime_client()
    client = _realtime_client
    if client is None or not client.connected:
        raise RuntimeError("Realtime WebSocket unavailable")
    if _realtime_rec_session != (client, client.session_id) or client.dropped_audio():
        seconds = total_bytes / 2.0 / REALTIME_SAMPLE_RATE
        if seconds > 300:
            raise RuntimeError("Long recording needs chunked REST fallback")
        log(f"Live stream incomplete; replaying {seconds:.1f}s of recorded audio")
        client.replay_audio(pcm_chunks(audio_file))
    return client.commit_and_get_text(timeout=config.get("realtime_timeout", 30))


def write_pcm_wav(audio_file, path: Path, rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        for chunk in pcm_chunks(audio_file):
            wf.writeframesraw(chunk)


def transcribe_rest(path: Path = RECORDING_FILE) -> str:
    # File uploads are capped at 25 MB. Split only when needed; four minutes
    # of 24 kHz PCM WAV is about 11.5 MB, leaving ample headroom.
    if path.stat().st_size > 24_000_000:
        parts = []
        with wave.open(str(path), "rb") as source, tempfile.TemporaryDirectory() as tmp:
            frames_per_part = source.getframerate() * 240
            for index in range(0, source.getnframes(), frames_per_part):
                part = Path(tmp) / f"part-{index // frames_per_part}.wav"
                with wave.open(str(part), "wb") as target:
                    target.setparams(source.getparams())
                    target.writeframes(source.readframes(frames_per_part))
                parts.append(_transcribe_rest_one(part))
        return " ".join(parts).strip()
    return _transcribe_rest_one(path)


def _transcribe_rest_one(path: Path) -> str:
    key = api_key()
    client = get_http_client()
    with open(path, "rb") as fh:
        files = {"file": ("audio.wav", fh, "audio/wav")}
        data = {"model": config["transcription_model"]}
        if config.get("whisper_prompt"):
            data["prompt"] = config["whisper_prompt"]
        if config["transcription_model"] == "gpt-transcribe":
            keywords = vocab_keywords()
            if keywords:
                data["keywords[]"] = keywords
        if config.get("language"):
            field = "languages[]" if config["transcription_model"] == "gpt-transcribe" else "language"
            data[field] = config["language"]
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
    """SIGUSR1 handler. Records the toggle and returns immediately.

    Python runs this on the main thread, so anything slow here blocks
    signal.pause() and every later tap with it.
    """
    # SimpleQueue.put is reentrant-safe, which is what makes it usable here.
    _toggle_queue.put(time.time())


def worker_loop() -> None:
    """Drain toggles and drive the state machine off the signal handler."""
    global state
    while True:
        _toggle_queue.get()
        try:
            if state == "idle":
                start_recording()
            elif state == "recording":
                stop_and_process()
            else:
                log(f"Toggle ignored in state={state}")
        except Exception as exc:
            # A pipeline that dies mid-flight must not strand the state
            # machine: state=processing makes every later tap a no-op.
            log(f"Toggle handling failed: {exc}")
            play_sound(config.get("error_sound", "Funk"))
            notify_overlay("error")
        finally:
            if state == "processing":
                log("Forcing state back to idle after processing")
                state = "idle"
                notify_overlay("hide")


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
    threading.Thread(target=worker_loop, daemon=True, name="worker").start()
    log(f"macwhspr daemon ready (PID {os.getpid()}). kill -USR1 {os.getpid()} to toggle.")
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
