"""Offline regressions for provider grounding and strict answer envelopes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from PIL import Image, ImageDraw
from ai_client import SethAIClient
from inference import AIError, DecisionClient


class InferenceAuditTests(unittest.TestCase):
    def setUp(self):
        self.client = SethAIClient({"api": {"base_url": "https://example.invalid/v1",
                                  "api_key": "fake", "model": "fake"}})
        self.answer = {"action": "click", "coordinate_space": "pixels", "click_x": 60, "click_y": 60, "confidence": .95,
                       "explanation": "Answer"}

    def test_nonstream_requires_success_when_reason_is_present_and_plain_text(self):
        for reason in ("length", "content_filter", "tool_calls", "function_call", None, "unknown"):
            with self.subTest(reason=reason):
                self.assert_completion_rejected({"finish_reason": reason,
                    "message": {"content": json.dumps(self.answer)}})
        for field in ("refusal", "tool_calls", "function_call"):
            with self.subTest(field=field):
                self.assert_completion_rejected({"finish_reason": "stop",
                    "message": {"content": json.dumps(self.answer), field: "nonplain"}})
        # Missing metadata remains supported for legacy compatible gateways.
        for metadata in ({}, {"finish_reason": "stop"}):
            response = Mock(status_code=200)
            response.json.return_value = {"choices": [metadata | {"message": {"content": json.dumps(self.answer)}}]}
            with patch("ai_client.requests.post", return_value=response):
                self.assertEqual(self.client._request({"stream": False}), json.dumps(self.answer))
            response.close.assert_called_once()

    def assert_completion_rejected(self, choice):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [choice]}
        with patch("ai_client.requests.post", return_value=response), self.assertRaises(AIError):
            self.client._request({"stream": False})
        response.close.assert_called_once()

    def test_action_schemas_reject_contradictory_plans_before_grounding(self):
        controls = {"01": {"kind": "radio", "box": [10, 10, 30, 30]}}
        for field, value in (("selection_type", "classification"), ("selections", [])):
            with self.subTest(field=field), self.assertRaises(AIError):
                DecisionClient().parse_coordinates(json.dumps(self.answer | {field: value}), 100, 100, control_targets=controls)
        plan = {"action": "select", "selection_type": "classification", "confidence": .95,
                "explanation": "Rows", "selections": [{"row": 1, "choice": "A", "target_id": "01"}]}
        for field in ("target_id", "click_x", "click_y", "target_control", "target_view", "model_point"):
            with self.subTest(field=field), self.assertRaises(AIError):
                DecisionClient().parse_coordinates(json.dumps(plan | {field: None}), 100, 100, control_targets=controls)

    def test_api_single_regions_exclude_outside_controls_from_ids(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "question.png"
            image = Image.new("RGB", (300, 200), "white")
            draw = ImageDraw.Draw(image)
            draw.ellipse((39, 49, 60, 70), outline="#aaa", width=2)
            draw.ellipse((239, 49, 260, 70), outline="#aaa", width=2)
            image.save(source)
            def exchange(upload, prompt, response_format=None):
                self.assertIn("coordinate_space", prompt)
                self.assertNotIn("target_id", prompt)
                return json.dumps(self.answer | {"click_x": 250})
            with patch.object(self.client, "_analyze_screenshot", side_effect=exchange):
                result = json.loads(self.client.analyze_screenshot(source, control_regions=[(200, 0, 300, 200)]))
            self.assertEqual((result["click_x"], result["click_y"]), (250, 60))

    def test_api_default_uses_original_coordinates_and_layout_custom_prompts_keep_original(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.png"
            image = Image.new("RGB", (160, 140), "white")
            ImageDraw.Draw(image).ellipse((49, 49, 70, 70), outline="#aaa", width=2)
            image.save(source)
            original = source.read_bytes()
            uploads = []
            def exchange(upload, prompt, response_format=None):
                uploads.append(upload)
                self.assertEqual(upload, source)
                self.assertIn("coordinate_space", prompt)
                self.assertNotIn("target_id", prompt)
                return json.dumps(self.answer)
            with patch.object(self.client, "_analyze_screenshot", side_effect=exchange) as request:
                decision = json.loads(self.client.analyze_screenshot(source))
            request.assert_called_once()
            self.assertEqual((decision["click_x"], decision["click_y"]), (60, 60))
            self.assertTrue(self.client.last_grounded)
            self.assertTrue(uploads[0].exists())
            self.assertEqual(source.read_bytes(), original)
            for operation in (self.client.analyze_screenshot, self.client.inspect_layout):
                with patch.object(self.client, "_analyze_screenshot", return_value="layout") as request:
                    self.assertEqual(operation(source, "Read layout only"), "layout")
                    self.assertEqual(request.call_args.args[:2], (source, "Read layout only"))
            with patch.object(self.client, "_analyze_screenshot", return_value=json.dumps(self.answer | {"click_x": 159, "click_y": 139})), self.assertRaises(AIError):
                self.client.analyze_screenshot(source)
            self.assertFalse(self.client.last_grounded)
