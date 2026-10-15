import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from ai_client import AIError, SethAIClient
from capture import DesktopError, Frame, SethCapture, Window
from clicker import SethClicker
from main import SethEngine
from settings import load_config
from trigger import SethTrigger, TriggerError, send_trigger


DECISION = {"action": "click", "click_x": 150, "click_y": 100,
            "confidence": 0.99, "explanation": "Select answer B."}


def configuration(root):
    return {
        "project_dir": str(root),
        "api": {"base_url": "http://127.0.0.1:1/v1", "api_key": "local-test-key", "model": "mock-vision"},
        "vm": {"window_name": "Seth Integration Test"},
        "capture": {"output_dir": "captures", "max_age_seconds": 90},
        "clicker": {"smooth_movement": False, "delay_ms": [0, 0], "jitter_pixels": 0},
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
        self.frame = Frame(Path("dummy.png"), Window("1", 40, 60, 320, 200), time.monotonic())
        self.patches = [patch.object(self.engine.capture, "capture_frame", return_value=self.frame),
                        patch.object(self.engine.ai, "analyze_screenshot", return_value=json.dumps(DECISION)),
                        patch.object(self.engine.clicker, "human_click", return_value={"click_x": 150, "click_y": 100})]
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
                operation.side_effect = DesktopError("test failure")
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
    def test_jitter_is_clamped_and_failure_propagates(self):
        frame = Frame(Path("dummy"), Window("1", 40, 60, 320, 200), time.monotonic())
        clicker = SethClicker(configuration("/tmp"))
        clicker.options["jitter_pixels"] = 2
        with patch.object(clicker, "check_target"), patch("clicker.random.randint", return_value=-2), patch("clicker.run_xdotool") as run:
            self.assertEqual(clicker.human_click(frame, 0, 0)["screen_x"], 40)
            run.assert_called_once_with("mousemove", 40, 60, "click", "1")
            run.side_effect = DesktopError("click failed")
            with self.assertRaises(DesktopError):
                clicker.human_click(frame, 0, 0)

    def test_moved_unfocused_or_stale_window_never_clicks(self):
        frame = Frame(Path("dummy"), Window("1", 40, 60, 320, 200), time.monotonic())
        for geometry, active, captured_at in ((Window("1", 41, 60, 320, 200), "1", frame.captured_at),
                                              (frame.window, "2", frame.captured_at),
                                              (frame.window, "1", time.monotonic() - 100)):
            with self.subTest(geometry=geometry, active=active, captured_at=captured_at):
                candidate = Frame(frame.path, frame.window, captured_at)
                with patch("clicker.window_geometry", return_value=geometry), patch("clicker.run_xdotool", return_value=active) as run:
                    with self.assertRaises(DesktopError):
                        SethClicker(configuration("/tmp")).human_click(candidate, 150, 100)
                    self.assertFalse(any(call.args[0] == "mousemove" for call in run.call_args_list))


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
    def test_http_errors_and_invalid_response_shapes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.png"
            Image.new("RGB", (320, 200)).save(path)
            for status, content in ((401, None), (429, None), (302, None), (200, {}), (200, {"choices": []})):
                with self.subTest(status=status, content=content), LocalAPI(status=status, content=content) as api:
                    config = configuration(root)
                    config["api"]["base_url"] = api.url
                    with self.assertRaises(AIError):
                        SethAIClient(config).analyze_screenshot(path)
                    self.assertEqual(len(api.requests), 1)


@unittest.skipUnless(all(shutil.which(name) for name in ("Xvfb", "xwd", "xdotool")) and
                     (shutil.which("magick") or shutil.which("convert")), "X11 test tools unavailable")
class DesktopIntegrationTests(unittest.TestCase):
    def test_real_capture_http_and_single_click_on_isolated_display(self):
        try:
            from Xlib import X, display
        except ImportError:
            self.skipTest("python-xlib is required only for this desktop integration test")
        xvfb = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "800x600x24", "-nolisten", "tcp"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(xvfb.stderr.close)
        self.addCleanup(xvfb.stdout.close)
        self.addCleanup(lambda: (xvfb.terminate(), xvfb.wait(timeout=5)))
        self.assertTrue(select.select([xvfb.stdout], [], [], 5)[0], "Xvfb did not start")
        number = xvfb.stdout.readline().strip()
        self.assertTrue(number.isdigit())
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DISPLAY": ":" + number, "XAUTHORITY": "/dev/null"}):
            connection = display.Display()
            try:
                screen = connection.screen()
                window = screen.root.create_window(40, 60, 320, 200, 0, screen.root_depth,
                                                   background_pixel=screen.white_pixel,
                                                   event_mask=X.ButtonPressMask)
                window.set_wm_name("Seth Integration Test")
                window.map()
                connection.sync()
                window.set_input_focus(X.RevertToParent, X.CurrentTime)
                gc = window.create_gc(foreground=screen.black_pixel)
                window.draw_text(gc, 20, 30, "Test question: select B")
                window.rectangle(gc, 120, 80, 60, 40)
                window.draw_text(gc, 145, 105, "B")
                connection.sync()
                with LocalAPI() as api:
                    config = configuration(root)
                    config["api"]["base_url"] = api.url
                    engine = SethEngine(config, execute=True)
                    result = engine.run_cycle()
                    self.assertEqual(result["status"], "clicked", result)
                    self.assertEqual(result["click"]["screen_x"], 190)
                    self.assertEqual(result["click"]["screen_y"], 160)
                    presses = []
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        while connection.pending_events():
                            event = connection.next_event()
                            if event.type == X.ButtonPress:
                                presses.append((event.event_x, event.event_y))
                        if presses:
                            break
                        time.sleep(0.01)
                    self.assertEqual(presses, [(150, 100)])
                    self.assertEqual(len(api.requests), 1)
                    route, auth, payload = api.requests[0]
                    self.assertEqual(route, "/v1/chat/completions")
                    self.assertEqual(auth, "Bearer local-test-key")
                    encoded = payload["messages"][0]["content"][1]["image_url"]["url"].split(",", 1)[1]
                    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                        self.assertEqual(image.size, (320, 200))
                    self.assertEqual(engine.cycle_count, 1)
                    self.assertEqual(len(list((Path(root) / "captures").glob("*.png"))), 1)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
