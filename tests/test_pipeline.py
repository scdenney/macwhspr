import importlib.util
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1] / "config"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RealtimeTests(unittest.TestCase):
    def setUp(self):
        self.client = load("realtime_client").RealtimeClient()

    def test_late_completion_does_not_wake_new_turn(self):
        self.client._handle_event({"type": "conversation.item.input_audio_transcription.completed", "item_id": "old", "transcript": "old text"})
        self.client._handle_event({"type": "input_audio_buffer.committed", "item_id": "new"})
        self.assertFalse(self.client.response_event.is_set())
        self.client._handle_event({"type": "conversation.item.input_audio_transcription.completed", "item_id": "new", "transcript": "new text"})
        self.assertTrue(self.client.response_event.is_set())

    def test_completion_before_commit_ack_is_matched(self):
        self.client._handle_event({"type": "conversation.item.input_audio_transcription.completed", "item_id": "one", "transcript": "hello"})
        self.assertFalse(self.client.response_event.is_set())
        self.client._handle_event({"type": "input_audio_buffer.committed", "item_id": "one"})
        self.assertTrue(self.client.response_event.is_set())

    def test_clear_is_queued_before_new_audio(self):
        self.client.connected = True
        self.client.ws = object()
        self.client.clear_audio_buffer()
        self.client.append_audio(b"\x00\x00")
        self.assertEqual(len(self.client._audio_queue), 2)
        self.assertEqual(self.client._audio_queue[1], b"\x00\x00")


class RestTests(unittest.TestCase):
    def test_rest_multipart_has_keywords_and_languages(self):
        daemon = load("daemon")
        daemon.config = {"transcription_model": "gpt-transcribe", "transcription_url": "https://example.test", "whisper_prompt": "A meeting.", "language": "en", "rest_timeout": 10, "realtime_keywords": ["Hyprland", "macwhspr"]}
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            original = daemon.RECORDING_FILE
            daemon.RECORDING_FILE = Path(audio.name)
            try:
                def respond(request):
                    body = request.read().decode("latin1")
                    for field in ('name="keywords[]"', 'name="languages[]"'):
                        self.assertIn(field, body)
                    self.assertIn("Hyprland", body)
                    self.assertNotIn('name="language"', body)
                    return daemon.httpx.Response(200, json={"text": "hello"})
                with daemon.httpx.Client(transport=daemon.httpx.MockTransport(respond)) as client:
                    with mock.patch.object(daemon, "api_key", return_value="test"), mock.patch.object(daemon, "get_http_client", return_value=client):
                        self.assertEqual(daemon.transcribe_rest(Path(audio.name)), "hello")
            finally:
                daemon.RECORDING_FILE = original

    def test_oversized_wav_is_split_before_upload(self):
        daemon = load("daemon")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "long.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b"\x00\x00" * (24000 * 510))
            sizes = []
            def fake_upload(part):
                sizes.append(part.stat().st_size)
                return "part"
            with mock.patch.object(daemon, "_transcribe_rest_one", side_effect=fake_upload):
                result = daemon.transcribe_rest(path)
            self.assertEqual(result, "part part part")
            self.assertTrue(all(size < 24_000_000 for size in sizes))


class CleanupTests(unittest.TestCase):
    def test_incomplete_cleanup_is_rejected(self):
        cleanup = load("cleanup")
        response = mock.Mock()
        response.json.return_value = {"choices": [{"finish_reason": "length", "message": {"content": "short"}}]}
        client = mock.Mock()
        client.post.return_value = response
        with mock.patch.object(cleanup, "api_key", return_value="test"):
            with self.assertRaises(ValueError):
                cleanup.clean("A complete dictated sentence.", http_client=client)

    def test_unusually_short_cleanup_is_rejected(self):
        cleanup = load("cleanup")
        response = mock.Mock()
        response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": "Too short."}}]}
        client = mock.Mock()
        client.post.return_value = response
        raw = " ".join(f"word{i}" for i in range(30))
        with mock.patch.object(cleanup, "api_key", return_value="test"):
            with self.assertRaises(ValueError):
                cleanup.clean(raw, http_client=client)

    def test_history_retains_most_recent_entries(self):
        cleanup = load("cleanup")
        with tempfile.TemporaryDirectory() as tmp:
            cleanup.LOG_FILE = Path(tmp) / "history.jsonl"
            cleanup.MAX_LOG_ENTRIES = 2
            for word in ("first", "second", "third"):
                cleanup._log_writes_since_prune = 100
                cleanup.log_pair(word, word)
            rows = [__import__("json").loads(line) for line in cleanup.LOG_FILE.read_text().splitlines()]
            self.assertEqual([row["raw"] for row in rows], ["second", "third"])


if __name__ == "__main__":
    unittest.main()
