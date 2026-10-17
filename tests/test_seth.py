import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw
import requests
from ai_client import AIError, SethAIClient
from capture import Frame, SethCapture
from clicker import SethClicker
from main import SethEngine
from settings import load_config
from trigger import SethTrigger, TriggerError, send_trigger
from vbox_backend import GuestDisplay, VMError, VirtualBoxBackend, sdk_operation
from fake_vbox import FakeManager, TEST_DISPLAY


DECISION = {"action": "click", "coordinate_space": "pixels", "click_x": 150, "click_y": 100,
            "confidence": 0.99, "explanation": "Select answer B."}

CLASSIFICATION = {"action": "select", "selection_type": "classification", "coordinate_space": "pixels",
                  "selections": [{"row": 1, "choice": "False", "click_x": 150, "click_y": 60},
                                 {"row": 2, "choice": "True", "click_x": 150, "click_y": 100},
                                 {"row": 3, "choice": "False", "click_x": 150, "click_y": 140}],
                  "confidence": 0.99, "explanation": "Classify each row."}
MULTIPLE_ANSWER = {"action": "select", "selection_type": "multiple_answer", "coordinate_space": "pixels",
                   "selections": [{"option": "B", "click_x": 150, "click_y": 60},
                                  {"option": "D", "click_x": 150, "click_y": 140}],
                   "confidence": 0.99, "explanation": "Both B and D are correct."}


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

    def test_abstention_confidence_is_optional_validated_and_never_reused(self):
        invalid = (None, True, "0", -0.1, 1.1, math.nan, math.inf, [], {})
        for value in (0, .6, 1):
            with self.subTest(confidence=value):
                self.assertIsNone(self.client.parse_coordinates(json.dumps(
                    {"action": "none", "confidence": value}), 320, 200))
                self.assertEqual(self.client.last_no_action_confidence, value)
        for value in invalid:
            with self.subTest(invalid=value):
                self.client.parse_coordinates('{"action":"none","confidence":0.6}', 320, 200)
                self.assertIsNone(self.client.parse_coordinates(json.dumps(
                    {"action": "none", "confidence": value}), 320, 200))
                self.assertIsNone(self.client.last_no_action_confidence)
        for raw in ('{"action":"none"}', json.dumps(DECISION), "not JSON"):
            with self.subTest(next_response=raw):
                self.client.parse_coordinates('{"action":"none","confidence":0.6}', 320, 200)
                if raw == "not JSON":
                    with self.assertRaises(AIError):
                        self.client.parse_coordinates(raw, 320, 200)
                else:
                    self.client.parse_coordinates(raw, 320, 200)
                self.assertIsNone(self.client.last_no_action_confidence)

    def test_no_action_explanation_is_optional_and_never_reused(self):
        reason = "The computed answer does not match any listed option."
        for fields in ({}, {"explanation": None}, {"explanation": []},
                       {"explanation": 7}, {"explanation": "   "}):
            with self.subTest(fields=fields):
                self.assertIsNone(self.client.parse_coordinates(json.dumps(
                    {"action": "none", "explanation": reason}), 320, 200))
                self.assertEqual(self.client.last_no_action_explanation, reason)
                self.assertIsNone(self.client.parse_coordinates(json.dumps(
                    {"action": "none"} | fields), 320, 200))
                self.assertIsNone(self.client.last_no_action_explanation)

    def test_selection_and_invalid_response_clear_previous_abstention_reason(self):
        for raw in (json.dumps(DECISION), "not JSON"):
            with self.subTest(raw=raw):
                self.client.parse_coordinates(
                    '{"action":"none","explanation":"Earlier question was ambiguous."}', 320, 200)
                if raw == "not JSON":
                    with self.assertRaises(AIError):
                        self.client.parse_coordinates(raw, 320, 200)
                else:
                    self.client.parse_coordinates(raw, 320, 200)
                self.assertIsNone(self.client.last_no_action_explanation)

    def test_strict_invalid_decisions(self):
        invalid = ["x:150 y:100", "```json\n{}\n```", "[]", "null", "{}"]
        for changes in ({"click_x": -1}, {"click_x": 320}, {"click_y": 200},
                        {"click_x": True}, {"click_x": 150.1}, {"click_x": "150"},
                        {"confidence": math.nan}, {"confidence": 1.1}, {"confidence": True},
                        {"coordinate_space": None}, {"coordinate_space": "unknown"},
                        {"explanation": []}, {"action": "submit"}):
            invalid.append(json.dumps(DECISION | changes))
        invalid.append(json.dumps({name: value for name, value in DECISION.items() if name != "coordinate_space"}))
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

    def test_normalized_points_map_to_actual_screenshot_pixels(self):
        # Regression: Gemini located a 1920x1080 radio button with a 0..1000
        # point. Interpreting (227,647) as pixels clicked the empty left margin.
        raw = json.dumps(DECISION | {"coordinate_space": "normalized_1000", "click_x": 227, "click_y": 647})
        result = self.client.parse_coordinates(raw, 1920, 1080)
        self.assertEqual((result["click_x"], result["click_y"]), (436, 699))
        self.assertEqual(result["coordinate_space"], "pixels")
        for x, y, expected in ((0, 0, (0, 0)), (1000, 1000, (319, 199)), (500, 500, (160, 100))):
            raw = json.dumps(DECISION | {"coordinate_space": "normalized_1000", "click_x": x, "click_y": y})
            result = self.client.parse_coordinates(raw, 320, 200)
            self.assertEqual((result["click_x"], result["click_y"]), expected)

    def test_invalid_normalized_points_or_dimensions_are_rejected(self):
        for x, y in ((-1, 500), (1001, 500), (500, 1001), (True, 500), (500, 500.1)):
            raw = json.dumps(DECISION | {"coordinate_space": "normalized_1000", "click_x": x, "click_y": y})
            with self.subTest(x=x, y=y), self.assertRaises(AIError):
                self.client.parse_coordinates(raw, 1920, 1080)
        for width, height in ((0, 1080), (1920, -1), (True, 1080)):
            with self.subTest(width=width, height=height), self.assertRaises(AIError):
                self.client.parse_coordinates(json.dumps(DECISION), width, height)

    def test_classification_and_multiple_answer_plans_are_validated(self):
        for plan in (CLASSIFICATION, MULTIPLE_ANSWER):
            self.assertEqual(self.client.parse_coordinates(json.dumps(plan), 320, 200), plan)
        invalid = [CLASSIFICATION | {"selections": []}, CLASSIFICATION | {"selections": [None]},
                   CLASSIFICATION | {"selection_type": "unknown"},
                   CLASSIFICATION | {"selections": CLASSIFICATION["selections"] * 4},
                   CLASSIFICATION | {"selections": [CLASSIFICATION["selections"][0]] * 2},
                   CLASSIFICATION | {"selections": [{"row": True, "choice": "True", "click_x": 10, "click_y": 10}]},
                   CLASSIFICATION | {"selections": [{"row": 1, "click_x": 10, "click_y": 10}]},
                   MULTIPLE_ANSWER | {"selections": [{"click_x": 10, "click_y": 10}]},
                   MULTIPLE_ANSWER | {"selections": [MULTIPLE_ANSWER["selections"][0], {"option": "b", "click_x": 151, "click_y": 61}]},
                   MULTIPLE_ANSWER | {"selections": [MULTIPLE_ANSWER["selections"][0], {"option": "D", "click_x": 150, "click_y": 60}]}]
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(AIError):
                self.client.parse_coordinates(json.dumps(plan), 320, 200)

    def test_normalized_multi_selection_points_are_converted_individually(self):
        plan = MULTIPLE_ANSWER | {"coordinate_space": "normalized_1000",
                                 "selections": [{"option": "B", "click_x": 469, "click_y": 300},
                                                {"option": "D", "click_x": 469, "click_y": 700}]}
        parsed = self.client.parse_coordinates(json.dumps(plan), 320, 200)
        self.assertEqual([(p["click_x"], p["click_y"]) for p in parsed["selections"]], [(150, 60), (150, 140)])

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
        guard = patch.object(self.engine.capture, "verify_target")
        self.verify = guard.start()
        self.addCleanup(guard.stop)

    def test_exactly_one_click_then_return(self):
        self.assertEqual(self.engine.run_cycle()["status"], "clicked")
        self.capture.assert_called_once()
        self.analyze.assert_called_once()
        self.click.assert_called_once()
        self.assertEqual(self.engine.cycle_count, 1)
        self.assertEqual(self.engine.progress, ("done", "Done"))

    def test_qwen_click_is_successful_only_after_visible_selection(self):
        self.engine.ai.last_provider = "qwen"
        events = []
        self.verify.side_effect = lambda *args: events.append("before")
        self.click.side_effect = lambda *args: events.append("click") or {"click_x": 150, "click_y": 100}
        with patch.object(self.engine.capture, "verify_selection", side_effect=lambda *args: events.append("after")) as confirm:
            result = self.engine.run_cycle()
        self.assertEqual(events, ["before", "click", "after"])
        self.assertEqual(result["status"], "clicked")
        self.assertTrue(result["click"]["selection_verified"])
        confirm.assert_called_once_with(self.frame, DECISION, self.engine.stop_event)

    def test_qwen_missed_click_is_an_error_and_is_never_retried(self):
        self.engine.ai.last_provider = "qwen"
        with patch.object(self.engine.capture, "verify_selection", side_effect=VMError("Control stayed unchecked")):
            result = self.engine.run_cycle()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["clicks_completed"], 1)
        self.assertEqual(self.engine.cycle_count, 0)
        self.click.assert_called_once()

    def test_scrolled_question_uses_context_and_rechecks_identity_before_input(self):
        self.engine.ai.supports_scroll_context = True
        self.engine.config["capture"]["scroll_context"] = {"enabled": True}
        bundle = Mock(frame=self.frame, contexts=[{"path": "context.png"}], regions=[(1, 1, 100, 100)])
        with patch("scroll_capture.ScrollCollector") as collector:
            collector.return_value.collect.return_value = bundle
            result = self.engine.run_cycle()
        self.assertEqual(result["status"], "clicked")
        self.analyze.assert_called_once_with(self.frame.path, context_images=bundle.contexts, control_regions=bundle.regions)
        bundle.verify_identity.assert_called_once_with(self.engine.capture, self.engine.stop_event)
        self.assertEqual(result["context_sections"], 1)
        self.assertEqual(result["capture_path"], str(self.frame.path))

    def test_scrolled_question_header_change_prevents_every_answer_click(self):
        self.engine.ai.supports_scroll_context = True
        self.engine.config["capture"]["scroll_context"] = {"enabled": True}
        bundle = Mock(frame=self.frame, contexts=[{"path": "context.png"}], regions=[])
        bundle.verify_identity.side_effect = VMError("Question header changed")
        with patch("scroll_capture.ScrollCollector") as collector:
            collector.return_value.collect.return_value = bundle
            result = self.engine.run_cycle()
        self.assertEqual(result["status"], "error")
        self.assertIn("header changed", result["error"])
        self.click.assert_not_called()

    def test_qwen_plan_stops_before_next_target_when_confirmation_fails(self):
        self.engine.ai.last_provider = "qwen"
        self.analyze.return_value = json.dumps(CLASSIFICATION)
        with patch.object(self.engine.capture, "verify_selection", side_effect=[None, VMError("Control stayed unchecked")]):
            result = self.engine.run_cycle()
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.click.call_count, 2)
        self.assertTrue(result["clicks"][0]["selection_verified"])
        self.assertNotIn("selection_verified", result["clicks"][1])
        self.assertEqual(self.engine.cycle_count, 0)

    def test_progress_tracks_capture_inference_and_selection(self):
        observed = []
        def capture():
            observed.append(self.engine.progress[0])
            return self.frame
        def analyze(*args):
            observed.append(self.engine.progress[0])
            return json.dumps(DECISION)
        def click(*args):
            observed.append(self.engine.progress[0])
            return {"click_x": 150, "click_y": 100}
        self.capture.side_effect = capture
        self.analyze.side_effect = analyze
        self.click.side_effect = click
        self.assertEqual(self.engine.run_cycle()["status"], "clicked")
        self.assertEqual(observed, ["capturing", "thinking", "selecting"])
        self.assertEqual(self.engine.progress, ("done", "Done"))

    def test_no_click_for_no_action_low_confidence_or_bad_coordinates(self):
        for decision, expected in (({"action": "none"}, "no_action"),
                                   (DECISION | {"confidence": 0.2}, "low_confidence"),
                                   (DECISION | {"click_x": 320}, "error")):
            with self.subTest(decision=decision):
                self.analyze.return_value = json.dumps(decision)
                self.assertEqual(self.engine.run_cycle()["status"], expected)
                self.assertEqual(self.engine.progress[0], expected)
                self.click.assert_not_called()

    def test_no_action_result_and_log_include_only_current_valid_explanation(self):
        reason = "The computed answer does not match any listed option."
        for fields in ({}, {"explanation": None}, {"explanation": []}):
            with self.subTest(fields=fields):
                self.analyze.return_value = json.dumps({"action": "none", "explanation": reason})
                with self.assertLogs("seth", level="INFO") as messages:
                    result = self.engine.run_cycle()
                self.assertEqual(result, {"status": "no_action", "explanation": reason})
                self.assertIn(json.dumps(result), messages.output[-1])
                self.analyze.return_value = json.dumps({"action": "none"} | fields)
                with self.assertLogs("seth", level="INFO") as messages:
                    result = self.engine.run_cycle()
                self.assertEqual(result, {"status": "no_action"})
                self.assertNotIn(reason, messages.output[-1])
                self.click.assert_not_called()
                self.verify.assert_not_called()

    def test_no_action_confidence_is_logged_without_clicking_or_guessing_missing_values(self):
        for value in (0, .6, 1, None, True, "0.6", math.nan):
            with self.subTest(confidence=value):
                self.analyze.return_value = '{"action":"none","confidence":0.99}'
                self.engine.run_cycle()
                self.analyze.return_value = json.dumps({"action": "none", "confidence": value})
                with self.assertLogs("seth", level="INFO") as messages:
                    result = self.engine.run_cycle()
                expected = {"status": "no_action"}
                if type(value) in (int, float) and math.isfinite(value):
                    expected["confidence"] = value
                self.assertEqual(result, expected)
                self.assertIn(json.dumps(expected), messages.output[-1])
                self.click.assert_not_called()
                self.verify.assert_not_called()

    def test_dry_run_never_clicks(self):
        self.engine.execute = False
        self.assertEqual(self.engine.run_cycle()["status"], "dry_run")
        self.click.assert_not_called()

    def test_one_model_plan_selects_every_table_row_or_correct_checkbox(self):
        for plan in (CLASSIFICATION, MULTIPLE_ANSWER):
            with self.subTest(kind=plan["selection_type"]):
                self.click.reset_mock()
                self.verify.reset_mock()
                self.analyze.return_value = json.dumps(plan)
                result = self.engine.run_cycle()
                self.assertEqual(result["status"], "clicked", result)
                self.assertEqual(result["click_count"], len(plan["selections"]))
                self.assertEqual(self.click.call_count, len(plan["selections"]))
                self.assertEqual(self.verify.call_count, len(plan["selections"]))
                self.assertEqual([(c.args[1], c.args[2]) for c in self.click.call_args_list],
                                 [(p["click_x"], p["click_y"]) for p in plan["selections"]])

    def test_invalid_final_target_low_confidence_or_dry_plan_clicks_nothing(self):
        invalid = CLASSIFICATION | {"selections": CLASSIFICATION["selections"][:2] +
                                   [CLASSIFICATION["selections"][2] | {"click_x": 320}]}
        for plan, expected in ((invalid, "error"), (CLASSIFICATION | {"confidence": 0.2}, "low_confidence")):
            self.analyze.return_value = json.dumps(plan)
            self.assertEqual(self.engine.run_cycle()["status"], expected)
        self.engine.execute = False
        self.analyze.return_value = json.dumps(CLASSIFICATION)
        self.assertEqual(self.engine.run_cycle()["status"], "dry_run")
        self.click.assert_not_called()
        self.verify.assert_not_called()

    def test_mid_plan_failure_or_changed_target_stops_and_reports_partial_progress(self):
        for operation in (self.click, self.verify):
            with self.subTest(operation=operation):
                self.click.reset_mock(side_effect=True)
                self.verify.reset_mock(side_effect=True)
                operation.side_effect = [{"click_x": 150, "click_y": 60}, VMError("target changed or input failed")]
                self.analyze.return_value = json.dumps(CLASSIFICATION)
                result = self.engine.run_cycle()
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["clicks_completed"], 1)
                self.assertEqual(self.engine.progress, ("error", "Stopped (1)"))
                self.assertEqual(self.engine.cycle_count, 0)
                self.assertLessEqual(self.click.call_count, 2)

    def test_cancellation_between_table_rows_stops_further_input(self):
        self.analyze.return_value = json.dumps(CLASSIFICATION)
        def cancel_after_first(*args):
            self.engine.stop_event.set()
            return {"click_x": 150, "click_y": 60}
        self.click.side_effect = cancel_after_first
        result = self.engine.run_cycle()
        self.assertEqual(result["clicks_completed"], 1)
        self.click.assert_called_once()

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

    def test_live_legacy_listener_and_regular_file_are_not_replaced(self):
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


