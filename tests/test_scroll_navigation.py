"""Multi-viewport graph/table selection using generated frames and a fake VM."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from capture import Frame, SethCapture
from ai_client import SethAIClient
from lightpanda_runtime import write_private_json
from inference import AIError, DecisionClient, build_control_prompt
from main import SethEngine
from scroll_capture import ScrollCollector, ScrollNavigationError, find_scrollbar
from scroll_navigation import scroll_offset
from tests.fake_vbox import TEST_DISPLAY
from tests.test_control_targets import draw_control
from tests.test_scroll_capture import screen
from vbox_backend import GuestDisplay, VMError
from web_inference import WebInferenceClient


class ScrollingVM(SethCapture):
    def __init__(self, root, kind='checkbox', two_panels=False):
        self.root, self.kind = Path(root), kind
        self.display = GuestDisplay(**(TEST_DISPLAY | {'width': 720, 'height': 540}))
        self.backend, self.max_age, self.counter = self, 90, 0
        self.panels = [360, 30] if two_panels else [360]
        self.positions = {panel: 1 for panel in self.panels}
        self.drifts = dict.fromkeys(self.panels, 0)
        self.post_click_shifts, self.question_panels = {}, set()
        self.selected, self.drags, self.clicks = set(), [], []
        self.header, self.stuck, self.drift = 'Question 1', False, 0
        self.mutate, self.fail_click = None, 0
        self.centers = [(24, 65 + row * 190) for row in range(5)] if kind == 'checkbox' else [
            (x, 60 + row * 95) for row in range(9) for x in (170, 240)]
        self.config = {'project_dir': str(root), 'capture': {'max_age_seconds': 90, 'scroll_context': {
            'enabled': True, 'max_views': 12, 'regions': [[.5, .28, .95, .86]]}},
            'api': {'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'test', 'model': 'test'},
            'clicker': {'delay_ms': [0, 0]}}
        if two_panels:
            self.config['capture']['scroll_context']['regions'].append([.04, .28, .49, .86])

    def document(self, panel):
        image = Image.new('RGB', (296, 950), 'white')
        draw = ImageDraw.Draw(image)
        if panel in self.question_panels:
            for row in range(45):
                draw.text((8, 15 + row * 20), f'Question paragraph {row}: details {row * 37}', fill='black')
            return image
        for index, (x, y) in enumerate(self.centers):
            draw_control(image, self.kind, (x, y), selected=(panel, index) in self.selected)
            if self.kind == 'checkbox':
                draw.text((48, y - 8), f'Option {index + 1}, graph {index * 31 + 11}', fill='black')
                draw.line((65, y + 95, 65, y + 20, 230, y + 20), fill='black', width=2)
                draw.line((65, y + 90, 210, y + 35 + index * 5), fill='blue', width=2)
            elif x == 170:
                draw.text((8, y - 8), f'Row {index // 2 + 1} value {index * 17}', fill='black')
                draw.line((0, y + 40, 290, y + 40), fill='#bbb')
        return image

    def capture_frame(self):
        self.counter += 1
        image = Image.new('RGB', (720, 540), 'white')
        ImageDraw.Draw(image).text((45, 95), self.header, fill='black')
        for panel in self.panels:
            offset = round(self.positions[panel] * (950 - 306)) + self.drift + self.drifts[panel]
            image.paste(self.document(panel).crop((0, offset, 296, offset + 306)), (panel, 157))
            native = screen(self.positions[panel], x=panel + 306, thumb_height=84)
            image.paste(native.crop((panel + 300, 150, panel + 324, 464)), (panel + 300, 150))
        if self.mutate:
            self.mutate(image)
        path = self.root / f'frame-{self.counter}.png'
        image.save(path)
        return Frame(path, self.display, time.monotonic())

    def drag(self, frame, x, y, end_x, end_y, max_age, stop_event):
        if stop_event is not None and stop_event.is_set():
            raise VMError('cancelled')
        panel = min(self.panels, key=lambda p: abs(p + 312 - x))
        self.drags.append((panel, x, y, end_x, end_y))
        with Image.open(frame.path) as image:
            bar = find_scrollbar(image, (panel, 151, panel + 324, 464))
        if not self.stuck:
            self.positions[panel] = max(0, min(1, (end_y - bar.top - bar.height / 2) / (bar.bottom - bar.top - bar.height)))

    def park_pointer(self, *args):
        pass

    def click(self, frame, x, y, max_age, stop_event=None):
        panel = max(p for p in self.panels if p <= x)
        offset = round(self.positions[panel] * (950 - 306)) + self.drift + self.drifts[panel]
        index = min(range(len(self.centers)), key=lambda i: abs(self.centers[i][0] + panel - x) + abs(self.centers[i][1] + 157 - offset - y))
        cx, cy = self.centers[index]
        if abs(cx + panel - x) > 2 or abs(cy + 157 - offset - y) > 2:
            raise AssertionError('Click missed the actual native control')
        self.clicks.append((panel, index))
        if len(self.clicks) != self.fail_click:
            if self.kind == 'radio':
                self.selected.difference_update({(panel, i) for i in range(len(self.centers)) if self.centers[i][1] == cy})
            self.selected.add((panel, index))
        for moved_panel, shift in self.post_click_shifts.items():
            self.drifts[moved_panel] += shift
        return {'click_x': x, 'click_y': y}


class ScrollNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stop = threading.Event()

    def collect(self, vm):
        question = ScrollCollector(vm.config, vm, self.stop).collect(vm.capture_frame())
        return question, question.navigate(vm.config, vm, self.stop)

    def decision(self, nav, identifiers, kind='multiple_answer'):
        raw = {'action': 'select', 'selection_type': kind, 'confidence': .99, 'explanation': 'Complete plan.',
               'selections': [({'option': f'Option {i + 1}'} if kind == 'multiple_answer' else {'row': i + 1, 'choice': 'True'})
                              | {'target_id': identifier} for i, identifier in enumerate(identifiers)]}
        return DecisionClient().parse_coordinates(json.dumps(raw), 720, 540, control_targets=nav.controls)

    def execute(self, vm, nav, decision):
        for point in nav.plan(decision):
            frame, target = nav.prepare(point)
            vm.verify_target(frame, target['click_x'], target['click_y'])
            vm.click(frame, target['click_x'], target['click_y'], 90)
            nav.verify_selection(frame, target)
            nav.confirm(target)

    def test_graph_options_receive_one_id_despite_overlap_and_select_across_views(self):
        vm = ScrollingVM(self.root)
        question, nav = self.collect(vm)
        self.assertGreater(len(question.views), 3)
        self.assertEqual(len(nav.controls), 5)
        self.assertTrue(any(len(node['occurrences']) > 1 for node in nav.records.values()))
        decision = self.decision(nav, ['05', '01', '03'])
        self.execute(vm, nav, decision)
        self.assertEqual(set(vm.clicks), {(360, 0), (360, 2), (360, 4)})
        self.assertEqual(len(vm.clicks), 3)
        self.assertGreaterEqual(len(nav.returned_views), 2)
        nav.verify_current()
        with self.assertRaises(ScrollNavigationError):
            nav.plan(decision)
        self.assertEqual(len(vm.clicks), 3)

    def test_table_rows_across_views_select_exactly_one_category_each(self):
        vm = ScrollingVM(self.root, 'radio')
        _, nav = self.collect(vm)
        self.assertEqual(len(nav.controls), 18)
        ids = [f'{index:02d}' for index in range(1, 19, 2)]
        self.execute(vm, nav, self.decision(nav, ids, 'classification'))
        self.assertEqual(vm.selected, {(360, index) for index in range(0, 18, 2)})
        self.assertEqual(len(vm.clicks), 9)
        nav.verify_current()

    def test_two_panels_keep_independent_views_and_identical_local_coordinates(self):
        vm = ScrollingVM(self.root, two_panels=True)
        _, nav = self.collect(vm)
        self.assertEqual(len(nav.controls), 10)
        self.execute(vm, nav, self.decision(nav, ['01', '06', '05', '10']))
        self.assertEqual(vm.selected, {(360, 0), (360, 4), (30, 0), (30, 4)})

    def test_question_panel_pixel_nudges_after_table_clicks_are_tracked_independently(self):
        vm = ScrollingVM(self.root, 'radio', two_panels=True)
        vm.question_panels = {30}
        vm.post_click_shifts = {30: -1}
        _, nav = self.collect(vm)
        self.assertEqual(len(nav.controls), 18)
        self.execute(vm, nav, self.decision(nav, [f'{index:02d}' for index in range(1, 19, 2)], 'classification'))
        self.assertEqual(len(vm.clicks), 9)
        self.assertEqual(nav.shifts[2], 9)
        self.assertEqual(vm.selected, {(360, index) for index in range(0, 18, 2)})
        nav.verify_current()

    def test_answer_panel_nudges_after_checkbox_clicks_are_verified_without_reclicking(self):
        vm = ScrollingVM(self.root)
        vm.post_click_shifts = {360: -1}
        _, nav = self.collect(vm)
        self.execute(vm, nav, self.decision(nav, ['01', '03', '05']))
        self.assertEqual(vm.clicks, [(360, 0), (360, 2), (360, 4)])
        self.assertEqual(nav.shifts[1], 3)
        nav.verify_current()

    def test_larger_post_click_scroll_stops_before_any_second_click(self):
        vm = ScrollingVM(self.root)
        vm.post_click_shifts = {360: -8}
        _, nav = self.collect(vm)
        with self.assertRaises(ScrollNavigationError):
            self.execute(vm, nav, self.decision(nav, ['01', '03']))
        self.assertEqual(len(vm.clicks), 1)
        self.assertEqual(nav.selected, set())

    def test_tiny_scroll_does_not_hide_even_a_small_foreground_change(self):
        vm = ScrollingVM(self.root)
        _, nav = self.collect(vm)
        vm.drifts[360] = -1
        vm.mutate = lambda image: ImageDraw.Draw(image).point((650, 250), fill='black')
        with self.assertRaises(ScrollNavigationError):
            nav.verify_current()
        self.assertEqual(vm.clicks, [])

    def test_small_return_offset_is_locally_verified_before_using_fresh_coordinates(self):
        vm = ScrollingVM(self.root)
        _, nav = self.collect(vm)
        vm.drift = 3
        # The current viewport must remain unchanged until the controlled drag.
        original = vm.drag
        vm.drift = 0
        def drag(*args):
            original(*args)
            vm.drift = 3
        vm.drag = drag
        self.execute(vm, nav, self.decision(nav, ['01']))
        self.assertEqual(vm.clicks, [(360, 0)])

    def test_same_table_row_in_different_views_is_rejected_before_any_click(self):
        vm = ScrollingVM(self.root, 'radio')
        _, nav = self.collect(vm)
        decision = self.decision(nav, ['01', '02'], 'classification')
        with self.assertRaisesRegex(ScrollNavigationError, 'same visible table row'):
            nav.plan(decision)
        self.assertEqual(vm.clicks, [])

    def test_stuck_return_and_changed_question_stop_before_click(self):
        vm = ScrollingVM(self.root)
        _, nav = self.collect(vm)
        point = self.decision(nav, ['01'])['selections'][0]
        vm.header = 'Question 2'
        with self.assertRaisesRegex(VMError, 'header changed'):
            nav.prepare(point)
        vm.header, vm.stuck = 'Question 1', True
        with self.assertRaisesRegex(ScrollNavigationError, 'did not return'):
            nav.prepare(point)
        self.assertEqual(vm.clicks, [])

    def test_cancellation_and_external_scroll_stop_before_another_input(self):
        vm = ScrollingVM(self.root)
        _, nav = self.collect(vm)
        point = self.decision(nav, ['01'])['selections'][0]
        count = len(vm.drags)
        self.stop.set()
        with self.assertRaisesRegex(VMError, 'cancelled'):
            nav.prepare(point)
        self.assertEqual(len(vm.drags), count)
        self.stop.clear()
        vm.positions[360] = .5
        with self.assertRaises(VMError):
            nav.prepare(point)
        self.assertEqual(len(vm.drags), count)

    def test_changed_option_text_after_return_fails_content_match(self):
        vm = ScrollingVM(self.root)
        _, nav = self.collect(vm)
        original = vm.drag
        def drag(*args):
            original(*args)
            vm.mutate = lambda image: ImageDraw.Draw(image).rectangle((400, 170, 500, 250), fill='black')
        vm.drag = drag
        with self.assertRaisesRegex(ScrollNavigationError, 'does not match'):
            nav.prepare(self.decision(nav, ['01'])['selections'][0])
        self.assertEqual(vm.clicks, [])

    def test_engine_uses_one_inference_and_stops_after_an_unconfirmed_click(self):
        vm = ScrollingVM(self.root)
        vm.fail_click = 2
        engine = SethEngine(vm.config, execute=True, manual_selection=True)
        engine.capture, engine.clicker.backend = vm, vm
        engine.toggle_capture()
        def infer(path, *, context_images, control_regions, scroll_targets):
            controls = scroll_targets['controls']
            raw = {'action': 'select', 'selection_type': 'multiple_answer', 'confidence': .99,
                   'explanation': 'Choose all three.', 'selections': [
                       {'option': label, 'target_id': identifier} for identifier, label in [('01','A'),('03','C'),('05','E')]]}
            resolved = DecisionClient().parse_coordinates(json.dumps(raw), 720, 540, control_targets=controls)
            return json.dumps(resolved)
        with patch.object(engine.ai, 'analyze_screenshot', side_effect=infer) as inference:
            result = engine.run_cycle()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['clicks_completed'], 2)
        self.assertEqual(len(vm.clicks), 2)
        self.assertTrue(result['clicks'][0]['selection_verified'])
        self.assertNotIn('selection_verified', result['clicks'][1])
        self.assertEqual(engine.progress, ('error', 'Stopped (2)'))
        inference.assert_called_once()

    def test_identical_pixel_points_in_different_views_are_distinct_targets(self):
        controls = {'01': {'kind': 'checkbox', 'box': [20, 30, 40, 50], 'target_view': '1:0'},
                    '02': {'kind': 'checkbox', 'box': [20, 30, 40, 50], 'target_view': '1:2'}}
        raw = json.dumps({'action': 'select', 'selection_type': 'multiple_answer', 'confidence': .95,
                          'explanation': 'Both.', 'selections': [{'option':'A','target_id':'01'}, {'option':'B','target_id':'02'}]})
        client = DecisionClient()
        result = client.parse_coordinates(raw, 720, 540, control_targets=controls)
        self.assertEqual(client.parse_coordinates(json.dumps(result), 720, 540, scroll_navigation=True), result)
        with self.assertRaises(AIError):
            client.parse_coordinates(json.dumps(result), 720, 540)

    def test_engine_completes_a_scrolled_table_from_one_inference_plan(self):
        vm = ScrollingVM(self.root, 'radio')
        engine = SethEngine(vm.config, execute=True, manual_selection=True)
        engine.capture, engine.clicker.backend = vm, vm
        engine.toggle_capture()
        def infer(path, *, context_images, control_regions, scroll_targets):
            raw = {'action': 'select', 'selection_type': 'classification', 'confidence': .98,
                   'explanation': 'Classify all rows.', 'selections': [
                       {'row': row + 1, 'choice': 'True', 'target_id': f'{row * 2 + 1:02d}'} for row in range(9)]}
            return json.dumps(DecisionClient().parse_coordinates(json.dumps(raw), 720, 540,
                                                                control_targets=scroll_targets['controls']))
        with patch.object(engine.ai, 'analyze_screenshot', side_effect=infer) as inference:
            result = engine.run_cycle()
        self.assertEqual(result['status'], 'clicked', result)
        self.assertEqual(result['click_count'], 9)
        self.assertTrue(all(click['selection_verified'] for click in result['clicks']))
        self.assertGreaterEqual(len(result['returned_views']), 2)
        self.assertEqual(vm.selected, {(360, index) for index in range(0, 18, 2)})
        inference.assert_called_once()

    def test_dry_or_uncertain_scrolled_plan_never_returns_or_clicks(self):
        for execute, confidence, status in ((False, .99, 'dry_run'), (True, .5, 'low_confidence')):
            with self.subTest(status=status):
                vm = ScrollingVM(self.root)
                engine = SethEngine(vm.config, execute=execute, manual_selection=True)
                engine.capture, engine.clicker.backend = vm, vm
                engine.toggle_capture()
                def infer(path, *, context_images, control_regions, scroll_targets):
                    raw = {'action':'select', 'selection_type':'multiple_answer', 'confidence':confidence,
                           'explanation':'Choose first.', 'selections':[{'option':'A','target_id':'01'}]}
                    return json.dumps(DecisionClient().parse_coordinates(json.dumps(raw), 720, 540,
                                                                        control_targets=scroll_targets['controls']))
                with patch.object(engine.ai, 'analyze_screenshot', side_effect=infer):
                    result = engine.run_cycle()
                self.assertEqual(result['status'], status, result)
                self.assertEqual(result['returned_views'], [])
                self.assertEqual(len(vm.drags), 5)
                self.assertEqual(vm.clicks, [])

    def test_api_gemini_and_qwen_upload_global_ids_and_preserve_view_metadata(self):
        image = Image.new('RGB', (320, 200), 'white')
        draw_control(image, 'checkbox', (70, 60))
        source = self.root / 'input.png'
        image.save(source)
        original = source.read_bytes()
        local = {'kind':'checkbox', 'box':[59,49,81,71]}
        controls = {'01':local | {'target_view':'1:0'}, '02':local | {'target_view':'1:2'}}
        payload = {'controls':controls, 'live_controls':{'02':local}}
        contexts = [{'path':source, 'box':(0,0,160,100), 'label':'Section 1', 'controls':{'01':local}}]
        raw = json.dumps({'action':'select', 'selection_type':'multiple_answer', 'confidence':.99,
                          'explanation':'Both options.', 'selections':[{'option':'A','target_id':'01'}, {'option':'B','target_id':'02'}]})
        for provider in ('api','gemini','qwen'):
            with self.subTest(provider=provider):
                uploads = []
                def exchange(upload, prompt):
                    uploads.append(upload)
                    self.assertIn('ALL sections are selectable', prompt)
                    self.assertNotIn('outside it, return action none', prompt)
                    with Image.open(upload) as packed:
                        self.assertEqual(packed.format, 'PNG')
                        self.assertGreater(packed.height, 200)
                        self.assertNotEqual(packed.crop((50,72,90,112)).tobytes(), image.crop((50,40,90,80)).tobytes())
                    return raw
                if provider == 'api':
                    client = SethAIClient({'api':{'base_url':'http://127.0.0.1:1/v1','api_key':'test','model':'test'}})
                    def call(upload, prompt, response_format):
                        return exchange(upload,prompt)
                    mocked = patch.object(client, '_analyze_screenshot', side_effect=call)
                else:
                    client = WebInferenceClient({'web':{'providers':[provider], 'state_dir':str(self.root / 'state'),
                                                        'executable_path':sys.executable, 'sites':{provider:{'model_label':'Fixture'}}}})
                    write_private_json(client.cookie_file(provider), [])
                    def run(name, deadline, operation):
                        self.assertEqual(name, provider)
                        adapter = Mock()
                        adapter.exchange.side_effect = lambda page,upload,prompt,report:exchange(upload,prompt)
                        return operation(adapter,None)
                    mocked = patch.object(client, '_run', side_effect=run)
                with mocked as inference:
                    response = client.analyze_screenshot(source, context_images=contexts, scroll_targets=payload)
                    inference.assert_called_once()
                result = client.parse_coordinates(response,320,200,scroll_navigation=True)
                self.assertEqual([point['target_view'] for point in result['selections']], ['1:0','1:2'])
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse(uploads[0].exists())

    def test_blank_or_repeated_content_is_not_used_for_guessing_scroll_offsets(self):
        blank = Image.new('RGB', (250, 300), 'white')
        with self.assertRaises(ScrollNavigationError):
            scroll_offset(blank, blank)
        repeated = blank.copy()
        draw = ImageDraw.Draw(repeated)
        for y in range(0, 300, 20):
            draw.text((20, y), 'identical row', fill='black')
        with self.assertRaises(ScrollNavigationError):
            scroll_offset(repeated, repeated)
        prompt = build_control_prompt(720, 540, {}, scroll_context=True, scroll_navigation=True)
        self.assertIn('ALL sections are selectable', prompt)
        self.assertNotIn('Every required new selection must be available in that live view', prompt)
