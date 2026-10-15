import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
import requests
from ai_client import AIError, SethAIClient
from capture import Frame, SethCapture
from clicker import SethClicker
from main import SethEngine
from settings import load_config
from trigger import SethTrigger, TriggerError, send_trigger
from vbox_backend import GuestDisplay, VMError, VirtualBoxBackend, sdk_operation
from fake_vbox import FakeManager, TEST_DISPLAY


DECISION = {"action": "click", "click_x": 150, "click_y": 100,
            "confidence": 0.99, "explanation": "Select answer B."}


def configuration(root):
    return {
        "project_dir": str(root),
        "api": {"base_url": "http://127.0.0.1:1/v1", "api_key": "local-test-key", "model": "mock-vision"},
        "vm": {"name": "Seth Integration Test", "screen": 0},
        "capture": {"output_dir": "captures", "max_age_seconds": 90},
        "clicker": {"delay_ms": [0, 0]},
        "min_confidence": 0.85,
    }


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.client = SethAIClient(configuration("/tmp"))

    def test_endpoint_accepts_base_or_full_route(self):
        for base in ("https://example.com/v1", "https://example.com/v1/", "https://example.com/v1/chat/completions"):
            self.client.api["base_url"] = base
            self.assertEqual(self.client.endpoint, "https://example.com/v1/chat/completions")

    def test_bad_endpoints(self):
        for base in ("", "file:///tmp/api", "http://remote.example/v1", "https://key@example.com/v1", "https://example.com/v1?key=secret"):
            with self.subTest(base=base), self.assertRaises(AIError):
                self.client.api["base_url"] = base
                self.client.endpoint

    def test_no_action(self):
        self.assertIsNone(self.client.parse_coordinates('{"action":"none"}', 320, 200))

    def test_strict_invalid_decisions(self):
        invalid = ["x:150 y:100", "```json\n{}\n```", "[]", "null", "{}"]
        for changes in ({"click_x": -1}, {"click_x": 320}, {"click_y": 200},
                        {"click_x": True}, {"click_x": 150.1}, {"click_x": "150"},
                        {"confidence": math.nan}, {"confidence": 1.1}, {"confidence": True},
                        {"explanation": []}, {"action": "submit"}):
            invalid.append(json.dumps(DECISION | changes))
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(AIError):
                self.client.parse_coordinates(raw, 320, 200)

    def test_valid_decision_and_exclusive_bounds(self):
        for x, y in ((0, 0), (319, 199)):
            result = self.client.parse_coordinates(json.dumps(DECISION | {"click_x": x, "click_y": y}), 320, 200)
            self.assertEqual((result["click_x"], result["click_y"]), (x, y))

    def test_one_complete_json_fence_is_accepted_without_relaxing_schema(self):
        for opening in ("```json", "```", "```JSON"):
            raw = opening + "\n" + json.dumps(DECISION) + "\n```"
            self.assertEqual(self.client.parse_coordinates(raw, 320, 200), DECISION)
        self.assertIsNone(self.client.parse_coordinates('```json\n{"action":"none"}\n```', 320, 200))
        valid = '```json\n' + json.dumps(DECISION) + '\n```'
        for raw in (valid + '\nextra text', 'Here is JSON:\n' + valid, valid + '\n' + valid,
                    '```python\n' + json.dumps(DECISION) + '\n```',
                    '```json\n' + json.dumps(DECISION | {"click_x": 320}) + '\n```'):
            with self.subTest(raw=raw), self.assertRaises(AIError):
                self.client.parse_coordinates(raw, 320, 200)

    def test_env_file_and_environment_override(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            root = Path(root)
            (root / "config.json").write_text('{}')
            (root / ".env").write_text('INFERENCE_BASE_URL="https://local.example/v1"\nINFERENCE_API_KEY="file-secret"\nINFERENCE_MODEL="vision"\n')
            os.environ["INFERENCE_API_KEY"] = "environment-secret"
            config = load_config(root / "config.json")
            self.assertEqual(config["api"]["base_url"], "https://local.example/v1")
            self.assertEqual(config["api"]["api_key"], "environment-secret")
            self.assertEqual(config["api"]["model"], "vision")


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = SethEngine(configuration("/tmp"), execute=True)
        self.frame = Frame(Path("dummy.png"), GuestDisplay(**TEST_DISPLAY), time.monotonic())
        self.patches = [patch.object(self.engine.capture, "capture_frame", return_value=self.frame),
                        patch.object(self.engine.ai, "analyze_screenshot", return_value=json.dumps(DECISION)),
                        patch.object(self.engine.clicker, "click", return_value={"click_x": 150, "click_y": 100})]
        self.capture, self.analyze, self.click = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def test_exactly_one_click_then_return(self):
        self.assertEqual(self.engine.run_cycle()["status"], "clicked")
        self.capture.assert_called_once()
        self.analyze.assert_called_once()
        self.click.assert_called_once()
        self.assertEqual(self.engine.cycle_count, 1)

    def test_no_click_for_no_action_low_confidence_or_bad_coordinates(self):
        for decision, expected in (({"action": "none"}, "no_action"),
                                   (DECISION | {"confidence": 0.2}, "low_confidence"),
                                   (DECISION | {"click_x": 320}, "error")):
            with self.subTest(decision=decision):
                self.analyze.return_value = json.dumps(decision)
                self.assertEqual(self.engine.run_cycle()["status"], expected)
                self.click.assert_not_called()

    def test_dry_run_never_clicks(self):
        self.engine.execute = False
        self.assertEqual(self.engine.run_cycle()["status"], "dry_run")
        self.click.assert_not_called()

    def test_invalid_confidence_threshold_is_rejected(self):
        for threshold in (math.nan, -0.1, 2, True):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                SethEngine(configuration("/tmp") | {"min_confidence": threshold})

    def test_capture_api_or_click_failure_is_not_success(self):
        for operation in (self.capture, self.analyze, self.click):
            with self.subTest(operation=operation):
                operation.side_effect = VMError("test failure")
                self.assertEqual(self.engine.run_cycle()["status"], "error")
                self.assertEqual(self.engine.cycle_count, 0)
                operation.side_effect = None

    def test_cancel_during_inference(self):
        def cancel(*args):
            self.engine.stop_event.set()
            return json.dumps(DECISION)
        self.analyze.side_effect = cancel
        self.assertEqual(self.engine.run_cycle()["status"], "cancelled")
        self.click.assert_not_called()

    def test_concurrent_requests_are_dropped(self):
        started, release = threading.Event(), threading.Event()
        def blocked(*args):
            started.set()
            release.wait(3)
            return json.dumps(DECISION)
        self.analyze.side_effect = blocked
        try:
            self.assertTrue(self.engine.request_cycle())
            self.assertTrue(started.wait(2))
            self.assertFalse(self.engine.request_cycle())
            self.assertEqual(self.engine.run_cycle()["status"], "busy")
        finally:
            release.set()
            self.engine.wait()
        self.click.assert_called_once()


class ClickTests(unittest.TestCase):
    def setUp(self):
        self.frame = Frame(Path("dummy"), GuestDisplay(**TEST_DISPLAY), time.monotonic())
        self.backend = Mock()
        self.clicker = SethClicker(configuration("/tmp"), self.backend)

    def test_exact_pixels_are_forwarded_without_host_offsets_or_jitter(self):
        self.clicker.click(self.frame, 0, 0)
        self.backend.click.assert_called_once_with(self.frame, 0, 0, 90, stop_event=None)

    def test_cancelled_stale_future_or_invalid_coordinates_do_not_reach_sdk(self):
        stop = threading.Event()
        stop.set()
        for timestamp, x, y, event in ((self.frame.captured_at, 150, 100, stop),
                                       (time.monotonic() - 100, 150, 100, None),
                                       (time.monotonic() + 100, 150, 100, None),
                                       (self.frame.captured_at, 320, 100, None),
                                       (self.frame.captured_at, True, 100, None)):
            with self.subTest(timestamp=timestamp, x=x), self.assertRaises(VMError):
                self.clicker.click(Frame(self.frame.path, self.frame.display, timestamp), x, y, event)
        self.backend.click.assert_not_called()

    def test_stop_interrupts_delay_and_prevents_input(self):
        event = threading.Event()
        self.clicker.options["delay_ms"] = [10000, 10000]
        timer = threading.Timer(0.02, event.set)
        timer.start()
        try:
            start = time.monotonic()
            with self.assertRaises(VMError):
                self.clicker.click(self.frame, 150, 100, event)
            self.assertLess(time.monotonic() - start, 1)
            self.backend.click.assert_not_called()
        finally:
            timer.join()

    def test_invalid_delays_and_sdk_failure_never_report_success(self):
        for delay in ([1], [-1, 1], [10, 1], [0, math.nan], [True, 5], "invalid"):
            self.clicker.options["delay_ms"] = delay
            with self.subTest(delay=delay), self.assertRaises(VMError):
                self.clicker.click(self.frame, 150, 100)
        self.backend.click.assert_not_called()
        self.clicker.options["delay_ms"] = [0, 0]
        self.backend.click.side_effect = VMError("SDK failure")
        with self.assertRaises(VMError):
            self.clicker.click(self.frame, 150, 100)
        self.backend.click.assert_called_once()


class TriggerTests(unittest.TestCase):
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
    def __init__(self, decision=DECISION, status=200, content=None):
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, self.headers.get("Authorization"), payload))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
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


