"""Interrupt a run without stopping the listener or allowing late selections."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from ai_client import SethAIClient
from cancellation import CycleCancellation
from capture import Frame
from inference import AIError
from main import SethEngine
from tests.fake_vbox import TEST_DISPLAY
from tests.test_runtime_selection import CONFIG, DECISION
from tests.test_seth import CLASSIFICATION
from vbox_backend import GuestDisplay, VMError


class InterruptTests(unittest.TestCase):
    def setUp(self):
        self.engine = SethEngine(CONFIG, execute=True, manual_selection=True)
        self.frame = Frame(Path('dummy.png'), GuestDisplay(**TEST_DISPLAY), time.monotonic())
        # These tests isolate cycle interruption; Single content identity and
        # control grounding have dedicated image fixtures elsewhere.
        question = SimpleNamespace(frame=self.frame, regions=None, verify=Mock(return_value=self.frame), confirm=Mock())
        for mocker in (patch('main.prepare_single', return_value=question),
                       patch('main.ground_decision', side_effect=lambda path, decision, **kwargs: decision),
                       patch.object(self.engine.capture, 'verify_selection')):
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_interrupt_during_each_provider_returns_idle_and_next_request_works(self):
        for provider in ('api', 'gemini', 'qwen'):
            with self.subTest(provider=provider):
                engine = SethEngine(CONFIG, execute=True, manual_selection=True, initial_provider=provider)
                entered = threading.Event()
                def answer(*args, **kwargs):
                    entered.set()
                    self.assertTrue(engine.ai.stop_event.wait(2))
                    # A provider finishing at the same time must not act on its answer.
                    return json.dumps(DECISION)
                with patch.object(engine.ai, 'validate_config'), \
                        patch.object(engine.capture, 'capture_frame', return_value=self.frame), \
                        patch.object(engine.ai, 'analyze_screenshot', side_effect=answer) as analyze, \
                        patch.object(engine.ai, 'parse_coordinates', return_value=DECISION) as parse, \
                        patch.object(engine.capture, 'verify_target'), \
                        patch.object(engine.capture, 'verify_selection'), \
                        patch.object(engine.clicker, 'click', return_value={}) as click:
                    try:
                        self.assertTrue(engine.request_cycle())
                        self.assertTrue(entered.wait(2))
                        self.assertTrue(engine.interrupt_cycle())
                        engine._worker.join(2)
                        self.assertFalse(engine._worker.is_alive())
                        self.assertEqual(engine.progress, ('idle', 'Idle'))
                        self.assertFalse(engine.stop_event.is_set())
                        self.assertEqual(engine.selected_provider, provider)
                        click.assert_not_called()
                        parse.assert_not_called()
                        analyze.side_effect = None
                        analyze.return_value = json.dumps(DECISION)
                        self.assertTrue(engine.request_cycle())
                        engine._worker.join(2)
                        self.assertFalse(engine._worker.is_alive())
                        click.assert_called_once()
                        self.assertTrue(engine.cycle_provider())
                        self.assertTrue(engine.toggle_capture())
                    finally:
                        engine.stop_event.set()
                        engine.wait()

    def test_capture_interrupt_stops_before_layout_or_answer_request(self):
        def capture():
            self.assertTrue(self.engine.interrupt_cycle())
            return self.frame
        with patch.object(self.engine.capture, 'capture_frame', side_effect=capture), \
                patch.object(self.engine.ai, 'analyze_screenshot') as analyze, \
                patch.object(self.engine.clicker, 'click') as click:
            result = self.engine.run_cycle()
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.engine.progress, ('idle', 'Idle'))
        analyze.assert_not_called()
        click.assert_not_called()

    def test_interrupt_during_scroll_unwinds_before_rearming(self):
        self.engine.toggle_capture()
        entered, release = threading.Event(), threading.Event()
        def collect(*args, **kwargs):
            entered.set()
            self.assertTrue(self.engine.cancel_event.wait(2))
            release.wait(2)
            raise VMError('Cycle cancelled while scrolling')
        with patch.object(self.engine.capture, 'capture_frame', return_value=self.frame), \
                patch('scroll_capture.ScrollCollector') as collector, \
                patch.object(self.engine.ai, 'analyze_screenshot') as analyze, \
                patch.object(self.engine.clicker, 'click') as click:
            collector.return_value.collect.side_effect = collect
            try:
                self.assertTrue(self.engine.request_cycle())
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.engine.interrupt_cycle())
                self.assertFalse(self.engine.request_cycle())
                self.assertFalse(self.engine.cycle_provider())
                self.assertTrue(self.engine.interrupt_cycle())
                release.set()
                self.engine._worker.join(2)
                self.assertFalse(self.engine._worker.is_alive())
                self.assertEqual(self.engine.progress, ('idle', 'Idle'))
                self.assertEqual(self.engine.indicator_selection, 'API · Scroll')
                analyze.assert_not_called()
                click.assert_not_called()
            finally:
                release.set()
                self.engine.stop_event.set()
                self.engine.wait()

    def test_interrupt_between_selections_keeps_completed_clicks_and_returns_idle(self):
        def clicked(*args):
            self.engine.interrupt_cycle()
            return {'click_x': 150, 'click_y': 60}
        with patch.object(self.engine.capture, 'capture_frame', return_value=self.frame), \
                patch.object(self.engine.ai, 'analyze_screenshot', return_value=json.dumps(CLASSIFICATION)), \
                patch.object(self.engine.capture, 'verify_target'), \
                patch.object(self.engine.clicker, 'click', side_effect=clicked) as click:
            result = self.engine.run_cycle()
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['clicks_completed'], 1)
        self.assertEqual(self.engine.progress, ('idle', 'Idle'))
        click.assert_called_once()
        self.assertFalse(self.engine.stop_event.is_set())

    def test_idle_interrupt_does_not_poison_next_run_or_reset_selection(self):
        self.engine.cycle_provider()
        self.engine.toggle_capture()
        self.engine.progress = ('error', 'Error')
        self.assertFalse(self.engine.interrupt_cycle())
        self.assertEqual(self.engine.progress, ('idle', 'Idle'))
        self.assertEqual(self.engine.indicator_selection, 'Gemini · Scroll')
        self.assertFalse(self.engine.cancel_event.is_set())

    def test_shutdown_cannot_be_cleared_by_interrupt_or_a_new_cycle(self):
        self.engine.stop_event.set()
        self.engine.interrupt_cycle()
        self.assertFalse(self.engine.request_cycle())
        self.assertEqual(self.engine.run_cycle()['status'], 'cancelled')
        self.assertTrue(self.engine.cancel_event.is_set())
        self.assertFalse(self.engine.cycle_provider())

    def test_engine_interrupt_and_shutdown_publish_before_returning(self):
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                engine = SethEngine(CONFIG, execute=True, manual_selection=True)
                published = []
                unregister = engine.cancel_event.register(lambda: published.append(True))
                engine._cycle_lock.acquire()
                engine._begin_cycle()
                try:
                    if shutdown:
                        engine.stop_event.set()
                    else:
                        self.assertTrue(engine.interrupt_cycle())
                    self.assertTrue(published)
                finally:
                    unregister()
                    engine._cycle_active = False
                    engine._cycle_lock.release()
                self.assertFalse(engine.stop_event._callbacks)
                self.assertFalse(engine.cancel_event.interrupted._callbacks)

    def test_wait_observes_interrupt_and_shutdown(self):
        for shutdown in (False, True):
            stop = threading.Event()
            event = CycleCancellation(stop)
            timer = threading.Timer(.02, stop.set if shutdown else event.set)
            timer.start()
            try:
                started = time.monotonic()
                self.assertTrue(event.wait(2))
                self.assertLess(time.monotonic() - started, .5)
                event.clear()
                self.assertEqual(event.is_set(), shutdown)
            finally:
                timer.join()


class CancellableAPITests(unittest.TestCase):
    def setUp(self):
        self.arrived, self.release = threading.Event(), threading.Event()
        self.mode = 'blocked'
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                try:
                    if owner.mode == 'blocked':
                        owner.arrived.set()
                        owner.release.wait(5)
                    if request['stream']:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream')
                        self.end_headers()
                        if owner.mode == 'success':
                            event = {'choices': [{'delta': {'content': json.dumps(DECISION)}, 'finish_reason': 'stop'}]}
                            self.wfile.write(('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode())
                        else:
                            self.wfile.write(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
                            self.wfile.flush()
                            owner.arrived.set()
                            owner.release.wait(5)
                    else:
                        self.send_response(530 if owner.mode == 'error' else 200)
                        self.end_headers()
                        self.wfile.write(json.dumps({'choices': [{'message': {'content': json.dumps(DECISION)}, 'finish_reason': 'stop'}]}).encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.root = tempfile.TemporaryDirectory()
        self.image = Path(self.root.name) / 'capture.png'
        Image.new('RGB', (320, 200), 'white').save(self.image)
        self.stop = threading.Event()
        self.client = SethAIClient({'api': {'base_url': f'http://127.0.0.1:{self.server.server_port}/v1',
                                           'api_key': 'local-test-key', 'model': 'test', 'timeout_seconds': 30}},
                                   stop_event=self.stop)
        self.children = {child.pid for child in multiprocessing.active_children()}

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.root.cleanup()

    def test_cancel_blocked_response_and_stream_reaps_helper_then_next_request_succeeds(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                self.mode = 'stream' if streaming else 'blocked'
                self.client.api['stream'] = streaming
                self.arrived.clear()
                self.release.clear()
                self.stop.clear()
                errors = []
                def request():
                    try:
                        self.client.analyze_screenshot(self.image, prompt='Test cancellation transport')
                    except AIError as error:
                        errors.append(error.code)
                thread = threading.Thread(target=request)
                thread.start()
                try:
                    self.assertTrue(self.arrived.wait(5))
                    started = time.monotonic()
                    self.stop.set()
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                    self.assertLess(time.monotonic() - started, 1.5)
                    self.assertEqual(errors, ['cancelled'])
                    self.assertEqual({child.pid for child in multiprocessing.active_children()}, self.children)
                finally:
                    self.release.set()
                    self.stop.set()
                    thread.join(5)
                self.stop.clear()
                self.mode = 'success'
                self.client.api['stream'] = streaming
                self.assertEqual(json.loads(self.client.analyze_screenshot(self.image, prompt='Test cancellation transport')), DECISION)

    def test_error_code_survives_worker_and_already_cancelled_request_never_posts(self):
        self.mode = 'error'
        with self.assertRaises(AIError) as caught:
            self.client.analyze_screenshot(self.image, prompt='Test cancellation transport')
        self.assertEqual(caught.exception.code, 'gateway_unavailable')
        self.stop.set()
        self.mode = 'blocked'
        with self.assertRaises(AIError) as caught:
            self.client.analyze_screenshot(self.image, prompt='Test cancellation transport')
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertFalse(self.arrived.is_set())


if __name__ == '__main__':
    unittest.main()
