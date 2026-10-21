"""Cancellation stops blocked HTTP and partial streams and reaps the helper."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import unittest

from PIL import Image

from ai_client import SethAIClient
from inference import AIError
from tests.test_seth import DECISION


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
                        self.client.analyze_screenshot(self.image, 'mc')
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
                self.assertEqual(json.loads(self.client.analyze_screenshot(self.image, 'mc')), DECISION)

    def test_error_code_survives_worker_and_already_cancelled_request_never_posts(self):
        self.mode = 'error'
        with self.assertRaises(AIError) as caught:
            self.client.analyze_screenshot(self.image, 'mc')
        self.assertEqual(caught.exception.code, 'gateway_unavailable')
        self.stop.set()
        self.mode = 'blocked'
        with self.assertRaises(AIError) as caught:
            self.client.analyze_screenshot(self.image, 'mc')
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertFalse(self.arrived.is_set())


if __name__ == '__main__':
    unittest.main()