class HTTPTests(unittest.TestCase):
    def test_transport_failures_are_identified_without_echoing_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.png"
            Image.new("RGB", (320, 200)).save(path)
            for error, message in ((requests.Timeout("secret"), "timed out"),
                                   (requests.exceptions.SSLError("secret"), "TLS"),
                                   (requests.ConnectionError("secret"), "Cannot connect"),
                                   (requests.RequestException("secret"), "request failed")):
                with self.subTest(error=type(error).__name__), patch("ai_client.requests.post", side_effect=error):
                    with self.assertRaisesRegex(AIError, message) as caught:
                        SethAIClient(configuration(root)).analyze_screenshot(path)
                    self.assertNotIn("secret", str(caught.exception))

    def test_http_errors_and_invalid_response_shapes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.png"
            Image.new("RGB", (320, 200)).save(path)
            for status, content in ((401, None), (429, None), (302, None), (200, {}), (200, {"choices": []}),
                                    (200, {"error": {"message": "Provider rejected request"}})):
                with self.subTest(status=status, content=content), LocalAPI(status=status, content=content) as api:
                    config = configuration(root)
                    config["api"]["base_url"] = api.url
                    with self.assertRaises(AIError):
                        SethAIClient(config).analyze_screenshot(path)
                    self.assertEqual(len(api.requests), 1)


