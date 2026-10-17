"""Manual inference/capture selection, without network requests or real VM input."""

import copy
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ai_client import SethAIClient
from capture import Frame
from main import SethEngine
from scroll_capture import ScrollLimitError
from tests.fake_vbox import TEST_DISPLAY
from vbox_backend import GuestDisplay, VMError
from web_inference import WebInferenceClient


DECISION = {'action': 'click', 'coordinate_space': 'pixels', 'click_x': 150, 'click_y': 100,
            'confidence': .95, 'explanation': 'B'}
CONFIG = {'inference': {'backend': 'web'},
          'api': {'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'local-test-key', 'model': 'test-vision'},
          'capture': {'max_age_seconds': 90, 'scroll_context': {'enabled': True, 'regions': [[.5, .28, .95, .86]]}},
          'web': {'providers': ['qwen', 'gemini'], 'sites': {'gemini': {'model_label': 'Argon'},
                                                          'qwen': {'model_label': 'Qwen3.8-Omni-Flash'}}}}


class RuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(CONFIG)
        self.engine = SethEngine(self.config, manual_selection=True)
        self.frame = Frame(Path('dummy.png'), GuestDisplay(**TEST_DISPLAY), time.monotonic())

    def test_startup_is_api_single_and_switching_never_starts_work_or_mutates_configuration(self):
        original = copy.deepcopy(self.config)
        self.assertIsInstance(self.engine.ai, SethAIClient)
        self.assertEqual(self.engine.indicator_selection, 'API · Single')
        with patch('ai_client.requests.post') as post, patch.object(WebInferenceClient, '_run') as browser, \
                patch.object(self.engine.capture, 'capture_frame') as capture:
            for expected in ('gemini', 'qwen', 'api'):
                self.assertTrue(self.engine.cycle_provider())
                self.assertEqual(self.engine.selected_provider, expected)
                if expected != 'api':
                    self.assertEqual(self.engine.ai.web['providers'], [expected])
                self.assertTrue(self.engine.toggle_capture())
                self.assertTrue(self.engine.scroll_enabled)
                self.assertTrue(self.engine.toggle_capture())
                self.assertFalse(self.engine.scroll_enabled)
            post.assert_not_called()
            browser.assert_not_called()
            capture.assert_not_called()
        self.assertEqual(self.config, original)

    def test_invalid_api_configuration_does_not_block_selecting_web(self):
        engine = SethEngine({'api': {}, 'web': CONFIG['web']}, manual_selection=True)
        self.assertEqual(engine.selected_provider, 'api')
        self.assertTrue(engine.cycle_provider())
        self.assertEqual(engine.selected_provider, 'gemini')

    def test_explicit_startup_override_is_supported_and_invalid_provider_is_rejected(self):
        engine = SethEngine(self.config, manual_selection=True, initial_provider='gemini')
        self.assertEqual(engine.indicator_selection, 'Gemini · Single')
        self.assertEqual(engine.ai.web['providers'], ['gemini'])
        with self.assertRaises(ValueError):
            SethEngine(self.config, manual_selection=True, initial_provider='unknown')

    def test_uncertainty_does_not_change_the_selected_provider_or_start_another_request(self):
        initial = self.engine.ai
        with patch.object(self.engine.capture, 'capture_frame', return_value=self.frame), \
                patch.object(initial, 'analyze_screenshot', return_value=json.dumps(DECISION | {'confidence': .8})) as analyze, \
                patch.object(WebInferenceClient, '_run') as browser:
            result = self.engine.run_cycle()
        self.assertEqual(result['status'], 'low_confidence')
        self.assertEqual(self.engine.selected_provider, 'api')
        self.assertIs(self.engine.ai, initial)
        analyze.assert_called_once()
        browser.assert_not_called()

    def test_capture_stays_selected_between_cycles_and_resets_with_a_new_listener(self):
        self.engine.toggle_capture()
        bundle = Mock(frame=self.frame, contexts=[{'path': 'context.png'}], regions=[])
        with patch.object(self.engine.capture, 'capture_frame', return_value=self.frame), \
                patch.object(self.engine.ai, 'analyze_screenshot', return_value=json.dumps(DECISION)) as analyze, \
                patch('scroll_capture.ScrollCollector') as collector:
            collector.return_value.collect.return_value = bundle
            for _ in range(2):
                self.assertEqual(self.engine.run_cycle()['status'], 'dry_run')
                self.assertTrue(self.engine.scroll_enabled)
            self.assertEqual(collector.return_value.collect.call_count, 2)
            self.assertEqual(analyze.call_args.kwargs['context_images'], bundle.contexts)
            self.engine.toggle_capture()
            collector.reset_mock()
            self.engine.run_cycle()
            collector.assert_not_called()
            self.assertEqual(analyze.call_args.args, (self.frame.path,))
            self.assertEqual(analyze.call_args.kwargs, {})
        fresh = SethEngine(self.config, manual_selection=True)
        self.assertEqual(fresh.indicator_selection, 'API · Single')

    def test_provider_and_capture_switches_are_rejected_until_the_running_cycle_finishes(self):
        started, release = threading.Event(), threading.Event()
        initial_client = self.engine.ai
        def analyze(*args, **kwargs):
            started.set()
            release.wait(3)
            return json.dumps(DECISION)
        with patch.object(self.engine.capture, 'capture_frame', return_value=self.frame), \
                patch.object(initial_client, 'analyze_screenshot', side_effect=analyze):
            try:
                self.assertTrue(self.engine.request_cycle())
                self.assertTrue(started.wait(2))
                self.assertFalse(self.engine.cycle_provider())
                self.assertFalse(self.engine.toggle_capture())
                self.assertFalse(self.engine.request_cycle())
                self.assertIs(self.engine.ai, initial_client)
                self.assertEqual(self.engine.indicator_selection, 'API · Single')
            finally:
                release.set()
                self.engine.wait()
        self.assertTrue(self.engine.cycle_provider())
        self.assertTrue(self.engine.toggle_capture())

    def test_scroll_limit_is_identified_before_inference_or_answer_input_for_every_provider(self):
        for provider in ('api', 'gemini', 'qwen'):
            with self.subTest(provider=provider):
                engine = SethEngine(self.config, execute=True, manual_selection=True, initial_provider=provider)
                engine.toggle_capture()
                with patch.object(engine.ai, 'validate_config'), \
                        patch.object(engine.capture, 'capture_frame', return_value=self.frame), \
                        patch.object(engine.ai, 'analyze_screenshot') as analyze, \
                        patch.object(engine.clicker, 'click') as click, \
                        patch('scroll_capture.ScrollCollector') as collector:
                    collector.return_value.collect.side_effect = ScrollLimitError(13, 12)
                    result = engine.run_cycle()
                self.assertEqual(result['error_code'], 'scroll_limit')
                self.assertEqual((result['required_views'], result['max_views']), (13, 12))
                self.assertEqual(result['inference_provider'], provider)
                self.assertEqual(engine.progress, ('error', 'Scroll limit'))
                analyze.assert_not_called()
                click.assert_not_called()

    def test_result_logging_is_inside_the_same_lock_as_inference(self):
        observed = []
        def record(message, *args):
            if message == 'Cycle finished: %s':
                observed.append(json.loads(args[0]))
                self.assertFalse(self.engine.cycle_provider())
                self.assertFalse(self.engine.toggle_capture())
        with patch.object(self.engine.capture, 'capture_frame', return_value=self.frame), \
                patch.object(self.engine.ai, 'analyze_screenshot', return_value=json.dumps(DECISION)), \
                patch('main.LOG.info', side_effect=record):
            result = self.engine.run_cycle()
        self.assertEqual(observed, [result])
        self.assertEqual(result['inference_provider'], 'api')
        self.assertEqual(result['capture_mode'], 'single')
        self.assertTrue(self.engine.cycle_provider())

    def test_selection_changes_preserve_the_last_result_status(self):
        self.engine.progress = ('low_confidence', 'Uncertain')
        self.engine.cycle_provider()
        self.engine.toggle_capture()
        self.assertEqual(self.engine.progress, ('low_confidence', 'Uncertain'))
        self.assertEqual(self.engine.indicator_selection, 'Gemini · Scroll')

    def test_switch_construction_failure_keeps_previous_client_and_releases_lock(self):
        original = self.engine.ai
        with patch('main.create_client', side_effect=RuntimeError('test constructor failure')):
            with self.assertRaises(RuntimeError):
                self.engine.cycle_provider()
        self.assertEqual(self.engine.selected_provider, 'api')
        self.assertIs(self.engine.ai, original)
        self.assertTrue(self.engine.cycle_provider())

    def test_stopping_the_listener_rejects_further_switches(self):
        self.engine.stop_event.set()
        self.assertFalse(self.engine.cycle_provider())
        self.assertFalse(self.engine.toggle_capture())
        self.assertEqual(self.engine.indicator_selection, 'API · Single')

    def test_context_grounded_api_and_gemini_stop_after_an_unconfirmed_selection(self):
        plan = {'action': 'select', 'selection_type': 'classification', 'coordinate_space': 'pixels',
                'confidence': .95, 'explanation': 'Classify both rows.', 'selections': [
                    {'row': 1, 'choice': 'True', 'click_x': 150, 'click_y': 60,
                     'target_control': {'kind': 'radio', 'box': [139, 49, 161, 71]}},
                    {'row': 2, 'choice': 'False', 'click_x': 150, 'click_y': 100,
                     'target_control': {'kind': 'radio', 'box': [139, 89, 161, 111]}}]}
        for provider in ('api', 'gemini'):
            with self.subTest(provider=provider):
                engine = SethEngine(self.config, execute=True, manual_selection=True)
                if provider == 'gemini':
                    engine.cycle_provider()
                engine.toggle_capture()
                bundle = Mock(frame=self.frame, contexts=[{'path': 'context.png'}], regions=[], selected_controls=[])
                engine.ai.last_grounded = True
                with patch.object(engine.ai, 'validate_config'), \
                        patch.object(engine.ai, 'analyze_screenshot', return_value=json.dumps(plan)), \
                        patch.object(engine.capture, 'capture_frame', return_value=self.frame), \
                        patch.object(engine.capture, 'verify_target'), \
                        patch.object(engine.capture, 'verify_selection', side_effect=VMError('Control stayed unchecked')), \
                        patch.object(engine.clicker, 'click', return_value={'click_x': 150, 'click_y': 60}) as click, \
                        patch('scroll_capture.ScrollCollector') as collector:
                    collector.return_value.collect.return_value = bundle
                    result = engine.run_cycle()
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['clicks_completed'], 1)
                self.assertEqual(result['decision']['confidence'], .95)
                self.assertEqual(result['inference_provider'], provider)
                self.assertEqual(result['capture_mode'], 'scroll')
                self.assertEqual(bundle.selected_controls, [])
                click.assert_called_once()
