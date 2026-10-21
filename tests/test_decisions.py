"""Display-key schema and the user's explicit numeric/L-R formats."""

import json
import unittest
from inference import AIError, build_prompt, parse_decision


class DecisionTests(unittest.TestCase):
    def decision(self, answer="2", **changes):
        return {"status": "answer", "confidence": .95, "answer": answer, **changes}

    def test_exact_requested_key_formats_and_normalization(self):
        for mode, raw, expected in (("mc", "2", "2"), ("mcma", "5,1, 3", "1, 3, 5"),
                                    ("table", "l, R, l, r, R", "L, R, L, R, R")):
            value = parse_decision(json.dumps(self.decision(raw)), mode)
            self.assertEqual(value["answer"], expected)
            self.assertIn("user selected", build_prompt(mode))
            self.assertNotIn("classify or change", build_prompt(mode))

    def test_wrong_format_partial_and_old_action_replies_are_rejected(self):
        for mode, answer in (("mc", "B"), ("mc", "1, 2"), ("mc", "0"), ("mcma", "1, 1"),
                             ("mcma", "A, C"), ("mcma", "1: correct"), ("table", "1: L, 2: R"),
                             ("table", "Benar, Salah"), ("table", "L, M"), ("mc", "")):
            with self.subTest(mode=mode, answer=answer), self.assertRaises(AIError):
                parse_decision(json.dumps(self.decision(answer)), mode)
        for raw in ('{"status":"answer"', '{"action":"click","click_x":20,"click_y":30}',
                    json.dumps(self.decision() | {"confidence": True})):
            with self.assertRaises(AIError):
                parse_decision(raw, "mc")

    def test_uncertain_and_no_action_have_no_keys(self):
        for status in ("uncertain", "no_action"):
            self.assertEqual(parse_decision(json.dumps(self.decision("", status=status)), "mc")["answer"], "")
            self.assertEqual(parse_decision(json.dumps({"status": status}), "mc")["answer"], "")
            self.assertEqual(parse_decision(json.dumps(self.decision("2", status=status)), "mc")["answer"], "")

    def test_unused_provider_metadata_is_never_part_of_the_feedback(self):
        value = self.decision() | {"explanation": "Provider metadata", "control_count": 6, "action": "click"}
        self.assertEqual(parse_decision(json.dumps(value), "mc"), self.decision())

    def test_reply_size_and_code_fence(self):
        decision = self.decision()
        self.assertEqual(parse_decision("```json\n" + json.dumps(decision) + "\n```", "mc"), decision)
        with self.assertRaises(AIError):
            parse_decision(json.dumps(self.decision("1" * 513)), "mc")
