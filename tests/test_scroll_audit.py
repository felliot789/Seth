"""Offline regressions for complete scroll verification and inference freshness."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from scroll_capture import ScrollCollector, ScrollNavigationError
from tests.test_wheel_scroll import WheelPageVM
from tests.test_scroll_layout import PageVM
from vbox_backend import VMError, validate_age


class ScrollAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vm = WheelPageVM(Path(temporary.name))
        self.addCleanup(patch.stopall)
        patch('scroll_capture.time.sleep').start()

    def navigator(self, stop=None):
        vm = self.vm
        question = ScrollCollector(vm.config, vm).collect(
            vm.capture_frame(), layout_client=Mock(inspect_layout=Mock(return_value=json.dumps(vm.layout))))
        return question.navigate(vm.config, vm, stop)

    def test_changed_middle_section_is_rejected_before_first_selection(self):
        nav = self.navigator()
        self.vm.changed = True
        with self.assertRaisesRegex(ScrollNavigationError, 'does not match'):
            nav.prepare({'target_id': '01'})
        self.assertFalse(nav.identity_verified)
        self.assertFalse(self.vm.clicks)

    def test_changed_middle_native_section_is_rejected_before_first_selection(self):
        self.vm = PageVM(self.vm.root)
        nav = self.navigator()
        self.vm.changed = True
        with self.assertRaisesRegex(ScrollNavigationError, 'does not match'):
            nav.prepare({'target_id': '01'})
        self.assertFalse(nav.identity_verified)
        self.assertFalse(self.vm.clicks)

    def test_all_sections_verified_with_one_forward_traversal(self):
        nav = self.navigator()
        wheel = nav.question.wheel_navigation
        with patch.object(wheel, 'top', wraps=wheel.top) as top, \
                patch.object(nav, '_verify_question_section', wraps=nav._verify_question_section) as verify:
            nav.prepare({'target_id': '06'})
        self.assertEqual([call.args[0].key for call in verify.call_args_list], list(nav.views))
        self.assertEqual(top.call_count, 1)
        self.assertTrue(nav.identity_verified)

    def test_wheel_pixel_nudge_tracks_shift_and_changed_foreground_is_rejected(self):
        nav = self.navigator()
        self.vm.position -= 1 / 868
        nav.verify_current()
        self.assertEqual(nav.shifts[1], 1)
        self.vm.question_label = 'Changed heading'
        # This changed option is visible at the current bottom position.
        self.vm.selected.add(5)
        with self.assertRaises(ScrollNavigationError):
            nav.verify_current()

    def test_oldest_section_expiry_stops_before_navigation(self):
        nav = self.navigator()
        first = next(iter(nav.views.values()))
        first.frame = replace(first.frame, captured_at=time.monotonic() - 1000)
        wheels = list(self.vm.wheels)
        with self.assertRaisesRegex(VMError, 'old|age|stale'):
            nav.prepare({'target_id': '01'})
        self.assertEqual(self.vm.wheels, wheels)
        self.assertFalse(self.vm.clicks)

    def test_return_frame_preserves_original_deadline_for_clicker(self):
        nav = self.navigator()
        earliest = nav.question.inference_captured_at
        frame, _ = nav.prepare({'target_id': '01'})
        self.assertEqual(frame.captured_at, earliest)
        self.assertGreater(nav.current.captured_at, earliest)
        with patch('vbox_backend.time.monotonic', return_value=earliest + 90.001):
            with self.assertRaises(VMError):
                validate_age(frame.captured_at, 90)

    def test_expiry_during_return_stops_subsequent_wheel_input(self):
        nav = self.navigator()
        wheels = len(self.vm.wheels)
        def expire():
            first = next(iter(nav.views.values()))
            first.frame = replace(first.frame, captured_at=time.monotonic() - 1000)
        self.vm.after_wheel = expire
        with self.assertRaises(VMError):
            nav.prepare({'target_id': '01'})
        self.assertEqual(len(self.vm.wheels), wheels + 1)
        self.assertFalse(self.vm.clicks)

    def test_cancellation_during_section_verification_stops_before_input(self):
        stop = threading.Event()
        nav = self.navigator(stop)
        original = nav._verify_question_section
        def cancel_after_section(view, fresh):
            original(view, fresh)
            stop.set()
        with patch.object(nav, '_verify_question_section', side_effect=cancel_after_section):
            with self.assertRaisesRegex(VMError, 'cancelled'):
                nav.prepare({'target_id': '01'})
        self.assertFalse(self.vm.clicks)
        self.assertFalse(nav.identity_verified)
