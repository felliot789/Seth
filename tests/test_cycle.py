"""One screenshot, one provider request and display-only answer keys."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from main import SethEngine
from vbox_backend import GuestDisplay


class ReadOnlyVM:
    """Deliberately has no mouse, wheel, click, key or navigation methods."""
    def __init__(self):
        self.frames = 0
        self.closed = 0

    def capture(self, path):
        self.frames += 1
        Image.new("RGB", (320, 200), "white").save(path)
        return GuestDisplay("test", 0, 320, 200, 0, 0, 1, 42, 1), time.monotonic()

    def close(self):
        self.closed += 1


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)

    def engine(self, mode="mc", provider="api"):
        vm = ReadOnlyVM()
        with patch("main.VirtualBoxBackend", return_value=vm):
            engine = SethEngine({"project_dir": self.root.name}, provider=provider, question_type=mode)
        return engine, vm

    def client(self, answer="2", status="answer", confidence=.99, mutation=None):
        def infer(path, question_type):
            self.assertTrue(Path(path).is_file())
            if mutation:
                mutation()
            return json.dumps({"status": status, "confidence": confidence, "answer": answer})
        return Mock(analyze_screenshot=Mock(side_effect=infer))

    def test_all_providers_and_formats_capture_once_and_publish_only_keys(self):
        for provider in ("api", "qwen", "gemini"):
            for mode, answer in (("mc", "2"), ("mcma", "1, 3, 5"), ("table", "L, R, L, R, R")):
                with self.subTest(provider=provider, mode=mode):
                    engine, vm = self.engine(mode, provider)
                    client = self.client(answer)
                    with patch("main.create_client", return_value=client) as factory:
                        result = engine.run_cycle()
                    self.assertEqual(result["status"], "answer", result)
                    self.assertEqual(engine.progress, ("done", answer))
                    self.assertEqual(vm.frames, 1)
                    self.assertEqual(vm.closed, 1)
                    self.assertEqual(factory.call_args.args[1], provider)
                    client.analyze_screenshot.assert_called_once()
                    self.assertFalse(any("click" in key for key in result))

    def test_uncertainty_low_confidence_and_no_action_publish_no_answer_and_never_fallback(self):
        for status, confidence in (("uncertain", .3), ("answer", .5), ("no_action", 0)):
            engine, vm = self.engine()
            client = self.client("2" if status == "answer" else "", status, confidence)
            with patch("main.create_client", return_value=client) as factory:
                result = engine.run_cycle()
            self.assertEqual(result["status"], "no_action" if status == "no_action" else "uncertain")
            self.assertEqual(result["decision"]["answer"], "")
            self.assertEqual(engine.selected_provider, "api")
            self.assertEqual(vm.frames, 1)
            factory.assert_called_once()

    def test_provider_error_is_visible_and_does_not_retry(self):
        engine, vm = self.engine()
        client = self.client()
        client.analyze_screenshot.side_effect = ValueError("Provider returned invalid JSON")
        with patch("main.create_client", return_value=client):
            result = engine.run_cycle()
        self.assertEqual(result["status"], "error")
        self.assertIn("Provider returned invalid JSON", engine.progress[1])
        client.analyze_screenshot.assert_called_once()

    def test_cancel_discards_late_answer_and_rearms_with_manual_choices(self):
        engine, vm = self.engine("table", "qwen")
        client = self.client("L, R", mutation=engine.interrupt_cycle)
        with patch("main.create_client", return_value=client):
            self.assertEqual(engine.run_cycle()["status"], "cancelled")
        self.assertEqual(engine.progress, ("idle", "Idle"))
        self.assertEqual((engine.selected_provider, engine.question_type), ("qwen", "table"))
        with patch("main.create_client", return_value=self.client("R, L")):
            self.assertEqual(engine.run_cycle()["status"], "answer")
        self.assertEqual(engine.progress, ("done", "R, L"))

    def test_busy_choices_are_discarded_and_idle_changes_clear_old_keys(self):
        engine, vm = self.engine()
        self.assertTrue(engine._begin())
        try:
            self.assertFalse(engine.cycle_provider())
            self.assertFalse(engine.cycle_question_type())
            self.assertFalse(engine.request_cycle())
        finally:
            engine._cycle_active = False
            engine._cycle_lock.release()
        with patch("main.create_client") as factory:
            for provider in ("qwen", "gemini", "api"):
                engine.progress = ("done", "2")
                self.assertTrue(engine.cycle_provider())
                self.assertEqual(engine.selected_provider, provider)
                self.assertEqual(engine.progress, ("idle", "Ready"))
            for mode in ("mcma", "table", "mc"):
                self.assertTrue(engine.cycle_question_type())
                self.assertEqual(engine.question_type, mode)
            factory.assert_not_called()

    def test_removed_execute_flag_cannot_enable_old_answer_operations(self):
        result = subprocess.run([sys.executable, "main.py", "--execute"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --execute", result.stderr)
