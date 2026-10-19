"""Long image-answer questions retain every pixel and stable control IDs."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from PIL import Image, ImageDraw

from capture import Frame
from control_targets import _context_sheet, _document_sheet, annotated_controls, catalog_controls, locate_control
from inference import AIError, DecisionClient
from scroll_capture import ScrollCollector
from tests.test_control_targets import draw_control
from tests.test_wheel_scroll import WheelPageVM


class LongImagePageVM(WheelPageVM):
    height = 3200
    answer_rows = [350 + index * 600 for index in range(5)]

    def __init__(self, root):
        super().__init__(root)
        self.position = 1
        self.config['capture']['max_age_seconds'] = 300
        self.config['capture']['scroll_context']['max_views'] = 32
        self.layout['volatile_boxes'] = []
        self.layout['occlusion_boxes'] = []

    def document(self):
        image = Image.new('RGB', (740, self.height), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((10, 12), self.question_label, fill='black')
        for index, row in enumerate(self.answer_rows):
            draw_control(image, 'radio', (35, row), selected=index in self.selected)
            for level in range(4):
                y = row + 30 + level * 100
                draw.rectangle((90 + index * 4 + level * 20, y, 620 - level * 13, y + 65 - level * 4), outline='black', width=2)
                draw.text((120, y + 20), f'Diagram {index + 1}: stage {level + 1}; value {index * 71 + level * 13}', fill='black')
                if level < 3:
                    draw.line((355, y + 65, 355, y + 100), fill='black', width=2)
        return image

    def capture_frame(self):
        self.counter += 1
        image = Image.new('RGB', (840, 620), 'white')
        ImageDraw.Draw(image).text((24, 28), self.header, fill='black')
        offset = round(self.position * (self.height - 432))
        image.paste(self.document().crop((0, offset, 740, offset + 432)), (40, 136))
        path = self.root / f'long-{self.counter}.png'
        image.save(path)
        return Frame(path, self.display, time.monotonic())

    def wheel(self, frame, x, y, ticks, max_age, stop_event=None):
        if stop_event is not None and stop_event.is_set():
            raise AssertionError('Input after cancellation')
        self.wheels.append(ticks)
        self.position = max(0, min(1, self.position + ticks * self.tick_pixels / (self.height - 432)))

    def click(self, frame, x, y, max_age, stop_event=None):
        offset = round(self.position * (self.height - 432))
        index = min(range(5), key=lambda i: abs(self.answer_rows[i] + 136 - offset - y))
        if abs(x - 75) > 2 or abs(self.answer_rows[index] + 136 - offset - y) > 2:
            raise AssertionError('Missed the single-choice radio')
        self.clicks.append(index)
        self.selected = {index}
        return {'click_x': x, 'click_y': y}


class ScrollDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_round_letter_in_a_native_diagram_word_is_not_an_answer_badge(self):
        path = Path(__file__).parent / 'fixtures/controls/native-diagram-word.png'
        with Image.open(path) as image:
            self.assertEqual(catalog_controls(image), {})
            with self.assertRaisesRegex(AIError, 'unambiguous'):
                locate_control(image, 34, 16, 'radio')

    def test_more_than_twelve_views_keep_complete_diagrams_and_return_to_one_single_choice(self):
        vm = LongImagePageVM(self.root)
        stop = threading.Event()
        client = Mock(inspect_layout=Mock(return_value=json.dumps(vm.layout)))
        question = ScrollCollector(vm.config, vm, stop).collect(vm.capture_frame(), layout_client=client)
        nav = question.navigate(vm.config, vm, stop)
        self.assertGreater(len(question.views), 12)
        self.assertEqual(set(nav.controls), {'01', '02', '03', '04', '05'})
        self.assertTrue(all(control['kind'] == 'radio' for control in nav.controls.values()))
        self.assertGreater(len({control['target_view'] for control in nav.controls.values()}), 1)
        with Image.open(question.frame.path) as answer:
            with self.assertRaisesRegex(AIError, 'bounded size'):
                _context_sheet(answer, nav.contexts)
        with annotated_controls(question.frame.path, context_images=nav.contexts, scroll_targets=nav.payload) as (upload, controls):
            with Image.open(upload) as packed:
                self.assertEqual(packed.size, (740, vm.height + 36))
                # Every diagram pixel, including lines spanning capture seams,
                # survives exactly. All badges are left of this body crop.
                self.assertEqual(packed.crop((90, 36, 740, vm.height + 36)).tobytes(),
                                 vm.document().crop((90, 0, 740, vm.height)).tobytes())
                for row in vm.answer_rows:
                    badge = packed.crop((20, row + 26, 50, row + 46)).convert('RGB')
                    self.assertGreater(sum(pixel == (255, 242, 0) for pixel in badge.getdata()), 50)
            self.assertEqual(controls, nav.controls)
        decision = DecisionClient().parse_coordinates(json.dumps({
            'action': 'click', 'target_id': '03', 'confidence': .99, 'explanation': 'Single diagram choice'}),
            840, 620, control_targets=nav.controls)
        frame, target = nav.prepare(decision)
        vm.click(frame, target['click_x'], target['click_y'], 300)
        nav.verify_selection(frame, target)
        nav.confirm(target)
        self.assertEqual(vm.clicks, [2])
        self.assertEqual(vm.selected, {2})
        client.inspect_layout.assert_called_once()

    def test_invalid_gapped_or_oversized_documents_are_rejected_without_truncation(self):
        source = self.root / 'frame.png'
        Image.new('RGB', (500, 500), 'white').save(source)
        valid = {'sections': [{'path': source, 'box': [0, 0, 500, 500], 'offset': offset}
                              for offset in (0, 250)], 'controls': {}}
        answer = Image.new('RGB', (500, 500), 'white')
        for offsets in ([1, 250], [0, 0], [0, 400], [0, 250.5], list(range(0, 9000, 300))):
            bad = deepcopy(valid)
            bad['sections'] = [{'path': source, 'box': [0, 0, 500, 500], 'offset': offset} for offset in offsets]
            with self.subTest(offsets=offsets), self.assertRaises(AIError):
                _document_sheet(answer, bad)
        bad = deepcopy(valid)
        bad['controls'] = {'01': {'kind': 'radio', 'box': [20, 740, 40, 760]}}
        with self.assertRaisesRegex(AIError, 'badge lies outside'):
            _document_sheet(answer, bad)


if __name__ == '__main__':
    unittest.main()