class HTTPTests(unittest.TestCase):
    def test_api_mime_follows_png_or_jpeg_bytes_even_when_the_extension_differs(self):
        event = lambda obj: 'data: ' + json.dumps(obj) + '\n\n'
        stream = (event({'choices':[{'delta':{'content':json.dumps(DECISION)}}]})
                  + event({'choices':[{'delta':{},'finish_reason':'stop'}]}) + 'data: [DONE]\n\n')
        for streaming in (False, True):
            for image_format, suffix, mime in (('PNG','.png','image/png'), ('JPEG','.jpg','image/jpeg'),
                                                ('JPEG','.jpeg','image/jpeg'), ('JPEG','.png','image/jpeg'),
                                                ('PNG','.jpg','image/png')):
                with self.subTest(streaming=streaming,image_format=image_format,suffix=suffix), \
                        tempfile.TemporaryDirectory() as root, LocalAPI(stream_events=stream if streaming else None) as api:
                    path=Path(root)/('question'+suffix)
                    Image.new('RGB',(320,200),'white').save(path,format=image_format)
                    original=path.read_bytes()
                    config=configuration(root);config['api'].update(base_url=api.url,stream=streaming)
                    client=SethAIClient(config)
                    self.assertEqual(client.parse_coordinates(client.analyze_screenshot(path),320,200),DECISION)
                    self.assertEqual(len(api.requests),1)
                    url=api.requests[0][2]['messages'][0]['content'][1]['image_url']['url']
                    self.assertTrue(url.startswith('data:'+mime+';base64,'))
                    self.assertEqual(base64.b64decode(url.split(',',1)[1]),original)
                    self.assertEqual(path.read_bytes(),original)

    def test_api_rejects_unsupported_image_bytes_before_posting(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'looks-like-png.png'
            for image_format in ('GIF','BMP'):
                Image.new('RGB',(320,200),'white').save(path,format=image_format)
                with self.subTest(image_format=image_format),patch('ai_client.requests.post') as post, \
                        self.assertRaises(AIError):
                    SethAIClient(configuration(root)).analyze_screenshot(path)
                post.assert_not_called()

    def test_scroll_context_uploads_one_combined_png_and_maps_ids_to_the_live_frame(self):
        answer = {'action': 'click', 'target_id': '01', 'confidence': .95, 'explanation': 'B'}
        event = lambda obj: 'data: ' + json.dumps(obj) + '\n\n'
        stream = (event({'choices': [{'delta': {'content': json.dumps(answer)}}]})
                  + event({'choices': [{'delta': {}, 'finish_reason': 'stop'}]}) + 'data: [DONE]\n\n')
        for streaming,image_format in ((False,'PNG'),(True,'PNG'),(False,'JPEG'),(True,'JPEG')):
            with self.subTest(streaming=streaming,image_format=image_format), tempfile.TemporaryDirectory() as root, \
                    LocalAPI(decision=answer, stream_events=stream if streaming else None) as api:
                path = Path(root) / ('question.png' if image_format=='PNG' else 'question.jpg')
                image = Image.new('RGB', (320, 200), 'white')
                ImageDraw.Draw(image).ellipse((169, 89, 190, 110), outline='#aaa', width=2)
                image.save(path,format=image_format,quality=100,subsampling=0)
                original = path.read_bytes()
                config = configuration(root)
                config['api'].update(base_url=api.url, stream=streaming)
                client = SethAIClient(config)
                raw = client.analyze_screenshot(path, context_images=[{'path': path, 'box': (0, 0, 160, 80)}],
                                               control_regions=[(160, 40, 240, 150)])
                result = client.parse_coordinates(raw, 320, 200)
                self.assertEqual((result['click_x'], result['click_y']), (180, 100))
                self.assertEqual(result['target_control']['box'], [169, 89, 191, 111])
                self.assertTrue(client.last_grounded)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(len(api.requests), 1)
                content = api.requests[0][2]['messages'][0]['content']
                self.assertIn('LIVE ANSWER VIEW', content[0]['text'])
                self.assertTrue(content[1]['image_url']['url'].startswith('data:image/png;base64,'))
                png = base64.b64decode(content[1]['image_url']['url'].split(',', 1)[1])
                with Image.open(io.BytesIO(png)) as sent:
                    self.assertEqual(sent.width, 320)
                    self.assertGreater(sent.height, 200)

    def test_scroll_context_invalid_control_is_rejected_after_one_request(self):
        with tempfile.TemporaryDirectory() as root, LocalAPI(decision={
                'action': 'click', 'target_id': '99', 'confidence': .99, 'explanation': 'Bad ID'}) as api:
            path = Path(root) / 'question.png'
            Image.new('RGB', (320, 200), 'white').save(path)
            config = configuration(root)
            config['api'].update(base_url=api.url)
            client = SethAIClient(config)
            with self.assertRaises(AIError):
                client.analyze_screenshot(path, context_images=[{'path': path, 'box': (0, 0, 160, 80)}])
            self.assertFalse(client.last_grounded)
            self.assertEqual(len(api.requests), 1)

    def test_unresolvable_gateway_is_reported_without_retry_or_click(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.png"
            Image.new("RGB", (320, 200)).save(path)
            for streaming in (False, True):
                with self.subTest(streaming=streaming), LocalAPI(status=530, content={"origin": "private-origin"}) as api:
                    config = configuration(root)
                    config["api"].update(base_url=api.url, stream=streaming)
                    engine = SethEngine(config, execute=True)
                    frame = Frame(path, GuestDisplay(**TEST_DISPLAY), time.monotonic())
                    with patch.object(engine.capture, "capture_frame", return_value=frame), \
                            patch.object(engine.clicker, "click") as click:
                        result = engine.run_cycle()
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error_code"], "gateway_unavailable")
                    self.assertIn("INFERENCE_BASE_URL", result["error"])
                    self.assertNotIn("private-origin", result["error"])
                    self.assertEqual(engine.progress, ("error", "API offline"))
                    self.assertEqual(len(api.requests), 1)
                    click.assert_not_called()

    def test_streamed_decision_is_assembled_only_after_a_successful_finish(self):
        def event(data):
            return "data: " + json.dumps(data) + "\n\n"
        text = json.dumps(DECISION)
        stream = ": heartbeat\n\n" + event({"choices": [{"index": 0, "delta": {"reasoning_content": "ignored"}}]})
        stream += "".join(event({"choices": [{"index": 0, "delta": {"content": piece}}]}) for piece in (text[:40], text[40:]))
        finished = stream + event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        cases = [(finished, True), (finished + "data: [DONE]\n\n", True),
                 (stream, False), (stream + "data: [DONE]\n\n", False),
                 (stream + event({"choices": [{"delta": {}, "finish_reason": "length"}]}), False),
                 (stream + event({"error": {"message": "upstream failed"}}), False),
                 (stream + "data: broken JSON\n\n", False)]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.png"
            Image.new("RGB", (320, 200)).save(path)
            for events, valid in cases:
                with self.subTest(valid=valid, ending=events[-60:]), LocalAPI(stream_events=events) as api:
                    config = configuration(root)
                    config["api"].update(base_url=api.url, stream=True)
                    client = SethAIClient(config)
                    if valid:
                        self.assertEqual(client.parse_coordinates(client.analyze_screenshot(path), 320, 200), DECISION)
                    else:
                        with self.assertRaises(AIError):
                            client.analyze_screenshot(path)
                    self.assertEqual(len(api.requests), 1)
                    self.assertIs(api.requests[0][2]["stream"], True)

    def test_stream_deadline_interruptions_and_oversized_content_are_rejected(self):
        response = Mock()
        response.iter_lines.return_value = iter([": heartbeat", ""])
        with self.assertRaises(AIError) as caught:
            SethAIClient._read_stream(response, time.monotonic() - 2, 1)
        self.assertEqual(caught.exception.code, "response_timeout")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.png"
            Image.new("RGB", (320, 200)).save(path)
            config = configuration(root)
            config["api"]["stream"] = True
            response.status_code = 200
            response.headers = {"content-type": "text/event-stream"}
            response.iter_lines.side_effect = requests.ConnectionError("secret")
            with patch("ai_client.requests.post", return_value=response):
                with self.assertRaises(AIError) as caught:
                    SethAIClient(config).analyze_screenshot(path)
            self.assertEqual(caught.exception.code, "stream_error")
            self.assertNotIn("secret", str(caught.exception))
            response.close.assert_called_once()
        response = Mock()
        response.iter_lines.return_value = iter(["data: " + json.dumps({"choices": [{"delta": {"content": "x" * 65537}}]}), ""])
        with self.assertRaisesRegex(AIError, "size limit"):
            SethAIClient._read_stream(response, time.monotonic(), 10)

    def test_transport_failures_are_identified_without_echoing_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.png"
            Image.new("RGB", (320, 200)).save(path)
            for error, message in ((requests.Timeout("secret"), "response timed out"),
                                   (requests.exceptions.ConnectTimeout("secret"), "connection timed out"),
                                   (requests.exceptions.SSLError("secret"), "TLS"),
                                   (requests.ConnectionError("secret"), "Cannot connect"),
                                   (requests.RequestException("secret"), "request failed")):
                with self.subTest(error=type(error).__name__), patch("ai_client.requests.post", side_effect=error):
                    with self.assertRaisesRegex(AIError, message) as caught:
                        SethAIClient(configuration(root)).analyze_screenshot(path)
                    self.assertNotIn("secret", str(caught.exception))

    def test_timeout_is_identified_in_cycle_status(self):
        engine = SethEngine(configuration("/tmp"), execute=True)
        frame = Frame(Path("dummy.png"), GuestDisplay(**TEST_DISPLAY), time.monotonic())
        with patch.object(engine.capture, "capture_frame", return_value=frame), \
                patch.object(engine.ai, "analyze_screenshot", side_effect=AIError("Timed out", "response_timeout")), \
                patch.object(engine.clicker, "click") as click:
            result = engine.run_cycle()
        self.assertEqual(result["error_code"], "response_timeout")
        self.assertEqual(engine.progress, ("error", "API timeout"))
        click.assert_not_called()

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

    def test_virtual_mouse_observes_press_before_release(self):
        manager = FakeManager()
        manager.mouse.min_hold_seconds = 0.02
        result = sdk_operation(self.request(), lambda *args: manager)
        self.assertEqual(result["click_x"], 150)
        self.assertEqual([event[-1] for event in manager.mouse.events], [0, 1, 0])
        self.assertTrue(manager.unlocked)

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

    def test_plan_guard_allows_changes_elsewhere_but_rejects_changed_target(self):
        with tempfile.TemporaryDirectory() as root:
            config = configuration(root)
            display = GuestDisplay(**TEST_DISPLAY)
            original = Path(root) / "original.png"
            Image.new("RGB", (320, 200), "white").save(original)
            frame = Frame(original, display, time.monotonic())
            backend = Mock()
            updated = Image.new("RGB", (320, 200), "white")
            updated.putpixel((5, 5), (0, 0, 0))
            def capture(path):
                updated.save(path)
                return display, time.monotonic()
            backend.capture.side_effect = capture
            capture = SethCapture(config, backend)
            capture.verify_target(frame, 150, 100)
            updated.putpixel((150, 100), (0, 0, 0))
            with self.assertRaisesRegex(VMError, "control changed"):
                capture.verify_target(frame, 150, 100)

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
    def test_table_and_checkbox_plans_use_one_inference_and_the_required_presses(self):
        for plan in (CLASSIFICATION, MULTIPLE_ANSWER):
            with self.subTest(kind=plan["selection_type"]), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                (root / "vboxapi.py").write_text("from fake_vbox import FakeManager as VirtualBoxManager\n")
                log = root / "sdk.jsonl"
                env = {"PYTHONPATH": os.pathsep.join((str(root), str(Path(__file__).parent.resolve()))),
                       "SETH_TEST_VBOX_LOG": str(log)}
                normalized = plan | {"coordinate_space": "normalized_1000", "selections": [
                    point | {"click_x": round(point["click_x"] * 1000 / 320),
                             "click_y": round(point["click_y"] * 1000 / 200)} for point in plan["selections"]]}
                with patch.dict(os.environ, env), LocalAPI(decision=normalized) as api:
                    config = configuration(root)
                    config["api"]["base_url"] = api.url
                    engine = SethEngine(config, execute=True)
                    result = engine.run_cycle()
                    self.assertEqual(result["status"], "clicked", result)
                    self.assertEqual(result["click_count"], len(plan["selections"]))
                    self.assertEqual(engine.cycle_count, 1)
                    self.assertEqual(len(api.requests), 1)
                    events = [json.loads(line) for line in log.read_text().splitlines()]
                    presses = [event["args"] for event in events if event["kind"] == "mouse" and event["args"][-1] == 1]
                    self.assertEqual(presses, [[p["click_x"] + 1, p["click_y"] + 1, 0, 0, 1] for p in plan["selections"]])

    def test_helper_capture_http_and_one_click_with_fake_sdk(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "vboxapi.py").write_text("from fake_vbox import FakeManager as VirtualBoxManager\n")
            log = root / "sdk.jsonl"
            env = {"PYTHONPATH": os.pathsep.join((str(root), str(Path(__file__).parent.resolve()))),
                   "SETH_TEST_VBOX_LOG": str(log)}
            normalized = DECISION | {"coordinate_space": "normalized_1000", "click_x": 469, "click_y": 500}
            with patch.dict(os.environ, env), LocalAPI(decision=normalized) as api:
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
