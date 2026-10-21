"""API transport, host trigger and VirtualBox SDK contracts."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests

from ai_client import SethAIClient
from capture import SethCapture
from inference import AIError, parse_decision
from tests.fake_vbox import FakeManager, TEST_DISPLAY
from trigger import SethTrigger, TriggerError, send_trigger
from vbox_backend import GuestDisplay, VMError, VirtualBoxBackend, sdk_operation

DECISION = {"status": "answer", "confidence": .99, "answer": "2"}


def configuration(root):
    return {"project_dir": str(root),
            "api": {"base_url": "http://127.0.0.1:1/v1", "api_key": "local-test-key", "model": "mock-vision"},
            "vm": {"name": "Seth Integration Test", "screen": 0},
            "capture": {"output_dir": "captures", "max_age_seconds": 90}, "min_confidence": .85}


class TriggerTests(unittest.TestCase):
    def test_restarts_after_process_crash_without_manual_socket_removal(self):
        with tempfile.TemporaryDirectory() as root:
            script = ("import signal, sys; from trigger import SethTrigger; "
                      "trigger = SethTrigger(sys.argv[1], lambda: True); trigger.start(); "
                      "print('ready', flush=True); signal.pause()")
            child = subprocess.Popen([sys.executable, "-c", script, root], stdout=subprocess.PIPE,
                                     text=True, cwd=Path(__file__).resolve().parents[1])
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                child.kill()
                child.wait(timeout=5)
                trigger = SethTrigger(root, lambda: True)
                self.assertTrue(trigger.path.exists())
                trigger.start()
                trigger.stop()
                self.assertFalse(trigger.path.exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                child.stdout.close()

    def test_live_socket_and_regular_file_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as root:
            trigger = SethTrigger(root, lambda: True)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(trigger.path))
                server.listen(1)
                identity = trigger.path.stat().st_ino
                with self.assertRaises(TriggerError):
                    trigger.start()
                self.assertEqual(trigger.path.stat().st_ino, identity)
            trigger.path.unlink()
            trigger.path.write_text("keep this file")
            try:
                with self.assertRaises(TriggerError):
                    trigger.start()
                self.assertEqual(trigger.path.read_text(), "keep this file")
            finally:
                trigger.path.unlink()
            trigger.start()
            trigger.stop()

    def test_host_trigger_accepts_drops_busy_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as root:
            stop = threading.Event()
            accepted = iter((True, False))
            trigger = SethTrigger(root, lambda: next(accepted))
            trigger.start()
            thread = threading.Thread(target=trigger.serve, args=(stop,))
            thread.start()
            try:
                self.assertEqual(trigger.path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(send_trigger(root), "accepted")
                self.assertEqual(send_trigger(root), "busy")
                with self.assertRaises(TriggerError):
                    SethTrigger(root, lambda: True).start()
            finally:
                stop.set()
                thread.join(2)
                trigger.stop()
            self.assertFalse(thread.is_alive())
            self.assertFalse(trigger.path.exists())


class LocalAPI:
    def __init__(self, decision=DECISION, status=200, content=None, stream_events=None):
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, self.headers.get("Authorization"), payload))
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if stream_events is not None else "application/json")
                self.end_headers()
                if stream_events is not None:
                    self.wfile.write(stream_events.encode())
                    return
                result = content if content is not None else {"choices": [{"message": {"content": json.dumps(decision)}}]}
                self.wfile.write(json.dumps(result).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)

    def __enter__(self):
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class VirtualBoxSDKTests(unittest.TestCase):
    def test_all_input_operations_are_rejected_before_sdk_access(self):
        for operation in ("click", "move", "wheel", "drag", "keyboard", "type"):
            factory = Mock()
            with self.subTest(operation=operation), self.assertRaisesRegex(VMError, "capture-only"):
                sdk_operation({"operation": operation}, factory)
            factory.assert_not_called()
        backend = VirtualBoxBackend(configuration("."))
        for method in ("click", "wheel", "park_pointer", "_input"):
            self.assertFalse(hasattr(backend, method))

    def test_capture_and_status_work_without_any_mouse_interface(self):
        with tempfile.TemporaryDirectory() as root:
            for operation in ("status", "capture"):
                manager = FakeManager()
                path = Path(root) / "frame.png"
                result = sdk_operation({"operation": operation, "target": "test", "screen": 0,
                                        "path": str(path)}, lambda *args: manager)
                self.assertEqual(result["display"], TEST_DISPLAY)
                if operation == "capture":
                    self.assertTrue(path.is_file())
                self.assertTrue(manager.unlocked)
                self.assertTrue(manager.closed)

    def test_paused_disabled_blank_or_invalid_screen_cannot_capture(self):
        for change in ("paused", "disabled", "blank", "screen"):
            manager = FakeManager()
            request = {"operation": "capture", "target": "test", "screen": 0, "path": "unused.png"}
            if change == "paused": manager.machine.state = -1
            if change == "disabled": manager.console.display.status = 0
            if change == "blank": manager.console.display.status = 2
            if change == "screen": request["screen"] = 1
            with self.subTest(change=change), self.assertRaises(VMError):
                sdk_operation(request, lambda *args: manager)
            self.assertTrue(manager.closed)

    def test_resizing_during_capture_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as root:
            manager = FakeManager()
            manager.console.display.after_capture = lambda: setattr(manager.console.display, "size", (640, 480))
            backend = VirtualBoxBackend(configuration(root))
            with patch.object(backend, "_request", side_effect=lambda operation, **data: sdk_operation(
                    {"operation": operation, "target": "test", "screen": 0, **data}, lambda *args: manager)):
                with self.assertRaises(VMError):
                    SethCapture(configuration(root), backend).capture_frame()
            self.assertEqual(list((Path(root) / "captures").iterdir()), [])
            self.assertTrue(manager.unlocked)


class APITests(unittest.TestCase):
    def test_original_unbadged_image_bytes_are_sent_once_with_manual_format(self):
        for streaming in (False, True):
            for fmt in ("PNG", "JPEG"):
                event = {"choices": [{"delta": {"content": json.dumps(DECISION)}, "finish_reason": "stop"}]}
                stream = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n" if streaming else None
                with self.subTest(streaming=streaming, image=fmt), tempfile.TemporaryDirectory() as root, LocalAPI(stream_events=stream) as api:
                    path = Path(root) / "question.png"
                    Image.new("RGB", (320, 200), "white").save(path, format=fmt)
                    config = configuration(root)
                    config["api"].update(base_url=api.url, stream=streaming)
                    raw = SethAIClient(config).analyze_screenshot(path, "mc")
                    self.assertEqual(parse_decision(raw, "mc"), DECISION)
                    self.assertEqual(len(api.requests), 1)
                    content = api.requests[0][2]["messages"][0]["content"]
                    self.assertIn("MC:", content[0]["text"])
                    self.assertNotIn("target_id", content[0]["text"])
                    url = content[1]["image_url"]["url"]
                    self.assertEqual(base64.b64decode(url.split(",", 1)[1]), path.read_bytes())

    def test_nonstream_requires_complete_plain_response_when_metadata_is_present(self):
        for reason in ("length", "tool_calls", "content_filter", None):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "question.png"
                Image.new("RGB", (320, 200)).save(path)
                content = {"choices": [{"message": {"content": json.dumps(DECISION)}, "finish_reason": reason}]}
                with LocalAPI(content=content) as api:
                    config = configuration(root)
                    config["api"]["base_url"] = api.url
                    with self.assertRaises(AIError):
                        SethAIClient(config).analyze_screenshot(path, "mc")

    def test_partial_stream_and_tool_calls_never_produce_a_decision(self):
        cases = [
            [{"choices": [{"delta": {"content": json.dumps(DECISION)}}]}],
            [{"choices": [{"delta": {"content": json.dumps(DECISION)}, "finish_reason": "length"}]}],
            [{"choices": [{"delta": {"tool_calls": [{}]}, "finish_reason": "stop"}]}],
        ]
        for events in cases:
            stream = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
            with tempfile.TemporaryDirectory() as root, LocalAPI(stream_events=stream) as api:
                path = Path(root) / "question.png"
                Image.new("RGB", (320, 200)).save(path)
                config = configuration(root)
                config["api"].update(base_url=api.url, stream=True)
                with self.assertRaises(AIError):
                    SethAIClient(config).analyze_screenshot(path, "mc")
                self.assertEqual(len(api.requests), 1)

    def test_transport_errors_do_not_echo_credentials_or_retry(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "question.png"
            Image.new("RGB", (320, 200)).save(path)
            for error in (requests.ConnectionError("secret-value"), requests.Timeout("secret-value")):
                with patch("ai_client.requests.post", side_effect=error) as post:
                    with self.assertRaises(AIError) as caught:
                        SethAIClient(configuration(root)).analyze_screenshot(path, "mcma")
                self.assertNotIn("secret-value", str(caught.exception))
                post.assert_called_once()

    def test_persistent_sdk_reuses_one_helper_and_closes_it(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "vboxapi.py").write_text("from tests.fake_vbox import FakeManager as VirtualBoxManager\n")
            log = root / "sdk.jsonl"
            env = {"PYTHONPATH": os.pathsep.join((str(root), str(Path(__file__).resolve().parents[1]))),
                   "SETH_TEST_VBOX_LOG": str(log)}
            with patch.dict(os.environ, env):
                backend = VirtualBoxBackend(configuration(root))
                try:
                    backend.status()
                    process = backend._process
                    capture = SethCapture(configuration(root), backend)
                    for _ in range(3):
                        capture.capture_frame()
                    self.assertIs(backend._process, process)
                finally:
                    backend.close()
                self.assertIsNone(backend._process)
                self.assertIsNotNone(process.poll())
            events = [json.loads(line)["kind"] for line in log.read_text().splitlines()]
            self.assertEqual(events.count("capture"), 3)
            self.assertEqual(events.count("deinit"), 1)
            self.assertEqual(events.count("lock"), events.count("unlock"))
