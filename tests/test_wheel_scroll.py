"""Whole-page wheel capture and return trips, with no native scrollbar."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from inference import DecisionClient
from main import SethEngine
from scroll_capture import ScrollCollector, ScrollLayoutError, ScrollLimitError, find_scrollbars
from scroll_layout import parse_layout
from tests.test_scroll_layout import PageVM
from vbox_backend import VMError


class WheelPageVM(PageVM):
    def __init__(self, root):
        super().__init__(root)
        self.wheels, self.after_wheel = [], None
        self.tick_pixels = 80
        self.config['capture']['scroll_context']['scroll_method'] = 'wheel'
        self.layout['regions'][0].update(scrollbar_id=None, scrollable=True)

    def capture_frame(self):
        frame = super().capture_frame()
        with Image.open(frame.path) as source:
            image = source.convert('RGB')
        ImageDraw.Draw(image).rectangle((820, 0, 839, 619), fill='white')
        image.save(frame.path)
        return frame

    def wheel(self, frame, x, y, ticks, max_age, stop_event=None):
        if stop_event is not None and stop_event.is_set():
            raise AssertionError('Wheel input after cancellation')
        self.wheels.append(ticks)
        if not self.stuck:
            self.position = max(0, min(1, self.position + ticks * self.tick_pixels / 868))
        if self.after_wheel:
            self.after_wheel()


class WheelScrollTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stop = threading.Event()

    def collect(self, vm):
        client = Mock(inspect_layout=Mock(return_value=json.dumps(vm.layout)))
        question = ScrollCollector(vm.config, vm, self.stop).collect(vm.capture_frame(), layout_client=client)
        client.inspect_layout.assert_called_once()
        return question, question.navigate(vm.config, vm, self.stop)

    def test_scrollbar_free_capture_deduplicates_and_returns_to_first_and_last_answers(self):
        vm = WheelPageVM(self.root)
        with Image.open(vm.capture_frame().path) as image:
            self.assertEqual(find_scrollbars(image), [])
        question, nav = self.collect(vm)
        self.assertGreater(len(question.views), 2)
        self.assertEqual(len(nav.controls), 6)
        self.assertTrue(any(len(record['occurrences']) > 1 for record in nav.records.values()))
        raw = {'action': 'select', 'selection_type': 'multiple_answer', 'confidence': .99,
               'explanation': 'First and last options.', 'selections': [
                   {'option': 'First', 'target_id': '01'}, {'option': 'Last', 'target_id': '06'}]}
        decision = DecisionClient().parse_coordinates(json.dumps(raw), 840, 620, control_targets=nav.controls)
        for point in nav.plan(decision):
            frame, target = nav.prepare(point)
            vm.verify_target(frame, target['click_x'], target['click_y'])
            vm.click(frame, target['click_x'], target['click_y'], 90)
            nav.verify_selection(frame, target)
            nav.confirm(target)
        self.assertEqual(vm.selected, {0, 5})
        self.assertEqual(vm.clicks, [0, 5])
        self.assertFalse(vm.drags)
        nav.verify_current()

    def test_hover_is_cleared_before_baseline_and_clamped_top_probes(self):
        class HoverPageVM(WheelPageVM):
            hovered = True

            def capture_frame(self):
                frame = super().capture_frame()
                if self.hovered:
                    with Image.open(frame.path) as source:
                        image = source.convert('RGB')
                    ImageDraw.Draw(image).rectangle((50, 180, 750, 210), fill='#e8edf5')
                    image.save(frame.path)
                return frame

            def park_pointer(self, *args):
                self.hovered = False

            def wheel(self, *args, **kwargs):
                if self.hovered:
                    raise AssertionError('Endpoint probes must start from a neutral-pointer baseline')
                return super().wheel(*args, **kwargs)

        vm = HoverPageVM(self.root)
        vm.position = 0
        question, nav = self.collect(vm)
        self.assertEqual(vm.wheels[:2], [-1, -1])
        self.assertGreater(len(question.views), 2)
        self.assertEqual(len(nav.controls), 6)
        self.assertFalse(vm.clicks)
        nav.verify_current()

    def test_question_replacement_after_discovery_stops_before_first_probe(self):
        vm = WheelPageVM(self.root)
        vm.position = 0
        capture = vm.capture_frame

        def replace_question_before_probe():
            # Initial capture, neutral baseline and discovery check have all
            # completed. The URL stays fixed while the actual question changes.
            if vm.counter == 3:
                vm.question_label = 'Replacement question on the same URL'
            return capture()

        vm.capture_frame = replace_question_before_probe
        with self.assertRaisesRegex(ScrollLayoutError, 'content changed'):
            self.collect(vm)
        self.assertFalse(vm.wheels)
        self.assertFalse(vm.clicks)

    def test_engine_uses_one_layout_and_one_answer_request_without_a_scrollbar(self):
        vm = WheelPageVM(self.root)
        engine = SethEngine(vm.config, execute=True, manual_selection=True)
        engine.capture, engine.clicker.backend = vm, vm
        engine.toggle_capture()
        raw = {'action': 'select', 'selection_type': 'multiple_answer', 'confidence': .99,
               'explanation': 'Last option.', 'selections': [{'option': 'Last', 'target_id': '06'}]}
        def infer(path, *, context_images, control_regions, scroll_targets):
            self.assertGreater(len(context_images), 1)
            return json.dumps(DecisionClient().parse_coordinates(json.dumps(raw), 840, 620,
                                                                control_targets=scroll_targets['controls']))
        with patch.object(engine.ai, 'inspect_layout', return_value=json.dumps(vm.layout)) as layout, \
                patch.object(engine.ai, 'analyze_screenshot', side_effect=infer) as answer:
            result = engine.run_cycle()
        self.assertEqual(result['status'], 'clicked', result)
        self.assertTrue(result['clicks'][0]['selection_verified'])
        self.assertEqual(vm.selected, {5})
        self.assertFalse(vm.drags)
        layout.assert_called_once()
        answer.assert_called_once()

    def test_cancelled_or_changed_page_stops_wheel_collection_without_answer_input(self):
        for failure in ('cancelled', 'header changed'):
            with self.subTest(failure=failure):
                vm = WheelPageVM(self.root)
                self.stop.clear()
                vm.after_wheel = self.stop.set if failure == 'cancelled' else lambda: setattr(vm, 'header', 'different URL')
                with self.assertRaisesRegex(VMError, failure):
                    self.collect(vm)
                self.assertEqual(len(vm.wheels), 1)
                self.assertFalse(vm.clicks)

    def test_stuck_page_and_excessive_steps_cannot_produce_an_incomplete_answer(self):
        for scenario in ('stuck', 'jump', 'limit'):
            with self.subTest(scenario=scenario):
                vm = WheelPageVM(self.root)
                if scenario == 'stuck':
                    vm.stuck = True
                elif scenario == 'jump':
                    vm.position, vm.tick_pixels = 0, 400
                else:
                    vm.config['capture']['scroll_context']['max_views'] = 2
                reason = {'stuck': 'did not move', 'jump': 'overlap|align', 'limit': 'exceeding'}[scenario]
                with self.assertRaisesRegex(VMError, reason):
                    self.collect(vm)
                self.assertFalse(vm.clicks)

    def test_question_change_after_inference_is_rejected_during_wheel_return(self):
        vm = WheelPageVM(self.root)
        _, nav = self.collect(vm)
        point = nav.controls['01']
        vm.question_label = 'A different question on the same URL'
        with self.assertRaisesRegex(VMError, 'does not match|Recorded question'):
            nav.prepare({'target_id': '01', 'target_control': {'kind': point['kind'], 'box': point['box']},
                         'click_x': 75, 'click_y': 226, 'target_view': point['target_view']})
        self.assertFalse(vm.clicks)

    def test_static_or_independent_layouts_do_not_silently_gain_wheel_scrolling(self):
        vm = WheelPageVM(self.root)
        frame = vm.capture_frame()
        response = deepcopy(vm.layout)
        response['regions'][0]['scrollable'] = False
        response['occlusion_boxes'] = []
        self.assertFalse(parse_layout(json.dumps(response), frame, {}, wheel=True).areas[0].wheel)
        response['regions'][0]['role'] = 'question'
        response['regions'].append({'role': 'answers', 'box': [10, 80, 800, 120],
                                    'scrollbar_id': None, 'scrollable': True})
        with self.assertRaises(ScrollLayoutError):
            parse_layout(json.dumps(response), frame, {}, wheel=True)


if __name__ == '__main__':
    unittest.main()
