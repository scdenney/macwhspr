"""
OpenAI Realtime WebSocket transcription client for macwhspr.

Transcribe-only, OpenAI-only port of the equivalent client in hyprwhspr
(the Linux counterpart to this setup). Ported deliberately narrow: no
multi-provider abstraction, no converse/voice-to-AI mode, no resampling
(daemon.py records natively at 24kHz for this backend, matching what the
Realtime API requires, so raw PCM16 bytes are sent as-is).

Known limitation (confirmed against OpenAI's Realtime transcription guide
and hyprwhspr's own realtime_client.py): gpt-realtime-whisper does not
support prompt/vocabulary steering in GA Realtime sessions. whisper_prompt
is NOT sent here, matching upstream behavior. Domain vocabulary correction
still happens downstream in cleanup.py via vocab.md.
"""

import json
import threading
import time
from collections import deque
from queue import Empty, Queue
from typing import Optional

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

    def __init__(self, sample_rate: int = 24000, max_buffer_seconds: float = 5.0):
        self.ws = None
        self.url = None
        self.api_key = None
        self.model = None
        self.sample_rate = sample_rate
        self.max_buffer_seconds = max(1.0, max_buffer_seconds)

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
            ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
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
        if self._connect_internal():
            self._send_session_update()
            return True
        return False

    def _send_session_update(self):
        if not self.connected or not self.ws:
            return
        session_data = {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": self.sample_rate},
                    "transcription": {"model": self.model, "delay": "low"},
                    # gpt-realtime-whisper doesn't use server VAD; commits are manual
                    # (matches hyprwhspr's handling for this model).
                    "turn_detection": None,
                }
            },
        }
        event = {"type": "session.update", "session": session_data}
        try:
            self.ws.send(json.dumps(event))
            print("[REALTIME] Sent session.update", flush=True)
        except Exception as e:
            print(f"[REALTIME] Failed to send session.update: {e}", flush=True)

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
            if self._sender_thread and self._sender_thread.is_alive():
                return
            self._sender_running = True
            self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
            self._sender_thread.start()

    def _sender_loop(self):
        while True:
            with self.lock:
                self._queue_cond.wait_for(
                    lambda: (not self._sender_running)
                    or (self.connected and self.ws and len(self._audio_queue) > 0)
                )
                if not self._sender_running:
                    return
                chunk = self._audio_queue.popleft()
                chunk_duration = len(chunk) / 2.0 / float(self.sample_rate)
                self.audio_buffer_seconds = max(0.0, self.audio_buffer_seconds - chunk_duration)
                ws = self.ws
                if not self._audio_queue:
                    self._queue_cond.notify_all()
            try:
                import base64
                event = {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("utf-8"),
                }
                ws.send(json.dumps(event))
            except Exception as e:
                print(f"[REALTIME] Failed to send queued audio: {e}", flush=True)

    def append_audio(self, pcm16_bytes: bytes):
        """Queue raw PCM16 mono bytes at self.sample_rate for sending."""
        if not self.connected or not self.ws:
            return
        with self.lock:
            chunk_duration = len(pcm16_bytes) / 2.0 / float(self.sample_rate)
            while (
                (self.audio_buffer_seconds + chunk_duration) > self.max_buffer_seconds
                and self._audio_queue
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

    def clear_audio_buffer(self):
        """Reset client-side state before starting a new recording."""
        if not self.connected or not self.ws:
            return
        try:
            self.ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
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
        if not self.connected or not self.ws:
            print("[REALTIME] Not connected, cannot commit", flush=True)
            return ""
        try:
            with self.lock:
                drain_timeout = min(self.max_buffer_seconds + 1.0, max(0.5, timeout * 0.5))
                buffer_was_committed = self._buffer_committed
                self._buffer_committed = False
                self.response_event.clear()

            with self.lock:
                self._queue_cond.wait_for(lambda: len(self._audio_queue) == 0, timeout=drain_timeout)

            time.sleep(0.05)  # grace period for any in-flight send to land before commit

            if not buffer_was_committed:
                self.ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                print("[REALTIME] Committed audio buffer", flush=True)
            else:
                print("[REALTIME] Skipping commit (already committed)", flush=True)

            print("[REALTIME] Waiting for transcription...", flush=True)
            if not self.response_event.wait(timeout=timeout):
                print(f"[REALTIME] Timeout waiting for transcript ({timeout}s)", flush=True)

            with self.lock:
                result = " ".join(p for p in self._committed_segments if p).strip()
                self._committed_segments = []
                self._transcript_generation = 0
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
