"""
OpenAI Realtime WebSocket transcription client for macwhspr.

Transcribe-only, OpenAI-only port of the equivalent client in hyprwhspr
(the Linux counterpart to this setup). Ported deliberately narrow: no
multi-provider abstraction, no converse/voice-to-AI mode, no resampling
(daemon.py records natively at 24kHz for this backend, matching what the
Realtime API requires, so raw PCM16 bytes are sent as-is).

Vocabulary steering: `gpt-live-transcribe` (the successor to
gpt-realtime-whisper) accepts `prompt`, `keywords` and `languages` in the
transcription session config, so domain terms are steered at the ASR stage
rather than being left entirely to cleanup.py. gpt-realtime-whisper itself
has no steering; on that model these fields are simply omitted.

Hang safety: every ws.send() is bounded. The socket carries SO_SNDTIMEO and
run_forever runs client-side keepalive pings, and the commit is routed
through the sender thread rather than being sent from the caller's thread.
Without that last part a half-open TCP connection (Mac sleep, Wi-Fi change)
wedges the caller forever inside websocket-client's internal send lock,
which used to brick the daemon in state=processing until a kickstart.
"""

import base64
import json
import socket
import struct
import threading
import time
from collections import deque
from queue import Empty, Queue

# Wall-clock ceiling on a single socket write. Without it a half-open TCP
# connection blocks write() indefinitely while holding websocket-client's
# internal send lock, which in turn blocks every other sender.
SEND_TIMEOUT_SECONDS = 5

# Client-side keepalive. The server closes on its own ping timeout, but only
# the client pinging detects a peer that has silently gone away.
PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 10

# Models that accept prompt/keywords/languages in the transcription session.
STEERABLE_MODELS = ("gpt-live-transcribe", "gpt-transcribe")

_COMMIT_SENTINEL = object()
_CLEAR_SENTINEL = object()

try:
    import websocket
except (ImportError, ModuleNotFoundError) as e:
    raise SystemExit(
        "ERROR: websocket-client is not available in this Python environment.\n"
        f"ImportError: {e}\n"
        "Install it: pip install websocket-client>=1.6.0"
    )


