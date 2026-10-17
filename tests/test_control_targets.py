"""Answer-control grounding and visible-selection regressions, without a VM."""

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from control_targets import annotated_controls, catalog_controls, ground_decision, locate_control, selection_visible
from capture import Frame, SethCapture
from inference import AIError, DecisionClient, build_control_prompt
from tests.fake_vbox import TEST_DISPLAY
from vbox_backend import GuestDisplay, VMError


def draw_control(image, kind, center=(60, 60), size=22, selected=False):
    x, y = center
    box = (x - size // 2, y - size // 2, x - size // 2 + size - 1, y - size // 2 + size - 1)
    draw = ImageDraw.Draw(image)
    if kind == "radio":
        draw.ellipse(box, outline="#aaa", width=2)
        if selected:
            radius = max(2, size // 5)
            draw.ellipse((x - radius, y - radius, x + radius - 1, y + radius - 1), fill="#0877c9")
    else:
        draw.rounded_rectangle(box, radius=2, outline="#aaa", width=2,
                               fill="#0877c9" if selected else None)
        if selected:
            draw.line((x - 5, y, x - 1, y + 4, x + 5, y - 4), fill="white", width=2)


class ControlTargetTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (160, 140), "white")

    def test_real_missed_radio_centers_thirteen_pixels_to_the_right(self):
        # Only the anonymous 40x40 control glyph is retained from the real VM.
        fixture = Path(__file__).parent / "fixtures/controls/unchecked-radio.png"
        with Image.open(fixture) as image:
            control, x, y = locate_control(image, 7, 17, "radio")
        self.assertEqual((x, y), (20, 20))
        self.assertEqual(control, {"kind": "radio", "box": [9, 9, 31, 31]})

    def test_real_selected_radio_with_a_white_check_is_confirmed(self):
        fixtures = Path(__file__).parent / "fixtures/controls"
        with Image.open(fixtures / "unchecked-radio-large.png") as before, \
                Image.open(fixtures / "selected-radio.png") as after:
            control, _, _ = locate_control(before, 20, 20, "radio")
            self.assertTrue(selection_visible(before, after, control))
            self.assertEqual(catalog_controls(after), {})

    def test_radio_and_checkbox_sizes_and_dark_background(self):
        for kind in ("radio", "checkbox"):
            for size in (16, 22, 30):
                with self.subTest(kind=kind, size=size):
                    image = self.image.copy()
                    draw_control(image, kind, size=size)
                    for candidate in (image, Image.eval(image, lambda value: 255 - value)):
                        _, x, y = locate_control(candidate, 47, 57, kind)
                        self.assertEqual((x, y), (60, 60))

    def test_absent_distant_wrong_shape_and_clipped_controls_are_rejected(self):
        cases = [(self.image.copy(), (60, 60), "radio")]
        distant = self.image.copy()
        draw_control(distant, "radio", (90, 60))
        cases.append((distant, (60, 60), "radio"))
        for actual, requested in (("radio", "checkbox"), ("checkbox", "radio")):
            wrong = self.image.copy()
            draw_control(wrong, actual)
            cases.append((wrong, (60, 60), requested))
        oval = self.image.copy()
        ImageDraw.Draw(oval).ellipse((51, 48, 68, 71), outline="black", width=2)
        cases.append((oval, (60, 60), "radio"))
        clipped = self.image.copy()
        draw_control(clipped, "radio", (3, 60))
        cases.append((clipped, (3, 60), "radio"))
        for image, point, kind in cases:
            with self.subTest(point=point, kind=kind), self.assertRaises(AIError) as caught:
                locate_control(image, *point, kind)
            self.assertEqual(caught.exception.code, "browser_grounding")

    def test_selected_and_ambiguous_neighboring_controls_are_rejected(self):
        for kind in ("radio", "checkbox"):
            with self.subTest(kind=kind):
                selected = self.image.copy()
                draw_control(selected, kind, selected=True)
                with self.assertRaises(AIError):
                    locate_control(selected, 60, 60, kind)
                neighbors = self.image.copy()
                draw_control(neighbors, kind, (60, 40))
                draw_control(neighbors, kind, (60, 76), selected=True)
                with self.assertRaises(AIError):
                    locate_control(neighbors, 60, 58, kind)

    def test_precise_point_in_a_dense_table_keeps_its_own_control(self):
        draw_control(self.image, "radio", (60, 40), size=16)
        draw_control(self.image, "radio", (60, 62), size=16)
        _, x, y = locate_control(self.image, 59, 40, "radio")
        self.assertEqual((x, y), (60, 40))
        with self.assertRaises(AIError):
            locate_control(self.image, 60, 51, "radio")

    def test_entire_plan_is_grounded_and_duplicate_resolutions_are_rejected(self):
        draw_control(self.image, "checkbox", (60, 40))
        draw_control(self.image, "checkbox", (60, 100))
        plan = {"action": "select", "selection_type": "multiple_answer", "coordinate_space": "pixels",
                "selections": [{"option": "A", "click_x": 47, "click_y": 37},
                               {"option": "C", "click_x": 48, "click_y": 98}],
                "confidence": .95, "explanation": "Both options are correct."}
        original = json.dumps(plan)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "question.png"
            self.image.save(path)
            grounded = ground_decision(path, plan)
            self.assertEqual([(p["click_x"], p["click_y"]) for p in grounded["selections"]], [(60, 40), (60, 100)])
            self.assertTrue(all(p["target_control"]["kind"] == "checkbox" for p in grounded["selections"]))
            self.assertEqual(json.dumps(plan), original)
            plan["selections"][1].update(click_x=61, click_y=42)
            with self.assertRaises(AIError) as caught:
                ground_decision(path, plan)
            self.assertEqual(caught.exception.code, "browser_grounding")

    def test_selection_requires_filled_interior_and_preserved_control(self):
        for kind in ("radio", "checkbox"):
            with self.subTest(kind=kind):
                before = self.image.copy()
                draw_control(before, kind)
                control, _, _ = locate_control(before, 60, 60, kind)
                selected = before.copy()
                draw_control(selected, kind, selected=True)
                self.assertTrue(selection_visible(before, selected, control))
                self.assertFalse(selection_visible(before, before, control))
                hover = before.copy()
                draw = ImageDraw.Draw(hover)
                draw.rectangle((46, 46, 74, 74), outline="blue", width=2)
                self.assertFalse(selection_visible(before, hover, control))
                self.assertFalse(selection_visible(before, Image.new("RGB", before.size, "black"), control))

    def test_catalog_labels_unchecked_controls_and_discards_large_backgrounds(self):
        image = Image.new("RGB", (360, 240), "white")
        ImageDraw.Draw(image).rectangle((0, 0, 359, 35), fill="#222")
        draw_control(image, "radio", (60, 80))
        draw_control(image, "checkbox", (200, 80))
        draw_control(image, "radio", (60, 160), selected=True)
        for candidate in (image, Image.eval(image, lambda value: 255 - value)):
            controls = catalog_controls(candidate)
            self.assertEqual(controls, {"01": {"kind": "radio", "box": [49, 69, 71, 91]},
                                        "02": {"kind": "checkbox", "box": [189, 69, 211, 91]}})

    def test_annotated_input_preserves_the_source_and_removes_its_private_copy(self):
        draw_control(self.image, "radio")
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.png"
            self.image.save(source)
            original = source.read_bytes()
            with annotated_controls(source) as (upload, controls):
                self.assertEqual(set(controls), {"01"})
                self.assertEqual(upload.stat().st_mode & 0o777, 0o600)
                with Image.open(upload) as annotated:
                    self.assertEqual(annotated.size, self.image.size)
                    self.assertNotEqual(annotated.tobytes(), self.image.tobytes())
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(upload.exists())

    def test_scroll_reference_controls_are_unbadged_and_never_become_live_targets(self):
        image = Image.new("RGB", (360, 240), "white")
        draw_control(image, "radio", (60, 80))
        draw_control(image, "radio", (200, 160))
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.png"
            image.save(source)
            context = [{"path": source, "box": (0, 40, 120, 120), "label": "Earlier context"}]
            with annotated_controls(source, context_images=context, regions=[(150, 120, 300, 200)]) as (upload, controls):
                self.assertEqual(controls, {"01": {"kind": "radio", "box": [189, 149, 211, 171]}})
                with Image.open(upload) as packed:
                    self.assertGreater(packed.height, 240)
                    self.assertEqual(packed.crop((0, 32, 120, 112)).tobytes(), image.crop((0, 40, 120, 120)).tobytes())
                    self.assertEqual(packed.getpixel((60, packed.height - 240 + 80)), image.getpixel((60, 80)))
                self.assertEqual(upload.stat().st_mode & 0o777, 0o600)
            self.assertFalse(upload.exists())

    def test_invalid_scroll_crop_or_frame_dimensions_reject_the_whole_pack(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.png"
            other = Path(root) / "other.png"
            self.image.save(source)
            Image.new("RGB", (5, 5), "white").save(other)
            for path, box in ((source, (-1, 0, 40, 40)), (source, (0, 0, True, 40)),
                              (other, (0, 0, 40, 40))):
                with self.subTest(path=path, box=box), self.assertRaises(AIError):
                    with annotated_controls(source, context_images=[{"path": path, "box": box}]):
                        self.fail("Invalid context should never be uploaded")

    def test_jpeg_live_and_reference_images_pack_as_png_without_changing_original_pixels(self):
        draw_control(self.image,'radio')
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'question.jpg'
            self.image.save(source,format='JPEG',quality=100,subsampling=0)
            original=source.read_bytes()
            with annotated_controls(source,context_images=[{'path':source,'box':(0,0,100,35)}]) as (upload,controls):
                with Image.open(upload) as packed:
                    self.assertEqual(packed.format,'PNG')
                    self.assertGreater(packed.height,self.image.height)
                decision=DecisionClient().parse_coordinates(
                    '{"action":"click","target_id":"01","confidence":0.95,"explanation":"A"}',
                    *self.image.size,control_targets=controls)
                self.assertEqual((decision['click_x'],decision['click_y']),(60,60))
            self.assertEqual(source.read_bytes(),original)

    def test_ids_resolve_single_classification_and_multiple_answer_plans(self):
        controls = {"01": {"kind": "radio", "box": [49, 29, 71, 51]},
                    "02": {"kind": "checkbox", "box": [49, 89, 71, 111]},
                    "03": {"kind": "radio", "box": [99, 89, 121, 111]}}
        common = {"confidence": .95, "explanation": "Choose the correct controls."}
        decisions = [common | {"action": "click", "target_id": "01"},
                     common | {"action": "select", "selection_type": "classification", "selections": [
                         {"row": 2, "choice": "True", "target_id": "03"},
                         {"row": 1, "choice": "False", "target_id": "01"}]},
                     common | {"action": "select", "selection_type": "multiple_answer", "selections": [
                         {"option": "B", "target_id": "02"}]}]
        client = DecisionClient()
        for decision in decisions:
            with self.subTest(action=decision["action"], kind=decision.get("selection_type")):
                result = client.parse_coordinates(json.dumps(decision), 160, 140, control_targets=controls)
                points = result.get("selections", [result])
                for point in points:
                    control = controls[point["target_id"]]
                    self.assertEqual(point["target_control"], control)
                    self.assertEqual(point["click_x"], round((control["box"][0] + control["box"][2] - 1) / 2))
                # The normal engine parser consumes the locally resolved plan.
                self.assertEqual(client.parse_coordinates(json.dumps(result), 160, 140), result)
        prompt = build_control_prompt(160, 140, controls)
        for cue in ("MCMA", "jawaban benar lebih dari satu", "classification", "target_id", "leading zeros"):
            self.assertIn(cue, prompt)

    def test_unknown_missing_wrong_type_and_duplicate_ids_reject_the_whole_plan(self):
        controls = {"01": {"kind": "radio", "box": [49, 29, 71, 51]},
                    "02": {"kind": "checkbox", "box": [49, 89, 71, 111]}}
        common = {"action": "click", "confidence": .95, "explanation": "Answer."}
        invalid = [common | {"target_id": identifier} for identifier in ("99", "1", 1, None, "02")]
        invalid.append(common | {"click_x": 60, "click_y": 40, "coordinate_space": "pixels"})
        invalid.append(common | {"action": "select", "selection_type": "classification", "selections": [
            {"row": 1, "choice": "True", "target_id": "01"},
            {"row": 2, "choice": "False", "target_id": "01"}]})
        client = DecisionClient()
        for decision in invalid:
            with self.subTest(decision=decision), self.assertRaises(AIError):
                client.parse_coordinates(json.dumps(decision), 160, 140, control_targets=controls)
        self.assertIsNone(client.parse_coordinates('{"action":"none"}', 160, 140, control_targets={}))


class SelectionCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        before = Image.new("RGB", (160, 140), "white")
        draw_control(before, "radio")
        control, x, y = locate_control(before, 60, 60, "radio")
        self.target = {"click_x": x, "click_y": y, "target_control": control}
        after = before.copy()
        draw_control(after, "radio", selected=True)
        before.save(root / "before.png")
        after.save(root / "after.png")
        display = GuestDisplay(**(TEST_DISPLAY | {"width": 160, "height": 140}))
        self.before = Frame(root / "before.png", display, time.monotonic())
        self.after = Frame(root / "after.png", display, time.monotonic())
        self.capture = SethCapture({"project_dir": str(root)}, backend=Mock())

    def test_clears_hover_once_and_waits_for_selection_without_another_click(self):
        with patch.object(self.capture, "capture_frame", side_effect=[self.before, self.after]) as capture:
            self.capture.verify_selection(self.before, self.target)
        self.assertEqual(capture.call_count, 2)
        self.capture.backend.park_pointer.assert_called_once_with(self.before, 150, None)
        self.capture.backend.click.assert_not_called()

    def test_unchanged_control_times_out_and_cancellation_prevents_capture(self):
        with patch.object(self.capture, "capture_frame", return_value=self.before), \
                patch("capture.time.monotonic", side_effect=[0, 2]), self.assertRaises(VMError):
            self.capture.verify_selection(self.before, self.target)
        event = threading.Event()
        event.set()
        self.capture.backend.reset_mock()
        with patch.object(self.capture, "capture_frame") as capture, self.assertRaises(VMError):
            self.capture.verify_selection(self.before, self.target, event)
        capture.assert_not_called()
        self.capture.backend.park_pointer.assert_not_called()

    def test_real_table_selection_is_confirmed_after_parking_clears_row_hover(self):
        fixtures = Path(__file__).parent / 'fixtures/controls'
        paths = [fixtures / name for name in ('unchecked-radio-row.png', 'selected-radio-row-hover.png',
                                               'selected-radio-row.png')]
        with Image.open(paths[0]) as before, Image.open(paths[1]) as hovered, Image.open(paths[2]) as cleared:
            control, x, y = locate_control(before, 20, 20, 'radio')
            self.assertFalse(selection_visible(before, hovered, control))
            self.assertTrue(selection_visible(before, cleared, control))
        display = GuestDisplay(**(TEST_DISPLAY | {'width': 40, 'height': 40}))
        frames = [Frame(path, display, time.monotonic()) for path in paths]
        hover_active = True
        def park(*args):
            nonlocal hover_active
            hover_active = False
        self.capture.backend.park_pointer.side_effect = park
        with patch.object(self.capture, 'capture_frame', side_effect=lambda: frames[1 if hover_active else 2]) as capture:
            self.capture.verify_selection(frames[0], {'click_x': x, 'click_y': y, 'target_control': control})
        self.capture.backend.park_pointer.assert_called_once()
        self.capture.backend.click.assert_not_called()
        self.assertEqual(capture.call_count, 1)


if __name__ == "__main__":
    unittest.main()