class VirtualBoxSDKTests(unittest.TestCase):
    def request(self, **changes):
        return {"operation": "click", "target": TEST_DISPLAY["vm_uuid"], "screen": 0,
                "expected": TEST_DISPLAY.copy(), "captured_at": time.monotonic(),
                "max_age": 90, "x": 150, "y": 100, **changes}

    def test_absolute_coordinates_include_guest_origin_and_one_based_conversion(self):
        for x, y in ((0, 0), (319, 199)):
            manager = FakeManager()
            manager.console.display.origin = (-320, 120)
            expected = TEST_DISPLAY | {"x_origin": -320, "y_origin": 120}
            result = sdk_operation(self.request(x=x, y=y, expected=expected), lambda *args: manager)
            expected_xy = (x - 320 + 1, y + 120 + 1, 0, 0)
            self.assertEqual(manager.mouse.events, [expected_xy + (0,), expected_xy + (1,), expected_xy + (0,)])
            self.assertEqual(result["click_x"], x)
            self.assertTrue(manager.unlocked)
            self.assertTrue(manager.closed)

    def test_stale_or_changed_vm_and_unsupported_input_never_send_mouse_events(self):
        for change in ("uuid", "resolution", "origin", "restart", "state_change", "paused", "absolute", "disabled", "screen", "stale", "future", "coordinates"):
            with self.subTest(change=change):
                manager = FakeManager()
                request = self.request()
                if change == "uuid": manager.machine.id = "different-vm"
                if change == "resolution": manager.console.display.size = (640, 480)
                if change == "origin": manager.console.display.origin = (320, 0)
                if change == "restart": manager.machine.sessionPID += 1
                if change == "state_change": manager.machine.lastStateChange += 1
                if change == "paused": manager.machine.state = -1
                if change == "absolute": manager.mouse.absoluteSupported = False
                if change == "disabled": manager.console.display.status = 0
                if change == "screen": request["screen"] = 1
                if change == "stale": request["captured_at"] -= 100
                if change == "future": request["captured_at"] += 100
                if change == "coordinates": request["x"] = True
                with self.assertRaises(VMError):
                    sdk_operation(request, lambda *args: manager)
                self.assertEqual(manager.mouse.events, [])
                self.assertTrue(manager.closed)
                if change != "paused": self.assertTrue(manager.unlocked)

    def test_change_after_move_prevents_press(self):
        manager = FakeManager()
        manager.mouse.after_move = lambda: setattr(manager.console.display, "size", (640, 480))
        with self.assertRaises(VMError):
            sdk_operation(self.request(), lambda *args: manager)
        self.assertEqual([event[-1] for event in manager.mouse.events], [0])
        self.assertTrue(manager.unlocked)

    def test_press_or_release_failure_attempts_release_and_never_retries(self):
        for failure in ("press", "release"):
            with self.subTest(failure=failure):
                manager = FakeManager()
                manager.mouse.failure = failure
                with self.assertRaises(VMError):
                    sdk_operation(self.request(), lambda *args: manager)
                self.assertEqual([event[-1] for event in manager.mouse.events], [0, 1, 0])
                self.assertTrue(manager.unlocked)
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

    def test_click_helper_is_pinned_to_capture_uuid_and_receives_no_api_config(self):
        backend = VirtualBoxBackend(configuration("/tmp"))
        frame = Frame(Path("dummy"), GuestDisplay(**TEST_DISPLAY), time.monotonic())
        process = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": {}}), "")
        with patch.dict(os.environ, {"INFERENCE_API_KEY": "test-secret"}), patch("vbox_backend.subprocess.run", return_value=process) as run:
            backend.click(frame, 150, 100, 90)
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["target"], TEST_DISPLAY["vm_uuid"])
        self.assertNotIn("api", request)
        self.assertNotIn("local-test-key", run.call_args.kwargs["input"])
        self.assertNotIn("INFERENCE_API_KEY", run.call_args.kwargs["env"])

    def test_cancel_reaches_helper_before_press(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "cancelled"
            manager = FakeManager()
            manager.mouse.after_move = lambda: marker.touch()
            with self.assertRaisesRegex(VMError, "cancelled"):
                sdk_operation(self.request(cancel_path=str(marker)), lambda *args: manager)
            self.assertEqual([event[-1] for event in manager.mouse.events], [0])
            self.assertTrue(manager.unlocked)

    def test_cancellation_is_relayed_during_native_io_and_marker_is_cleaned_up(self):
        stop = threading.Event()
        backend = VirtualBoxBackend(configuration("/tmp"))
        frame = Frame(Path("dummy"), GuestDisplay(**TEST_DISPLAY), time.monotonic())
        markers = []

        def blocked(operation, **data):
            marker = Path(data["cancel_path"])
            markers.append(marker)
            stop.set()
            deadline = time.monotonic() + 1
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            raise VMError("Cycle cancelled; no click.")

        with patch.object(backend, "_request", side_effect=blocked):
            with self.assertRaises(VMError):
                backend.click(frame, 150, 100, 90, stop_event=stop)
        self.assertFalse(markers[0].parent.exists())

    def test_sleeping_display_does_not_capture_or_send_input(self):
        manager = FakeManager()
        manager.console.display.status = 2
        with self.assertRaisesRegex(VMError, "blank/asleep"):
            sdk_operation({"operation": "capture", "target": "test", "screen": 0, "path": "unused.png"},
                          lambda *args: manager)
        self.assertTrue(manager.unlocked)
        self.assertEqual(manager.mouse.events, [])

    def test_helper_failure_or_timeout_is_not_retried(self):
        backend = VirtualBoxBackend(configuration("/tmp"))
        for result in (subprocess.CompletedProcess([], 1, "not json", ""),
                       subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": "SDK missing"}), ""),
                       subprocess.TimeoutExpired("sdk", 30)):
            with self.subTest(result=result), patch("vbox_backend.subprocess.run") as run:
                if isinstance(result, Exception): run.side_effect = result
                else: run.return_value = result
                with self.assertRaises(VMError): backend.status()
                run.assert_called_once()


class VirtualBoxIntegrationTests(unittest.TestCase):
    def test_helper_capture_http_and_one_click_with_fake_sdk(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "vboxapi.py").write_text("from fake_vbox import FakeManager as VirtualBoxManager\n")
            log = root / "sdk.jsonl"
            env = {"PYTHONPATH": os.pathsep.join((str(root), str(Path(__file__).parent.resolve()))),
                   "SETH_TEST_VBOX_LOG": str(log)}
            with patch.dict(os.environ, env), LocalAPI() as api:
                config = configuration(root)
                config["api"]["base_url"] = api.url
                engine = SethEngine(config, execute=True)
                result = engine.run_cycle()
                self.assertEqual(result["status"], "clicked", result)
                events = [json.loads(line) for line in log.read_text().splitlines()]
                mouse = [event["args"] for event in events if event["kind"] == "mouse"]
                self.assertEqual(mouse, [[151, 101, 0, 0, 0], [151, 101, 0, 0, 1], [151, 101, 0, 0, 0]])
                self.assertEqual(sum(event["kind"] == "unlock" for event in events), 2)
                self.assertEqual(sum(event["kind"] == "deinit" for event in events), 2)
                self.assertEqual(len(api.requests), 1)
                route, auth, payload = api.requests[0]
                self.assertEqual(route, "/v1/chat/completions")
                self.assertEqual(auth, "Bearer local-test-key")
                self.assertIs(payload["stream"], False)
                encoded = payload["messages"][0]["content"][1]["image_url"]["url"].split(",", 1)[1]
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                    self.assertEqual(image.size, (320, 200))
                capture = next((root / "captures").glob("*.png"))
                self.assertEqual(capture.stat().st_mode & 0o777, 0o600)
                self.assertEqual(engine.cycle_count, 1)
                # A listener worker also uses fresh helpers and remains a dry run.
                engine.execute = False
                self.assertTrue(engine.request_cycle())
                engine.wait()
                self.assertEqual(len(api.requests), 2)
                events_after = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(sum(event["kind"] == "mouse" for event in events_after), 3)


if __name__ == "__main__":
    unittest.main()
