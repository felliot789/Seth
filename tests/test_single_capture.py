"""Single execution retains content identity and confirms every selection."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from capture import SethCapture
from control_targets import catalog_controls
from inference import DecisionClient
from main import SethEngine
from scroll_capture import Scrollbar
from single_capture import (prepare_single, _clear_choice_expansion,
                            _selected_controls, _mask_existing_focus)
from tests.test_control_targets import draw_control
from vbox_backend import GuestDisplay, VMError


class SingleVM:
    def __init__(self, kind='checkbox', hover=130):
        self.kind, self.hover = kind, hover
        self.display = GuestDisplay('single-test', 0, 320, 200, 0, 0, 1, 1, 1)
        self.selected, self.clicks, self.events = set(), [], []
        self.question, self.option, self.clock = 'Original question', 'Original option', '12:00'
        self.after_click = None

    def image(self):
        image = Image.new('RGB', (320, 200), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((10, 10), self.question, fill='black')
        draw.text((250, 190), self.clock, fill='black')
        for row in (70, 130):
            if self.hover == row:
                draw.rectangle((10, row - 25, 300, row + 25), fill='#f0f0f0')
            draw_control(image, self.kind, (50, row), selected=row in self.selected)
            draw.text((100, row - 5), f'{self.option} {row}', fill='black')
        return image

    def capture(self, path):
        self.image().save(path)
        return self.display, time.monotonic()

    def park_pointer(self, *args):
        self.events.append('park')
        self.hover = None

    def click(self, frame, x, y, max_age, stop_event=None):
        if stop_event is not None and stop_event.is_set():
            raise VMError('Unexpected input after cancellation')
        self.clicks.append(y)
        self.events.append('click')
        self.hover = y
        if self.kind == 'radio':
            self.selected.clear()
        self.selected.add(y)
        if self.after_click:
            self.after_click()
        return {'click_x': x, 'click_y': y}


class SingleAI(DecisionClient):
    supports_scroll_context = False

    def __init__(self, vm, action='select', provider='api', mutation=None):
        self.vm, self.action, self.last_provider, self.mutation = vm, action, provider, mutation

    def validate_config(self):
        pass

    def analyze_screenshot(self, path, *, control_regions=None):
        with Image.open(path) as image:
            controls = catalog_controls(image)
        points = [{'target_id': identifier, 'option': identifier} for identifier in controls]
        decision = {'action': self.action, 'confidence': .99, 'explanation': 'Test complete question'}
        if self.action == 'select':
            decision.update(selection_type='multiple_answer', selections=points)
        elif self.action == 'blank':
            decision.update(action='click', coordinate_space='pixels', click_x=250, click_y=40)
        else:
            decision.update(target_id='01')
        if self.mutation:
            self.mutation()
        if self.action == 'blank':
            return json.dumps(decision)
        return json.dumps(self.parse_coordinates(json.dumps(decision), 320, 200, control_targets=controls))


class SingleCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'project_dir': self.temp.name, 'capture': {'max_age_seconds': 90},
                       'clicker': {'delay_ms': [0, 0]}}

    def engine(self, vm, **kwargs):
        engine = SethEngine(self.config, execute=True)
        engine.capture = SethCapture(self.config, vm)
        engine.clicker.backend = vm
        engine.ai = SingleAI(vm, **kwargs)
        return engine

    def test_initial_hover_is_cleared_and_all_providers_confirm_both_answers(self):
        for provider in ('api', 'gemini', 'qwen'):
            with self.subTest(provider=provider):
                vm = SingleVM()
                result = self.engine(vm, provider=provider).run_cycle()
                self.assertEqual(result['status'], 'clicked', result)
                self.assertEqual(vm.clicks, [70, 130])
                self.assertEqual(vm.events[0], 'park')
                self.assertTrue(all(click['selection_verified'] for click in result['clicks']))

    def test_changed_question_or_option_outside_control_patch_prevents_any_click(self):
        for field in ('question', 'option'):
            with self.subTest(field=field):
                vm = SingleVM('radio')
                engine = self.engine(vm, action='click', mutation=lambda: setattr(vm, field, 'Replacement text'))
                result = engine.run_cycle()
                self.assertEqual(result['status'], 'error', result)
                self.assertIn('content changed', result['error'])
                self.assertEqual(vm.clicks, [])

    def test_arbitrary_coordinate_and_missed_input_never_report_done(self):
        vm = SingleVM('radio')
        result = self.engine(vm, action='blank').run_cycle()
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(vm.clicks, [])
        vm = SingleVM('radio')
        engine = self.engine(vm, action='click')
        vm.after_click = vm.selected.clear
        with patch.object(engine.capture, 'verify_selection', side_effect=VMError('Control stayed unchecked')):
            result = engine.run_cycle()
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(len(vm.clicks), 1)
        self.assertNotEqual(engine.progress[0], 'done')

    def test_final_click_cannot_silently_clear_a_previously_confirmed_answer(self):
        vm = SingleVM()
        def clear_earlier():
            if len(vm.clicks) == 2:
                vm.selected.discard(70)
        vm.after_click = clear_earlier
        result = self.engine(vm).run_cycle()
        self.assertEqual(result['status'], 'error', result)
        self.assertIn('previously confirmed', result['error'])
        self.assertEqual(result['clicks_completed'], 2)

    def test_age_and_display_changes_stop_before_execution(self):
        vm = SingleVM()
        capture = SethCapture(self.config, vm)
        question = prepare_single(self.config, capture, capture.capture_frame())
        question.frame = replace(question.frame, captured_at=time.monotonic() - 100)
        with self.assertRaisesRegex(VMError, 'too old'):
            question.verify()
        self.assertEqual(vm.clicks, [])
        question = prepare_single(self.config, capture, capture.capture_frame())
        vm.display = replace(vm.display, session_pid=2)
        with self.assertRaisesRegex(VMError, 'display changed'):
            question.verify()

    def test_grounded_native_viewport_ignores_desktop_clock_but_keeps_content_and_url(self):
        vm = SingleVM()
        capture = SethCapture(self.config, vm)
        bar = Scrollbar((307, 50, 317, 100), 40, 170, (0, 32, 320, 180), viewport=(32, 180))
        with patch('single_capture.find_scrollbars', return_value=[bar]):
            question = prepare_single(self.config, capture, capture.capture_frame())
        vm.clock = '12:01'
        question.verify()
        vm.option = 'Changed answer text'
        with self.assertRaisesRegex(VMError, 'content changed'):
            question.verify()


class NativeSingleMutationTests(unittest.TestCase):
    def footer_pair(self):
        images = []
        directory = Path(__file__).resolve().parents[1] / 'assets/native_controls'
        for name in ('clear_choice_before.png', 'clear_choice_after.png'):
            image = Image.new('RGB', (1200, 600), 'white')
            with Image.open(directory / name) as footer:
                image.paste(footer.convert('RGB'), (100, 350))
            ImageDraw.Draw(image).text((120, 100), 'Question and unchanged answer', fill='black')
            images.append(image)
        return images

    def test_known_footer_insertion_preserves_all_existing_question_pixels(self):
        before, after = self.footer_pair()
        controls = [{'kind': 'radio', 'box': [130, 200, 150, 220]}]
        self.assertTrue(_clear_choice_expansion(before, after, (0, 0, 1200, 600), controls))
        ImageDraw.Draw(after).text((120, 150), 'Changed other answer', fill='black')
        self.assertFalse(_clear_choice_expansion(before, after, (0, 0, 1200, 600), controls))

    def test_forged_footer_and_additional_control_are_rejected(self):
        controls = [{'kind': 'radio', 'box': [130, 200, 150, 220]}]
        for mutation in ('label', 'control'):
            before, after = self.footer_pair()
            if mutation == 'label':
                ImageDraw.Draw(after).rectangle((200, 355, 220, 367), fill='black')
            else:
                draw_control(after, 'radio', (600, 380))
            self.assertFalse(_clear_choice_expansion(before, after, (0, 0, 1200, 600), controls))

    def test_insertion_above_remaining_control_is_rejected(self):
        before, after = self.footer_pair()
        controls = [{'kind': 'radio', 'box': [130, 400, 150, 420]}]
        self.assertFalse(_clear_choice_expansion(before, after, (0, 0, 1200, 600), controls))

    def test_existing_selection_focus_blink_preserves_control_interior(self):
        before = Image.new('RGB', (180, 120), 'white')
        draw_control(before, 'radio', (60, 60), selected=True)
        selected = _selected_controls(before)
        self.assertEqual(len(selected), 1)
        after = before.copy()
        ImageDraw.Draw(after).line((74, 46, 74, 74), fill='#88bbff')
        _mask_existing_focus(before, selected)
        _mask_existing_focus(after, selected)
        self.assertEqual(before.tobytes(), after.tobytes())
        draw_control(after, 'radio', (60, 60), selected=False)
        # Explicitly erase the selected interior (drawing an outline alone does not).
        ImageDraw.Draw(after).ellipse((55, 55, 64, 64), fill='white')
        _mask_existing_focus(after, selected)
        self.assertNotEqual(before.tobytes(), after.tobytes())


if __name__ == '__main__':
    unittest.main()
