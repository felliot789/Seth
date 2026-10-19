"""Scroll context and SDK input regressions; generated images, no real VM."""

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from capture import Frame
from scroll_capture import ScrollCollector, ScrollLimitError, find_scrollbar, pixel_box
from tests.fake_vbox import FakeManager, TEST_DISPLAY
from vbox_backend import GuestDisplay, VMError, sdk_operation


def screen(position=1, *, scrollbar=True, arrows=True, header='Question 1', shade=139, x=666, thumb_height=100):
    image = Image.new('RGB', (720, 540), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((45, 95), header, fill='black')
    draw.text((370, 220), f'Question section at {position:.2f}', fill='black')
    draw.ellipse((610, 290, 631, 311), outline='#aaa', width=2)
    if scrollbar:
        color = (shade,) * 3
        y = round(183 + position * (253 - thumb_height))
        draw.rounded_rectangle((x, y, x + 12, y + thumb_height - 1), radius=5, fill=color)
        if arrows:
            draw.polygon(((x + 6, 165), (x, 175), (x + 12, 175)), fill=color)
            draw.polygon(((x, 445), (x + 12, 445), (x + 6, 455)), fill=color)
    return image


class Viewport:
    def __init__(self, root):
        self.root, self.position, self.counter = root, 1, 0
        self.backend = self
        self.timestamp = time.monotonic()
        self.display = GuestDisplay(**(TEST_DISPLAY | {'width': 720, 'height': 540}))
        self.drags, self.after_drag = [], None
        self.parks = []
        self.header, self.no_scroll, self.stuck, self.unstable = 'Question 1', False, False, False
        self.selected = False
        self.thumb_height = 100

    def capture_frame(self):
        self.counter += 1
        path = self.root / f'frame-{self.counter}.png'
        image = screen(self.position, scrollbar=not self.no_scroll, header=self.header, thumb_height=self.thumb_height)
        if self.selected:
            ImageDraw.Draw(image).ellipse((617, 297, 624, 304), fill='#0877c9')
        if self.unstable:
            ImageDraw.Draw(image).text((400, 250), str(self.counter), fill='black')
        image.save(path)
        return Frame(path, self.display, self.timestamp)

    def drag(self, frame, x, y, end_x, end_y, max_age, stop_event):
        self.drags.append((x, y, end_x, end_y))
        with Image.open(frame.path) as image:
            bar = find_scrollbar(image, (360, 151, 684, 464))
        if not self.stuck:
            self.position = max(0, min(1, (end_y - bar.top - bar.height / 2) / (bar.bottom - bar.top - bar.height)))
        if self.after_drag:
            self.after_drag()

    def park_pointer(self, frame, max_age, stop_event):
        self.parks.append(frame)


class ScrollCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.viewport = Viewport(Path(self.temporary.name))
        self.config = {'capture': {'max_age_seconds': 90, 'scroll_context': {
            'enabled': True, 'regions': [[.5, .28, .95, .86]], 'max_views': 8}}}
        self.stop = threading.Event()
        self.collector = ScrollCollector(self.config, self.viewport, self.stop)

    def test_native_thumb_and_both_arrows_are_required_in_the_configured_column(self):
        region = (360, 151, 684, 464)
        for shade in (95, 139, 209):
            bar = find_scrollbar(screen(shade=shade), region)
            self.assertIsNotNone(bar)
            self.assertEqual(bar.height, 100)
            self.assertLess(bar.destination(0)[1], bar.destination(1)[1])
        for image in (screen(scrollbar=False), screen(arrows=False), screen(x=400)):
            self.assertIsNone(find_scrollbar(image, region))

    def test_two_matching_scrollbars_are_ambiguous(self):
        image = screen(x=640)
        extra = screen(x=666)
        image.paste(extra.crop((660, 150, 684, 464)), (660, 150))
        with self.assertRaises(VMError):
            find_scrollbar(image, (360, 151, 684, 464))

    def test_collects_overlapping_reference_views_and_finishes_on_live_bottom(self):
        initial = self.viewport.capture_frame()
        question = self.collector.collect(initial)
        self.assertEqual(len(self.viewport.drags), 4)
        self.assertEqual(len(question.contexts), 3)
        self.assertEqual(self.viewport.position, 1)
        self.assertEqual(len(self.viewport.parks), 1)
        self.assertNotEqual(question.frame.path, initial.path)
        self.assertTrue(all(context['path'].exists() for context in question.contexts))
        self.assertTrue(all(box[3] < 470 for box in question.regions))
        question.verify_identity(self.viewport, self.stop)

    def test_disabled_or_non_scrolling_screen_sends_no_input(self):
        for disabled in (True, False):
            with self.subTest(disabled=disabled):
                self.config['capture']['scroll_context']['enabled'] = not disabled
                self.viewport.no_scroll = True
                initial = self.viewport.capture_frame()
                question = self.collector.collect(initial)
                self.assertIs(question.frame, initial)
                self.assertEqual(question.contexts, [])
                self.assertEqual(self.viewport.drags, [])
                self.assertEqual(self.viewport.parks, [])

    def test_view_limit_is_checked_before_any_scroll(self):
        self.config['capture']['scroll_context']['max_views'] = 2
        with self.assertRaisesRegex(ScrollLimitError, 'requires 4 scroll views.*limit of 2') as failure:
            self.collector.collect(self.viewport.capture_frame())
        self.assertEqual((failure.exception.required_views, failure.exception.max_views), (4, 2))
        self.assertEqual(self.viewport.drags, [])

    def test_long_graph_question_can_collect_nine_views_with_the_expanded_limit(self):
        self.viewport.thumb_height = 48
        initial = self.viewport.capture_frame()
        with self.assertRaisesRegex(ScrollLimitError, 'requires 9 scroll views.*limit of 8'):
            self.collector.collect(initial)
        self.assertEqual(self.viewport.drags, [])
        self.config['capture']['scroll_context']['max_views'] = 12
        question = self.collector.collect(initial)
        self.assertEqual(len(self.viewport.drags), 9)
        self.assertEqual(len(question.contexts), 8)
        self.assertEqual(self.viewport.position, 1)
        question.verify_identity(self.viewport, self.stop)

    def test_header_change_during_collection_stops_before_the_next_drag(self):
        self.viewport.after_drag = lambda: setattr(self.viewport, 'header', 'Question 2')
        with self.assertRaisesRegex(VMError, 'header changed'):
            self.collector.collect(self.viewport.capture_frame())
        self.assertEqual(len(self.viewport.drags), 1)

    def test_cancellation_before_or_during_collection_stops_input(self):
        self.stop.set()
        with self.assertRaisesRegex(VMError, 'cancelled'):
            self.collector.collect(self.viewport.capture_frame())
        self.assertEqual(self.viewport.drags, [])
        self.stop.clear()
        self.viewport.after_drag = self.stop.set
        with self.assertRaisesRegex(VMError, 'cancelled'):
            self.collector.collect(self.viewport.capture_frame())
        self.assertEqual(len(self.viewport.drags), 1)

    def test_stuck_scrollbar_fails_instead_of_supplying_incomplete_context(self):
        self.viewport.stuck = True
        with self.assertRaisesRegex(VMError, 'endpoint'):
            self.collector.collect(self.viewport.capture_frame())
        self.assertEqual(len(self.viewport.drags), 1)

    def test_unsettled_question_times_out_without_answer_input(self):
        initial = self.viewport.capture_frame()
        self.viewport.unstable = True
        with patch('scroll_capture.time.monotonic', side_effect=[0, 4]):
            with self.assertRaisesRegex(VMError, 'did not settle'):
                self.collector.settled(initial, (360, 151, 684, 464), (36, 81, 252, 151))

    def test_question_or_vm_change_after_inference_rejects_answer_input(self):
        question = self.collector.collect(self.viewport.capture_frame())
        self.viewport.header = 'Question 2'
        with self.assertRaisesRegex(VMError, 'header changed'):
            question.verify_identity(self.viewport)
        self.viewport.header = 'Question 1'
        self.viewport.display = replace(self.viewport.display, session_pid=999)
        with self.assertRaisesRegex(VMError, 'display changed'):
            question.verify_identity(self.viewport)

    def test_scroll_change_without_header_change_rejects_answer_input(self):
        question = self.collector.collect(self.viewport.capture_frame())
        self.viewport.position = .5
        with self.assertRaisesRegex(VMError, 'view changed'):
            question.verify_identity(self.viewport)

    def test_only_prior_verified_control_changes_are_ignored_between_table_rows(self):
        question = self.collector.collect(self.viewport.capture_frame())
        self.viewport.selected = True
        with self.assertRaisesRegex(VMError, 'view changed'):
            question.verify_identity(self.viewport)
        question.selected_controls.append({'target_control': {'box': [610, 290, 632, 312]}})
        question.verify_identity(self.viewport)
        self.viewport.position = .5
        with self.assertRaisesRegex(VMError, 'view changed'):
            question.verify_identity(self.viewport)

    def test_invalid_regions_are_not_accepted(self):
        for value in ([0, 0, 1, True], [0, 0, 2, 1], [0, 1, 1, 0], [0, 0, float('nan'), 1], [0, 0, 1], 'bad'):
            with self.subTest(region=value), self.assertRaises(VMError):
                pixel_box(value, (720, 540))


class ScrollSDKTests(unittest.TestCase):
    def request(self, **changes):
        return {'operation': 'drag', 'target': 'fixture', 'screen': 0, 'expected': TEST_DISPLAY,
                'captured_at': time.monotonic(), 'max_age': 90, 'x': 300, 'y': 120,
                'end_x': 300, 'end_y': 40, **changes}

    def test_pointer_move_is_button_free_and_validates_age_and_vm(self):
        manager = FakeManager()
        result = sdk_operation(self.request(operation='move', x=8, y=100), lambda *args: manager)
        self.assertEqual(manager.mouse.events, [(9, 101, 0, 0, 0)])
        self.assertEqual((result['pointer_x'], result['pointer_y']), (8, 100))
        self.assertTrue(manager.closed and manager.unlocked)
        for changes in ({'captured_at': time.monotonic()-100}, {'expected': TEST_DISPLAY | {'session_pid':99}}, {'x':320}):
            manager = FakeManager()
            with self.subTest(changes=changes), self.assertRaises(VMError):
                sdk_operation(self.request(operation='move', **changes), lambda *args: manager)
            self.assertEqual(manager.mouse.events, [])

    def test_wheel_ticks_are_button_free_and_keep_both_directions_and_display_origin(self):
        for ticks in (-3, 2):
            manager = FakeManager()
            manager.console.display.origin = (100, -200)
            with patch('vbox_backend.time.sleep'):
                result = sdk_operation(self.request(operation='wheel', ticks=ticks,
                                      expected=TEST_DISPLAY | {'x_origin':100,'y_origin':-200}), lambda *args: manager)
            self.assertEqual(result['wheel_ticks'], ticks)
            self.assertEqual(manager.mouse.events[0], (401, -79, 0, 0, 0))
            self.assertEqual(manager.mouse.events[1:], [(401, -79, 1 if ticks > 0 else -1, 0, 0)] * abs(ticks))
            self.assertTrue(manager.closed and manager.unlocked)

    def test_invalid_stale_or_cancelled_wheel_input_sends_no_ticks(self):
        for changes in ({'ticks': 0}, {'ticks': True}, {'ticks': 33}, {'ticks': 1.5},
                        {'captured_at': time.monotonic()-100}, {'expected': TEST_DISPLAY | {'session_pid':99}}):
            manager = FakeManager()
            with self.subTest(changes=changes), self.assertRaises(VMError):
                sdk_operation(self.request(operation='wheel', ticks=1) | changes, lambda *args: manager)
            self.assertFalse(manager.mouse.events)

    def test_cancellation_between_wheel_ticks_stops_without_retry_or_press(self):
        manager = FakeManager()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'stop'
            original = manager.mouse.putMouseEventAbsolute
            def put(*args):
                original(*args)
                if args[2]:
                    marker.touch()
            manager.mouse.putMouseEventAbsolute = put
            with patch('vbox_backend.time.sleep'), self.assertRaisesRegex(VMError, 'cancelled'):
                sdk_operation(self.request(operation='wheel', ticks=3, cancel_path=str(marker)), lambda *args: manager)
        self.assertEqual(manager.mouse.events, [(301, 121, 0, 0, 0), (301, 121, 1, 0, 0)])

    def test_single_press_moves_in_bounded_steps_and_always_releases(self):
        manager = FakeManager()
        with patch('vbox_backend.time.sleep'):
            result = sdk_operation(self.request(), lambda *args: manager)
        self.assertEqual(result['end'], [300, 40])
        events = manager.mouse.events
        self.assertEqual(events[0], (301, 121, 0, 0, 0))
        self.assertTrue(all(event[-1] == 1 for event in events[1:-1]))
        self.assertEqual(events[-1], (301, 41, 0, 0, 0))
        self.assertTrue(manager.closed and manager.unlocked)

    def test_failure_after_press_releases_without_retrying(self):
        manager = FakeManager()
        manager.mouse.failure = 'press'
        with self.assertRaisesRegex(VMError, 'unknown'):
            sdk_operation(self.request(), lambda *args: manager)
        self.assertEqual([event[-1] for event in manager.mouse.events], [0, 1, 0])
        self.assertTrue(manager.closed and manager.unlocked)

    def test_cancellation_mid_drag_releases_at_the_last_point(self):
        manager = FakeManager()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'stop'
            original = manager.mouse.putMouseEventAbsolute
            def put(*args):
                original(*args)
                if len(manager.mouse.events) == 4:
                    marker.touch()
            manager.mouse.putMouseEventAbsolute = put
            with patch('vbox_backend.time.sleep'), self.assertRaisesRegex(VMError, 'cancelled'):
                sdk_operation(self.request(cancel_path=str(marker)), lambda *args: manager)
        self.assertEqual(len(manager.mouse.events), 5)
        self.assertEqual(manager.mouse.events[-1][-1], 0)
        self.assertEqual(manager.mouse.events[-1][:2], manager.mouse.events[-2][:2])

    def test_invalid_or_stale_drag_never_sends_mouse_input(self):
        for changes in ({'end_y': -1}, {'end_x': 320}, {'end_y': True}, {'captured_at': time.monotonic() - 100}):
            manager = FakeManager()
            with self.subTest(changes=changes), self.assertRaises(VMError):
                sdk_operation(self.request(**changes), lambda *args: manager)
            self.assertEqual(manager.mouse.events, [])
