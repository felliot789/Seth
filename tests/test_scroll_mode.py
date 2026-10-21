"""Scroll one synthetic whole-page question; never access a live VM/account."""

from dataclasses import replace, asdict
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from capture import SethCapture
from inference import build_prompt
from main import SethEngine
from scroll_capture import ScrollCapture, body, displacement
from tests.fake_vbox import FakeManager
from vbox_backend import GuestDisplay, VMError, sdk_operation


class FastEvent(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


class PageVM:
    """Only framebuffer capture and scrolling; answer state is immutable."""
    def __init__(self, offset=240, step=60):
        self.offset, self.step = offset, step
        self.events, self.positions = [], {}
        self.display = GuestDisplay("page", 0, 640, 480, 0, 0, 1, 42, 1)
        self.page = Image.new("RGB", (640, 1080), "white")
        draw = ImageDraw.Draw(self.page)
        for n in range(36):
            draw.text((40, n * 30 + 4), f"Question detail {n + 1}: option {(n * 1729) % 12347}", fill="black")
            draw.rectangle((350, n * 30 + 5, 350 + 3 + n * 5, n * 30 + 10), fill=(20 + n * 3, 80, 120))
        self.limit = self.page.height - 360
        self.after_scroll = None

    def capture(self, path):
        image = Image.new("RGB", (640, 480), "#cccccc")
        image.paste(self.page.crop((0, self.offset, 640, self.offset + 360)), (0, 80))
        draw = ImageDraw.Draw(image)
        draw.rectangle((500, 100, 639, 400), fill="#eeeeee")
        draw.text((510, 140), "Fixed navigation", fill="black")
        draw.text((15, 80), "Sticky heading", fill="red")
        draw.text((500, 10), f"Timer {len(self.positions)}", fill="black")
        image.save(path)
        self.positions[Path(path)] = self.offset
        return self.display, time.monotonic()

    def scroll(self, frame, ticks, stop_event):
        assert not stop_event.is_set()
        self.events.append(ticks)
        self.offset = max(0, min(self.limit, self.offset + ticks * self.step))
        if self.after_scroll:
            self.after_scroll()

    def close(self):
        pass


class ScrollModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = {"project_dir": self.temporary.name, "capture": {"mode": "scroll", "max_frames": 16}}
        self.stop = FastEvent()

    def capture(self, vm):
        return ScrollCapture(SethCapture(self.config, vm), self.config, self.stop)

    def test_top_to_bottom_preserves_order_overlap_and_original_pixels(self):
        vm = PageVM()
        frames = self.capture(vm).run()
        positions = [vm.positions[frame.path] for frame in frames]
        self.assertEqual(positions[0], 0)
        self.assertEqual(positions[-1], vm.limit)
        self.assertEqual(positions, sorted(set(positions)))
        self.assertGreater(len(frames), 1)
        for before, after in zip(frames, frames[1:]):
            self.assertEqual(displacement(body(before.path), body(after.path), 1),
                             vm.positions[after.path] - vm.positions[before.path])
        self.assertEqual(set(Path(self.temporary.name, "captures").iterdir()), {frame.path for frame in frames})
        self.assertTrue(all(frame.path.stat().st_mode & 0o777 == 0o600 for frame in frames))
        self.assertTrue(all(type(tick) is int and 1 <= abs(tick) <= 4 for tick in vm.events))

    def test_all_providers_and_formats_get_one_complete_batch_and_only_keys(self):
        for provider in ("api", "qwen", "gemini"):
            for format_, answer in (("mc", "2"), ("mcma", "1, 3"), ("table", "L, R, R, L")):
                with self.subTest(provider=provider, format=format_):
                    vm = PageVM()
                    with patch("main.VirtualBoxBackend", return_value=vm):
                        engine = SethEngine(self.config, provider=provider, question_type=format_)
                    engine.cancel_event.wait = lambda _: engine.cancel_event.is_set()
                    client = Mock()
                    client.analyze_screenshots.return_value = json.dumps(
                        {"status": "answer", "confidence": .99, "answer": answer})
                    with patch("main.create_client", return_value=client):
                        result = engine.run_cycle()
                    self.assertEqual(result["status"], "answer", result)
                    self.assertEqual(engine.progress, ("done", answer))
                    client.analyze_screenshot.assert_not_called()
                    client.analyze_screenshots.assert_called_once()
                    paths, actual_format = client.analyze_screenshots.call_args.args
                    self.assertEqual(actual_format, format_)
                    self.assertEqual(vm.positions[paths[0]], 0)
                    self.assertEqual(vm.positions[paths[-1]], vm.limit)
                    self.assertEqual(result["capture_paths"], [str(path) for path in paths])

    def test_cancel_resize_missing_overlap_and_image_limit_prevent_inference(self):
        for scenario in ("cancel", "resize", "gap", "limit"):
            with self.subTest(scenario=scenario):
                vm = PageVM(offset=0, step=400 if scenario == "gap" else 60)
                config = self.config | {"capture": {"mode": "scroll", "max_frames": 2 if scenario == "limit" else 16}}
                with patch("main.VirtualBoxBackend", return_value=vm):
                    engine = SethEngine(config)
                engine.cancel_event.wait = lambda _: engine.cancel_event.is_set()
                if scenario == "cancel":
                    vm.after_scroll = engine.interrupt_cycle
                elif scenario == "resize":
                    vm.after_scroll = lambda: setattr(vm, "display", replace(vm.display, session_pid=43))
                client = Mock()
                with patch("main.create_client", return_value=client):
                    result = engine.run_cycle()
                self.assertEqual(result["status"], "cancelled" if scenario == "cancel" else "error", result)
                client.analyze_screenshots.assert_not_called()
                client.analyze_screenshot.assert_not_called()
                if scenario in ("cancel", "resize"):
                    self.assertEqual(len(vm.events), 1)

    def test_capture_mode_switch_is_idle_only_and_sends_no_request(self):
        with patch("main.VirtualBoxBackend", return_value=PageVM()):
            engine = SethEngine({"project_dir": self.temporary.name})
        self.assertIn("Single", engine.indicator_selection)
        self.assertTrue(engine.cycle_capture_mode())
        self.assertIn("Scroll", engine.indicator_selection)
        self.assertTrue(engine._begin())
        self.assertFalse(engine.cycle_capture_mode())
        engine._cycle_lock.release()

    def test_time_limit_stops_before_the_next_scroll_and_keeps_no_partial_batch(self):
        vm = PageVM()
        collector = self.capture(vm)
        vm.after_scroll = lambda: setattr(collector, "deadline", 0)
        with self.assertRaisesRegex(VMError, "45 seconds"):
            collector.run()
        self.assertEqual(len(vm.events), 1)
        self.assertEqual(list(Path(self.temporary.name, "captures").iterdir()), [])

    def test_prompt_combines_sections_without_restarting_rows_or_options(self):
        prompt = build_prompt("table", 3)
        self.assertIn("top-to-bottom", prompt)
        self.assertIn("count each option/table row ONCE", prompt)
        self.assertIn("do not restart per image", prompt)


class ScrollSDKTests(unittest.TestCase):
    def test_scroll_has_fixed_coordinates_and_never_presses_a_button(self):
        for ticks in (-1, 1, 4):
            manager = FakeManager()
            mouse = SimpleNamespace(absoluteSupported=True, putMouseEventAbsolute=Mock())
            manager.console.mouse = mouse
            display = GuestDisplay("fake-vm-uuid", 0, 320, 200, 0, 0, 12345, 42, 1)
            request = {"operation": "scroll", "target": "test", "screen": 0, "expected": asdict(display),
                       "captured_at": time.monotonic(), "ticks": ticks, "x": 20, "buttons": 1}
            sdk_operation(request, lambda *_: manager)
            mouse.putMouseEventAbsolute.assert_called_once_with(296, 101, ticks, 0, 0)
            self.assertTrue(manager.unlocked and manager.closed)

    def test_bad_ticks_stale_capture_or_changed_vm_send_no_scroll(self):
        for scenario in ("bool", "zero", "large", "stale", "changed", "relative"):
            manager = FakeManager()
            manager.console.mouse = SimpleNamespace(absoluteSupported=scenario != "relative", putMouseEventAbsolute=Mock())
            display = GuestDisplay("fake-vm-uuid", 0, 320, 200, 0, 0, 12345, 42, 1)
            request = {"operation": "scroll", "target": "test", "screen": 0, "expected": asdict(display),
                       "captured_at": time.monotonic(), "ticks": 1}
            if scenario in ("bool", "zero", "large"):
                request["ticks"] = {"bool": True, "zero": 0, "large": 5}[scenario]
            elif scenario == "stale": request["captured_at"] -= 4
            elif scenario == "changed": request["expected"]["session_pid"] = 99
            with self.subTest(scenario=scenario), self.assertRaises(VMError):
                sdk_operation(request, lambda *_: manager)
            manager.console.mouse.putMouseEventAbsolute.assert_not_called()