class RealtimeClient:
    """WebSocket client for OpenAI's Realtime transcription API (transcribe mode only)."""

    def __init__(
        self,
        sample_rate: int = 24000,
        max_buffer_seconds: float = 5.0,
        prompt: "str | None" = None,
        keywords: "list | None" = None,
        languages: "list | None" = None,
        delay: str = "low",
    ):
        self.ws = None
        self.url = None
        self.api_key = None
        self.model = None
        self.sample_rate = sample_rate
        self.max_buffer_seconds = max(1.0, max_buffer_seconds)
        self.prompt = prompt
        self.keywords = keywords or []
        self.languages = languages or []
        self.delay = delay

        self.lock = threading.Lock()
        self.connected = False
        self.connecting = False
        self.receiver_thread = None
        self.receiver_running = False

        self.event_queue: Queue = Queue()
        self.response_event = threading.Event()

        self._committed_segments = []
        self._partial_transcript = ""
        self._transcript_generation = 0
        self._buffer_committed = False

        # Audio streaming (bytes-based; no numpy/resampling needed since the
        # daemon records at self.sample_rate natively for this backend).
        self._audio_queue = deque()
        self.audio_buffer_seconds = 0.0
        self._queue_cond = threading.Condition(self.lock)
        self._sender_thread = None
        self._sender_running = False
        self._dropped_chunks = 0
        # Bumped on every successful open. Each connection is a new server
        # session with an empty input buffer, so a change mid-recording means
        # the server has not heard all of the recording.
        self.session_id = 0

        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.reconnect_delays = [1, 2, 4, 8, 16]

    # -- connection lifecycle -------------------------------------------------

    def connect(self, url: str, api_key: str, model: str) -> bool:
        self.url = url
        self.api_key = api_key
        self.model = model
        return self._connect_internal()

    def _connect_internal(self) -> bool:
        if self.connecting:
            return False
        self.connecting = True
        try:
            print(f"[REALTIME] Connecting to {self.url}...", flush=True)
            self.ws = websocket.WebSocketApp(
                self.url,
                header=[f"Authorization: Bearer {self.api_key}"],
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            # SO_SNDTIMEO bounds write(); ping_interval/ping_timeout make a
            # peer that has silently gone away surface as a close instead of
            # a socket that accepts data forever and never answers.
            sndtimeo = struct.pack("ll", SEND_TIMEOUT_SECONDS, 0)
            ws_thread = threading.Thread(
                target=self.ws.run_forever,
                kwargs={
                    "ping_interval": PING_INTERVAL_SECONDS,
                    "ping_timeout": PING_TIMEOUT_SECONDS,
                    "sockopt": ((socket.SOL_SOCKET, socket.SO_SNDTIMEO, sndtimeo),),
                },
                daemon=True,
            )
            ws_thread.start()

            timeout = 10.0
            start_time = time.time()
            while not self.connected and (time.time() - start_time) < timeout:
                time.sleep(0.1)

            if self.connected:
                print("[REALTIME] Connected successfully", flush=True)
                self.reconnect_attempts = 0
                self._send_session_update()
                return True

            print("[REALTIME] Connection timeout", flush=True)
            try:
                self.ws.close()
            except Exception:
                pass
            return False
        except Exception as e:
            print(f"[REALTIME] Connection error: {e}", flush=True)
            return False
        finally:
            self.connecting = False

    def _on_open(self, _ws):
        start_receiver = False
        with self.lock:
            self.connected = True
            self.connecting = False
            self.session_id += 1
            if not self.receiver_running:
                self.receiver_running = True
                start_receiver = True
            self._queue_cond.notify_all()
        if start_receiver:
            self.receiver_thread = threading.Thread(target=self._receiver_loop, daemon=True)
            self.receiver_thread.start()
        self._start_sender_thread()

    def _on_message(self, _ws, message):
        try:
            self.event_queue.put(json.loads(message))
        except json.JSONDecodeError as e:
            print(f"[REALTIME] Failed to parse event: {e}", flush=True)

    def _on_error(self, _ws, error):
        print(f"[REALTIME] WebSocket error: {error}", flush=True)

    def _on_close(self, _ws, close_status_code, _close_msg):
        with self.lock:
            self.connected = False
            self._sender_running = False
            self._audio_queue.clear()
            self.audio_buffer_seconds = 0.0
            self._queue_cond.notify_all()
        print(f"[REALTIME] WebSocket closed (code: {close_status_code})", flush=True)
        if self.receiver_running and close_status_code != 1000:
            self._attempt_reconnect()

    def _attempt_reconnect(self):
        if not self.receiver_running:
            return False  # close() was called; the daemon replaced this client
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            print("[REALTIME] Max reconnection attempts reached", flush=True)
            return False
        delay = self.reconnect_delays[min(self.reconnect_attempts, len(self.reconnect_delays) - 1)]
        self.reconnect_attempts += 1
        print(
            f"[REALTIME] Reconnecting (attempt {self.reconnect_attempts}/"
            f"{self.max_reconnect_attempts}) in {delay}s...",
            flush=True,
        )
        time.sleep(delay)
        # _connect_internal already sends session.update on success.
        return bool(self._connect_internal())

    def _send_session_update(self):
        if not self.connected or not self.ws:
            return
        transcription = {"model": self.model, "delay": self.delay}
        steered = []
        if self.model in STEERABLE_MODELS:
            if self.prompt:
                transcription["prompt"] = self.prompt
                steered.append("prompt")
            if self.keywords:
                transcription["keywords"] = self.keywords
                steered.append(f"{len(self.keywords)} keywords")
            if self.languages:
                transcription["languages"] = self.languages
                steered.append("languages")
        session_data = {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": self.sample_rate},
                    "transcription": transcription,
                    # Recording start/stop is the hotkey's job, not the server's;
                    # commits are manual.
                    "turn_detection": None,
                }
            },
        }
        if not self._send_json({"type": "session.update", "session": session_data}):
            return
        detail = f" ({', '.join(steered)})" if steered else ""
        print(
            f"[REALTIME] Sent session.update: {self.model} delay={self.delay}{detail}",
            flush=True,
        )

    def _send_json(self, event: dict) -> bool:
        """Send one event, converting any socket failure into a False return.

        Every send in this client goes through here so a dead connection can
        never leave a caller blocked in websocket-client's send path.
        """
        ws = self.ws
        if ws is None:
            return False
        try:
            ws.send(json.dumps(event))
            return True
        except Exception as e:
            print(f"[REALTIME] Send failed ({event.get('type')}): {e}", flush=True)
            with self.lock:
                self.connected = False
                self._queue_cond.notify_all()
            return False

    # -- receiving --------------------------------------------------------

    def _receiver_loop(self):
        while self.receiver_running:
            try:
                event = self.event_queue.get(timeout=0.1)
                self._handle_event(event)
            except Empty:
                continue
            except Exception as e:
                print(f"[REALTIME] Error in receiver loop: {e}", flush=True)

    def _handle_event(self, event: dict):
        event_type = event.get("type", "")

        if event_type in ("session.created", "session.updated"):
            print(f"[REALTIME] Session event: {event_type}", flush=True)

        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = (event.get("transcript") or "").strip()
            with self.lock:
                if not transcript:
                    transcript = self._partial_transcript.strip()
                if transcript:
                    self._committed_segments.append(transcript)
                self._transcript_generation += 1
                self._partial_transcript = ""
            self.response_event.set()
            print(f"[REALTIME] Transcription completed ({len(transcript)} chars)", flush=True)

        elif event_type == "conversation.item.input_audio_transcription.delta":
            delta = event.get("delta") or ""
            if delta:
                with self.lock:
                    self._partial_transcript += delta

        elif event_type == "input_audio_buffer.committed":
            print("[REALTIME] Audio buffer committed", flush=True)
            with self.lock:
                self._buffer_committed = True

        elif event_type == "error":
            error_message = (event.get("error") or {}).get("message", "Unknown error")
            print(f"[REALTIME] Server error: {error_message}", flush=True)
            with self.lock:
                self._partial_transcript = ""
            self.response_event.set()

    # -- sending audio ------------------------------------------------------

    def _start_sender_thread(self):
        with self.lock:
            if self._sender_running and self._sender_thread and self._sender_thread.is_alive():
                return
            # A sender left over from a dropped connection may still be alive
            # (blocked in a socket write that is draining its SO_SNDTIMEO).
            # It exits on its own because _sender_running went False in
            # _on_close; it must not stop us starting the replacement, or the
            # new connection would have nothing servicing its queue.
            self._sender_running = True
            self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
            self._sender_thread.start()

    def _sender_loop(self):
        me = threading.current_thread()
        while True:
            with self.lock:
                self._queue_cond.wait_for(
                    lambda: (not self._sender_running)
                    or (self._sender_thread is not me)
                    or (self.connected and self.ws and len(self._audio_queue) > 0)
                )
                if not self._sender_running or self._sender_thread is not me:
                    return
                item = self._audio_queue.popleft()
                if item is not _COMMIT_SENTINEL and item is not _CLEAR_SENTINEL:
                    chunk_duration = len(item) / 2.0 / float(self.sample_rate)
                    self.audio_buffer_seconds = max(
                        0.0, self.audio_buffer_seconds - chunk_duration
                    )
                if not self._audio_queue:
                    self._queue_cond.notify_all()
            if item is _COMMIT_SENTINEL:
                # Sent from this thread, in order behind the audio it commits,
                # so the caller never touches the socket itself.
                if self._send_json({"type": "input_audio_buffer.commit"}):
                    print("[REALTIME] Committed audio buffer", flush=True)
                else:
                    self.response_event.set()
                continue
            if item is _CLEAR_SENTINEL:
                self._send_json({"type": "input_audio_buffer.clear"})
                continue
            self._send_json({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(item).decode("utf-8"),
            })

    def append_audio(self, pcm16_bytes: bytes):
        """Queue raw PCM16 mono bytes at self.sample_rate for sending."""
        if not self.connected or not self.ws:
            return
        with self.lock:
            chunk_duration = len(pcm16_bytes) / 2.0 / float(self.sample_rate)
            while (
                (self.audio_buffer_seconds + chunk_duration) > self.max_buffer_seconds
                and self._audio_queue
                and self._audio_queue[0] is not _COMMIT_SENTINEL
            ):
                dropped = self._audio_queue.popleft()
                self.audio_buffer_seconds = max(
                    0.0, self.audio_buffer_seconds - len(dropped) / 2.0 / float(self.sample_rate)
                )
                self._dropped_chunks += 1
            if (self.audio_buffer_seconds + chunk_duration) > self.max_buffer_seconds:
                self._dropped_chunks += 1
            else:
                self._audio_queue.append(pcm16_bytes)
                self.audio_buffer_seconds += chunk_duration
                self._queue_cond.notify_all()

    def dropped_audio(self) -> bool:
        with self.lock:
            return self._dropped_chunks > 0

    def replay_audio(self, chunks: list):
        """Replace the server's input buffer with the whole recording.

        Used when live streaming missed part of it (not connected at start,
        a reconnect mid-recording, or queue overflow). Queued behind a clear
        on the sender thread, so ordering is preserved and the size cap in
        append_audio does not apply.
        """
        with self.lock:
            self._audio_queue.clear()
            self._audio_queue.append(_CLEAR_SENTINEL)
            self._audio_queue.extend(chunks)
            self.audio_buffer_seconds = 0.0
            self._buffer_committed = False
            self._committed_segments = []
            self._partial_transcript = ""
            self._transcript_generation = 0
            self._dropped_chunks = 0
            self._queue_cond.notify_all()
        self.response_event.clear()

    def clear_audio_buffer(self):
        """Reset client-side state before starting a new recording."""
        if not self.connected or not self.ws:
            return
        try:
            self._send_json({"type": "input_audio_buffer.clear"})
            with self.lock:
                self._audio_queue.clear()
                self.audio_buffer_seconds = 0.0
                self._buffer_committed = False
                self._committed_segments = []
                self._transcript_generation = 0
                self._partial_transcript = ""
                self._dropped_chunks = 0
            self.response_event.clear()
        except Exception as e:
            print(f"[REALTIME] Failed to clear buffer: {e}", flush=True)

    # -- committing and reading the result -----------------------------------

    def commit_and_get_text(self, timeout: float = 30.0) -> str:
        """Commit the streamed audio and return the final transcript.

        Bounded by `timeout` under every failure mode. The caller is usually
        the daemon's processing path, and a caller that cannot return leaves
        the hotkey dead, so nothing here waits on the socket: the commit is
        queued for the sender thread and we only wait on `response_event`.
        """
        if not self.connected or not self.ws:
            print("[REALTIME] Not connected, cannot commit", flush=True)
            return ""
        deadline = time.monotonic() + timeout
        try:
            with self.lock:
                buffer_was_committed = self._buffer_committed
                self._buffer_committed = False
                self.response_event.clear()
                if not buffer_was_committed:
                    self._audio_queue.append(_COMMIT_SENTINEL)
                    self._queue_cond.notify_all()
                else:
                    print("[REALTIME] Skipping commit (already committed)", flush=True)

            print("[REALTIME] Waiting for transcription...", flush=True)
            remaining = max(0.1, deadline - time.monotonic())
            if not self.response_event.wait(timeout=remaining):
                print(f"[REALTIME] Timeout waiting for transcript ({timeout}s)", flush=True)

            with self.lock:
                result = " ".join(p for p in self._committed_segments if p).strip()
                if not result:
                    result = self._partial_transcript.strip()
                self._committed_segments = []
                self._partial_transcript = ""
                self._transcript_generation = 0
                self._audio_queue.clear()
                self.audio_buffer_seconds = 0.0

            print(f"[REALTIME] Transcript received ({len(result)} chars)", flush=True)
            return result
        except Exception as e:
            print(f"[REALTIME] Error in commit_and_get_text: {e}", flush=True)
            return ""

    def close(self):
        with self.lock:
            self._sender_running = False
            self.receiver_running = False
            self._audio_queue.clear()
            self.audio_buffer_seconds = 0.0
            self._queue_cond.notify_all()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        if self.receiver_thread and self.receiver_thread.is_alive():
            self.receiver_thread.join(timeout=1.0)
        if self._sender_thread and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=1.0)
        with self.lock:
            self.connected = False
        print("[REALTIME] Connection closed", flush=True)
