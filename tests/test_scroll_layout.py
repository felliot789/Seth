"""Automatic layout discovery across whole pages, fixed areas and independent panels."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from ai_client import SethAIClient
from capture import Frame, SethCapture
from inference import DecisionClient
from main import SethEngine
from scroll_capture import ScrollCollector, ScrollLayoutError, ScrollLimitError, find_scrollbars
from scroll_layout import discover_layout, local_wheel_layout, parse_layout
from scroll_navigation import scroll_offset
from scroll_capture import ScrollNavigationError
from scroll_masks import OverlayTracker
from types import SimpleNamespace
from tests.fake_vbox import TEST_DISPLAY
from tests.test_control_targets import draw_control
from tests.test_scroll_navigation import ScrollingVM
from vbox_backend import GuestDisplay
from web_inference import WebInferenceClient
from lightpanda_runtime import write_private_json


def layout(regions, *, identity=(20, 20, 680, 55), volatile=()):
    return {'action': 'layout', 'coordinate_space': 'pixels', 'confidence': .99,
            'regions': [{'role': role, 'box': list(box), 'scrollbar_id': identifier}
                        for role, box, identifier in regions],
            'identity_boxes': [list(identity)], 'volatile_boxes': [list(box) for box in volatile],
            'explanation': 'Detected question and answer viewports.'}


class PageVM(SethCapture):
    """A full-page scroller with a moving question heading and a fixed countdown."""
    box = (40, 136, 780, 568)
    timer = (150, 246, 245, 268)
    overlay = (90, 240, 340, 276)

    def __init__(self, root):
        self.root = Path(root)
        self.backend, self.max_age = self, 90
        self.position, self.counter = .5, 0
        self.selected, self.drags, self.clicks = set(), [], []
        self.stuck, self.changed, self.header = False, False, 'https://quiz.example/attempt?page=3'
        self.sticky = False
        self.question_label = 'Question 3: inspect all the graphs below'
        self.display = GuestDisplay(**(TEST_DISPLAY | {'width': 840, 'height': 620}))
        self.config = {'project_dir': str(root), 'capture': {'max_age_seconds': 90,
                       'scroll_context': {'enabled': True, 'layout': 'auto', 'max_views': 12}},
                       'api': {'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'test', 'model': 'test'},
                       'clicker': {'delay_ms': [0, 0]}}
        self.layout = layout([('both', self.box, '01')], volatile=[self.timer])
        self.layout['occlusion_boxes'] = [list(self.overlay)]

    def capture_frame(self):
        self.counter += 1
        image = Image.new('RGB', (840, 620), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((24, 28), self.header, fill='black')
        document = Image.new('RGB', (740, 1300), 'white')
        content = ImageDraw.Draw(document)
        content.text((10, 12), self.question_label, fill='black')
        for index in range(6):
            y = 90 + index * 190
            draw_control(document, 'checkbox', (35, y), selected=index in self.selected)
            content.text((65, y - 8), f'Option {index + 1}: value {index * 31}', fill='black')
            content.line((70, y + 85, 70, y + 20, 320, y + 20), fill='black', width=2)
            content.line((70, y + 80, 300, y + 35 + index * 5), fill='blue', width=2)
        if self.changed:
            content.rectangle((120, 600, 250, 640), fill='black')
        offset = round(self.position * (1300 - 432))
        image.paste(document.crop((0, offset, 740, offset + 432)), (40, 136))
        # Timer changes on every capture; its explicit exclusion must not mask controls.
        timer_shift = max(0, round((1 - self.position) * 200) - 50) if self.sticky else 0
        draw.rectangle((90, 240 + timer_shift, 339, 275 + timer_shift), fill='white')
        draw.text((95, 247 + timer_shift), 'Time:', fill='black')
        draw.text((155, 247 + timer_shift), f'0:49:{self.counter % 60:02}', fill='black')
        draw.text((280, 247 + timer_shift), 'Hide', fill='black')
        shade, x = '#8b8b8b', 824
        draw.polygon(((x + 6, 120), (x, 130), (x + 12, 130)), fill=shade)
        draw.polygon(((x, 570), (x + 12, 570), (x + 6, 580)), fill=shade)
        y = round(134 + self.position * 292)
        draw.rounded_rectangle((x, y, x + 12, y + 139), radius=5, fill=shade)
        path = self.root / f'page-{self.counter}.png'
        image.save(path)
        return Frame(path, self.display, time.monotonic())

    def drag(self, frame, x, y, end_x, end_y, max_age, stop_event):
        if stop_event is not None and stop_event.is_set():
            raise AssertionError('Input after cancellation')
        self.drags.append((x, y, end_x, end_y))
        if not self.stuck:
            self.position = max(0, min(1, (end_y - 203.5) / 292))

    def park_pointer(self, *args):
        pass

    def click(self, frame, x, y, max_age, stop_event=None):
        offset = round(self.position * 868)
        index = min(range(6), key=lambda i: abs(90 + i * 190 + 136 - offset - y))
        if abs(x - 75) > 2 or abs(90 + index * 190 + 136 - offset - y) > 2:
            raise AssertionError('Missed the native page checkbox')
        self.clicks.append(index)
        self.selected.add(index)
        return {'click_x': x, 'click_y': y}


class ObservedPanelVM(ScrollingVM):
    def drag(self, frame, x, y, end_x, end_y, max_age, stop_event):
        # The old fixture maps an absolute destination to an approximate ratio.
        # A real thumb retains its grab offset, including native arrow padding.
        if stop_event is not None and stop_event.is_set():
            raise AssertionError('Input after cancellation')
        panel = min(self.panels, key=lambda p: abs(p + 312 - x))
        self.drags.append((panel, x, y, end_x, end_y))
        if not self.stuck:
            self.positions[panel] = max(0, min(1, self.positions[panel] + (end_y - y) / (253 - 84)))


class AutomaticScrollLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stop = threading.Event()

    def collect(self, vm, response=None):
        client = Mock(inspect_layout=Mock(return_value=json.dumps(response or vm.layout)))
        question = ScrollCollector(vm.config, vm, self.stop).collect(vm.capture_frame(), layout_client=client)
        client.inspect_layout.assert_called_once()
        self.assertFalse(Path(client.inspect_layout.call_args.args[0]).exists())
        return question, question.navigate(vm.config, vm, self.stop)

    def split(self, *, scrolling_question=False, both=False):
        vm = ObservedPanelVM(self.root, two_panels=both)
        vm.config['capture']['scroll_context'] = {'enabled': True, 'layout': 'auto', 'max_views': 12}
        vm.mutate = lambda image: ImageDraw.Draw(image).text((40, 125), 'Fixed question diagram', fill='black')
        identity = (30, 80, 300, 115)
        if both:
            # Semantic roles can be reversed; scroll navigation has no left/right assumption.
            vm.question_panels = {360}
            response = layout([('answers', (30, 157, 326, 463), '01'),
                               ('question', (360, 157, 656, 463), '02')], identity=identity)
        else:
            vm.mutate = lambda image: ImageDraw.Draw(image).text((45, 220), 'Fixed question diagram', fill='black')
            response = layout([('question', (30, 157, 326, 463), None),
                               ('answers', (360, 157, 656, 463), '01')], identity=identity)
            if scrolling_question:
                vm.question_panels = {360}
                def fixed_answer(image):
                    draw_control(image, 'checkbox', (75, 230))
                    ImageDraw.Draw(image).text((100, 222), 'Fixed answer', fill='black')
                vm.mutate = fixed_answer
                response['regions'][0]['role'] = 'answers'
                response['regions'][1]['role'] = 'question'
        return vm, response

    def test_full_screen_discovery_finds_outer_and_independent_native_scrollbars(self):
        vm = PageVM(self.root)
        with Image.open(vm.capture_frame().path) as image:
            self.assertEqual([bar.thumb[0] for bar in find_scrollbars(image)], [824])
        vm, _ = self.split(both=True)
        with Image.open(vm.capture_frame().path) as image:
            self.assertEqual([bar.thumb[0] for bar in find_scrollbars(image)], [336, 666])

    def bordered_page(self, *, right=760, extra_border=False, divider=True):
        """Site-neutral browser/card geometry, including expanded sidebar width."""
        from scroll_capture import Scrollbar
        image = Image.new('RGB', (1000, 700), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 999, 79), fill='#222222')
        draw.text((30, 35), 'https://quiz.example/question/5', fill='white')
        if divider:
            draw.line((0, 140, 980, 140), fill='#dddddd')
        draw.rounded_rectangle((180, 280, right, 800), radius=8, outline='#cccccc')
        draw.text((205, 310), 'Question: Read the complete passage and all answers.', fill='black')
        draw.text((right - 70, 350), 'Text', fill='black')
        if extra_border:
            draw.line((500, 280, 500, 659), fill='#cccccc')
        path = self.root / f'bordered-{right}-{extra_border}-{divider}.png'
        image.save(path)
        display = GuestDisplay(**(TEST_DISPLAY | {'width': 1000, 'height': 700}))
        frame = Frame(path, display, time.monotonic())
        bars = {'01': Scrollbar((985, 95, 997, 295), 94, 648, (0, 80, 1000, 660), viewport=(80, 660))}
        return frame, bars

    def test_local_whole_page_geometry_handles_sidebar_width_without_model_request(self):
        for right in (760, 910):
            with self.subTest(right=right):
                frame, bars = self.bordered_page(right=right)
                capture = Mock(capture_frame=Mock(return_value=frame))
                collector = ScrollCollector({'capture': {'scroll_context': {'scroll_method': 'wheel'}}}, capture)
                client = Mock()
                with patch('scroll_layout.find_scrollbars', return_value=list(bars.values())):
                    discovered = discover_layout(collector, frame, client)
                client.inspect_layout.assert_not_called()
                self.assertEqual(discovered.areas[0].box, (184, 141, right - 4, 660))
                self.assertEqual(discovered.identity_boxes, [(0, 0, 1000, 80)])
                self.assertTrue(discovered.areas[0].wheel)

    def test_local_geometry_requires_unique_column_and_outer_browser_rail(self):
        frame, bars = self.bordered_page()
        self.assertIsNone(local_wheel_layout(frame, {}))
        self.assertIsNone(local_wheel_layout(frame, bars | {'02': bars['01']}))
        self.assertIsNone(local_wheel_layout(frame, {'01': replace(bars['01'], viewport=())}))
        self.assertIsNone(local_wheel_layout(frame, {'01': replace(bars['01'], thumb=(600, 95, 612, 295))}))
        for kwargs in ({'extra_border': True}, {'divider': False}, {'right': 600}):
            frame, bars = self.bordered_page(**kwargs)
            self.assertIsNone(local_wheel_layout(frame, bars))

    def test_local_geometry_still_rejects_reflow_before_wheel_input(self):
        initial, bars = self.bordered_page(right=760)
        fresh, _ = self.bordered_page(right=910)
        capture = Mock(capture_frame=Mock(side_effect=[initial, fresh]))
        collector = ScrollCollector({'capture': {'scroll_context': {'scroll_method': 'wheel'}}}, capture)
        with patch('scroll_layout.find_scrollbars', return_value=list(bars.values())):
            with self.assertRaisesRegex(ScrollLayoutError, 'content changed'):
                discover_layout(collector, initial, Mock())
        capture.backend.wheel.assert_not_called()

    def test_actual_antialiased_scrollbar_arrow_is_not_lost_in_full_screen_scan(self):
        fixture = Path(__file__).parent / 'fixtures/scrollbars/native-light-arrow.png'
        with Image.open(fixture) as strip:
            image = Image.new('RGB', (840, 980), 'white')
            image.paste(strip, (810, 30))
        bars = find_scrollbars(image)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].thumb, (812, 538, 825, 896))

    def test_normalized_layout_bounds_are_converted_to_original_vm_pixels(self):
        vm = PageVM(self.root)
        frame = vm.capture_frame()
        with Image.open(frame.path) as image:
            bars = {'01': find_scrollbars(image)[0]}
        response = deepcopy(vm.layout)
        response['coordinate_space'] = 'normalized_1000'
        for box in [response['regions'][0]['box'], *response['identity_boxes'],
                    *response['volatile_boxes'], *response['occlusion_boxes']]:
            box[:] = [round(value * 1000 / (840 if index % 2 == 0 else 620)) for index, value in enumerate(box)]
        discovered = parse_layout(json.dumps(response), frame, bars)
        self.assertLessEqual(max(abs(a - b) for a, b in zip(discovered.areas[0].box, vm.box)), 1)

    def test_layout_boxes_can_use_objects_to_avoid_web_citation_rendering(self):
        vm = PageVM(self.root)
        frame = vm.capture_frame()
        with Image.open(frame.path) as image:
            bars = {'01': find_scrollbars(image)[0]}
        response = deepcopy(vm.layout)
        for field in ('identity_boxes', 'volatile_boxes', 'occlusion_boxes'):
            response[field] = [{'box': box} for box in response[field]]
        parsed = parse_layout(json.dumps(response), frame, bars)
        self.assertEqual(parsed.ignore_boxes, [vm.timer])
        self.assertEqual(parsed.occlusion_boxes, [vm.overlay])

    def test_blank_identity_can_use_locally_verified_outer_browser_chrome(self):
        vm = PageVM(self.root)
        frame = vm.capture_frame()
        with Image.open(frame.path) as image:
            native = find_scrollbars(image)[0]
        response = deepcopy(vm.layout)
        response['identity_boxes'] = [[20, 60, 200, 80]]
        bars = {'01': replace(native, viewport=(136, 568))}
        parsed = parse_layout(json.dumps(response), frame, bars, wheel=True)
        self.assertEqual(parsed.identity_boxes, [(0, 0, 840, 136)])
        self.assertTrue(parsed.areas[0].wheel)
        # A blank identity is still rejected without a locally grounded outer viewport.
        response['regions'][0].update(scrollbar_id=None, scrollable=True)
        with self.assertRaisesRegex(ScrollLayoutError, 'visible stationary'):
            parse_layout(json.dumps(response), frame, {}, wheel=True)

    def test_actual_timer_can_be_tracked_after_sticky_font_rerasterization(self):
        fixture = Path(__file__).parent / 'fixtures/scrollbars'
        paths = []
        for name, y in [('timer-sticky.png', 140), ('timer-flow.png', 380)]:
            image = Image.new('RGB', (840, 620), 'white')
            with Image.open(fixture / name) as widget:
                image.paste(widget, (410, y))
            path = self.root / name
            image.save(path)
            paths.append(path)
        tracker = OverlayTracker(SimpleNamespace(path=paths[0]), [(522, 148, 599, 178)], [(410, 140, 766, 188)])
        with Image.open(paths[1]) as image:
            self.assertEqual(tracker.boxes(image), [(522, 388, 599, 418)])

    def test_fixed_overlays_are_masked_in_both_projections_of_scroll_alignment(self):
        document = Image.new('RGB', (400, 600), 'white')
        draw = ImageDraw.Draw(document)
        for row in range(28):
            draw.text((15 + row % 7, row * 20), f'Row {row}: measurement {row * 37 + 19}', fill='black')
        a, b = document.crop((0, 0, 400, 320)), document.crop((0, 37, 400, 357))
        ignored = [(10, 100, 210, 145)]
        for image, text in ((a, 'Time: 0:49:35'), (b, 'Time: 0:49:34')):
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 100, 209, 144), fill='white')
            draw.text((15, 110), text, fill='black')
        self.assertEqual(scroll_offset(a, b, 37, ignored=ignored, minimum=1), 37)
        ImageDraw.Draw(b).point((55, 190), fill='black')
        with self.assertRaises(ScrollNavigationError):
            scroll_offset(a, b, 37, ignored=ignored, minimum=1)

    def test_cancellation_during_layout_request_prevents_every_drag(self):
        vm = PageVM(self.root)
        def cancel(*args):
            self.stop.set()
            return json.dumps(vm.layout)
        with self.assertRaisesRegex(Exception, 'cancelled'):
            ScrollCollector(vm.config, vm, self.stop).collect(vm.capture_frame(), layout_client=Mock(inspect_layout=cancel))
        self.assertFalse(vm.drags)

    def test_static_layout_uses_discovered_controls_without_any_scroll(self):
        vm = PageVM(self.root)
        original = vm.capture_frame
        def fixed():
            frame = original()
            with Image.open(frame.path) as image:
                image = image.convert('RGB')
            ImageDraw.Draw(image).rectangle((810, 100, 839, 600), fill='white')
            image.save(frame.path)
            return frame
        vm.capture_frame = fixed
        response = deepcopy(vm.layout)
        response['regions'][0]['scrollbar_id'] = None
        response['occlusion_boxes'] = []
        question, nav = self.collect(vm, response)
        self.assertFalse(vm.drags)
        self.assertFalse(question.views)
        self.assertTrue(nav.controls)
        self.assertTrue(all(control['target_view'] == 'live' for control in nav.controls.values()))

    def test_whole_page_captures_moving_heading_and_returns_to_all_answer_views(self):
        vm = PageVM(self.root)
        question, nav = self.collect(vm)
        self.assertEqual(len(nav.controls), 6)
        self.assertTrue(question.ignore_boxes)
        self.assertEqual(vm.position, 1)
        chosen = [identifier for identifier, node in nav.records.items()
                  if node['global_box'][1] == min(n['global_box'][1] for n in nav.records.values())]
        chosen += [max(nav.controls)]
        for identifier in chosen:
            control = nav.controls[identifier]
            raw = json.dumps({'action': 'select', 'selection_type': 'multiple_answer', 'confidence': .99,
                              'explanation': 'Test', 'selections': [{'option': identifier, 'target_id': identifier}]})
            decision = DecisionClient().parse_coordinates(raw, 840, 620, control_targets=nav.controls)
            target = nav.plan(decision)[0]
            frame, point = nav.prepare(target)
            vm.click(frame, point['click_x'], point['click_y'], 90)
            nav.verify_selection(frame, point)
            nav.confirm(point)
        self.assertEqual(vm.selected, {0, 5})
        self.assertTrue(nav.returned_views)
        nav.verify_current()

    def test_countdown_is_tracked_when_it_moves_with_page_then_becomes_sticky(self):
        vm = PageVM(self.root)
        vm.sticky = True
        for key in ('volatile_boxes', 'occlusion_boxes'):
            vm.layout[key] = [[x1, y1 + 50, x2, y2 + 50] for x1, y1, x2, y2 in vm.layout[key]]
        question, nav = self.collect(vm)
        self.assertEqual(len(nav.controls), 6)
        for identifier in ('01', '06'):
            frame, point = nav.prepare({'target_id': identifier})
            vm.verify_target(frame, point['click_x'], point['click_y'])
        nav.verify_current()
        self.assertFalse(vm.clicks)

    def test_changed_offscreen_question_is_rejected_even_when_url_and_answers_match(self):
        vm = PageVM(self.root)
        _, nav = self.collect(vm)
        vm.question_label = 'A completely different question has replaced this prompt'
        with self.assertRaisesRegex(ScrollNavigationError, 'does not match'):
            nav.prepare({'target_id': max(nav.controls)})
        self.assertFalse(vm.clicks)

    def test_independent_panels_are_discovered_without_region_configuration(self):
        vm, response = self.split(both=True)
        question, nav = self.collect(vm, response)
        self.assertEqual(len(question.layout), 2)
        self.assertEqual(len(nav.controls), 5)
        self.assertEqual(set(vm.positions.values()), {1})
        self.assertEqual({node['panel'] for node in nav.records.values()}, {1})

    def test_fixed_question_and_scrollable_answers_both_reach_the_model(self):
        vm, response = self.split()
        question, nav = self.collect(vm, response)
        self.assertEqual(len(nav.controls), 5)
        self.assertIn((30, 157, 326, 463), question.regions)
        self.assertTrue(nav.contexts)

    def test_scrollable_question_preserves_fixed_answer_controls(self):
        vm, response = self.split(scrolling_question=True)
        question, nav = self.collect(vm, response)
        self.assertEqual(len(nav.controls), 1)
        self.assertEqual(nav.controls['01']['target_view'], 'live')
        self.assertTrue(question.contexts)

    def test_layout_change_while_provider_is_working_stops_before_probe(self):
        vm = PageVM(self.root)
        def change(*args):
            vm.header = 'https://quiz.example/attempt?page=4'
            return json.dumps(vm.layout)
        with self.assertRaisesRegex(Exception, 'header changed'):
            ScrollCollector(vm.config, vm, self.stop).collect(vm.capture_frame(), layout_client=Mock(inspect_layout=change))
        self.assertFalse(vm.drags)

    def test_wrong_scrollbar_ownership_is_rejected_after_restoring(self):
        vm, response = self.split(both=True)
        response['regions'][0]['scrollbar_id'], response['regions'][1]['scrollbar_id'] = '02', '01'
        with self.assertRaisesRegex(ScrollLayoutError, 'which question/answer area|incompatible'):
            self.collect(vm, response)
        self.assertFalse(vm.clicks)

    def test_no_matching_scrollbar_is_an_explicit_layout_error(self):
        vm = PageVM(self.root)
        response = deepcopy(vm.layout)
        response['regions'][0]['scrollbar_id'] = '99'
        with self.assertRaisesRegex(ScrollLayoutError, 'unknown'):
            self.collect(vm, response)
        self.assertFalse(vm.drags)

    def test_visible_scrollbar_cannot_silently_fall_back_to_a_static_capture(self):
        vm = PageVM(self.root)
        response = deepcopy(vm.layout)
        response['regions'][0]['scrollbar_id'] = None
        with self.assertRaisesRegex(ScrollLayoutError, 'not associated'):
            self.collect(vm, response)
        self.assertFalse(vm.drags)

    def test_view_limit_is_checked_before_any_probe(self):
        vm = PageVM(self.root)
        vm.config['capture']['scroll_context']['max_views'] = 2
        with self.assertRaises(ScrollLimitError):
            self.collect(vm)
        self.assertFalse(vm.drags)

    def test_stuck_scrollbar_does_not_send_answer_inference(self):
        vm = PageVM(self.root)
        vm.stuck = True
        with self.assertRaisesRegex(ScrollLayoutError, 'did not move'):
            self.collect(vm)
        self.assertEqual(len(vm.drags), 1)
        self.assertFalse(vm.clicks)

    def test_uncertain_or_malformed_layout_cannot_send_vm_input(self):
        vm = PageVM(self.root)
        frame = vm.capture_frame()
        with Image.open(frame.path) as image:
            bars = {'01': find_scrollbars(image)[0]}
        bad = []
        for field, value in [('confidence', .7), ('confidence', float('nan')), ('confidence', True),
                             ('coordinate_space', 'normalized'), ('action', 'none'),
                             ('identity_boxes', [[40, 140, 200, 160]]),
                             ('volatile_boxes', [[40, 136, 780, 568]]),
                             ('volatile_boxes', [[60, 161, 90, 185]])]:
            candidate = deepcopy(vm.layout)
            candidate[field] = value
            bad.append(candidate)
        candidate = deepcopy(vm.layout)
        candidate['regions'][0]['box'][0] = True
        bad.append(candidate)
        for response in bad:
            with self.subTest(response=response), self.assertRaises(ScrollLayoutError):
                parse_layout(json.dumps(response), frame, bars)
        self.assertFalse(vm.drags)

    def test_engine_indexes_complete_page_using_two_separate_inference_requests(self):
        vm = PageVM(self.root)
        engine = SethEngine(vm.config, execute=False)
        engine.capture, engine.clicker.backend = vm, vm
        engine.ai.inspect_layout = Mock(return_value=json.dumps(vm.layout))
        def answer(path, *, context_images, control_regions, scroll_targets):
            self.assertEqual(len(scroll_targets['controls']), 6)
            return json.dumps({'action': 'none', 'confidence': 0, 'explanation': 'Capture test'})
        engine.ai.analyze_screenshot = Mock(side_effect=answer)
        result = engine.run_cycle()
        self.assertEqual(result['status'], 'no_action', result)
        self.assertTrue(result['scroll_layout'])
        engine.ai.inspect_layout.assert_called_once()
        engine.ai.analyze_screenshot.assert_called_once()
        self.assertFalse(vm.clicks)


class LayoutProviderTests(unittest.TestCase):
    def test_api_can_resolve_discovered_static_controls_without_context_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixed.png'
            image = Image.new('RGB', (320, 200), 'white')
            draw_control(image, 'radio', (60, 60))
            image.save(path)
            controls = {'01': {'kind': 'radio', 'box': [49, 49, 71, 71], 'target_view': 'live'}}
            client = SethAIClient({})
            raw = json.dumps({'action': 'click', 'coordinate_space': 'pixels', 'click_x': 60, 'click_y': 60, 'confidence': .99, 'explanation': 'Fixture'})
            with patch.object(client, 'validate_config'), patch.object(client, '_analyze_screenshot', return_value=raw):
                result = json.loads(client.analyze_screenshot(path, context_images=[],
                                    scroll_targets={'controls': controls, 'live_controls': controls}))
            self.assertEqual((result['click_x'], result['click_y'], result['target_view']), (60, 60, 'live'))

    def test_api_layout_response_bypasses_answer_parser(self):
        client = SethAIClient({})
        raw = '{"action":"layout"}'
        with patch.object(client, '_analyze_screenshot', return_value=raw) as exchange:
            self.assertEqual(client.inspect_layout('image.png', 'layout prompt'), raw)
        exchange.assert_called_once_with('image.png', 'layout prompt')

    def test_web_layout_response_is_raw_and_uses_selected_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'question.png'
            Image.new('RGB', (320, 200), 'white').save(image)
            for provider in ('gemini', 'qwen'):
                client = WebInferenceClient({'web': {'state_dir': str(root), 'providers': [provider],
                                                    'sites': {provider: {'model_label': 'Test'}}}})
                write_private_json(client.cookie_file(provider), [])
                with patch.object(client, 'validate_config'), patch.object(client, '_run', return_value='{"action":"layout"}') as run:
                    self.assertEqual(client.inspect_layout(image, 'layout prompt'), '{"action":"layout"}')
                self.assertEqual(client.last_provider, provider)
                self.assertEqual(run.call_args.args[0], provider)


if __name__ == '__main__':
    unittest.main()
