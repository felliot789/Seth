"""The optional login helper captures only the requested authenticated session."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_inference import SITES, WebInferenceClient
from web_login import _capture, login, provider_cookies


class LoginCaptureTests(unittest.TestCase):
    def test_cookie_domains_are_exact_or_subdomains(self):
        cookies = [{"name": "SID", "value": "secret", "domain": domain} for domain in (
            ".google.com", "accounts.google.com", "evilgoogle.com", "google.com.evil.example", ".chatgpt.com")]
        self.assertEqual([c["domain"] for c in provider_cookies("gemini", cookies)],
                         [".google.com", "accounts.google.com"])

    def capture(self, cookies, state):
        connection = MagicMock()
        connection.recv.side_effect = [
            json.dumps({"id": 1, "result": {"cookies": cookies}}),
            json.dumps({"id": 2, "result": {"result": {"value": state}}})]
        with patch("websocket.create_connection", return_value=connection):
            result = _capture({"webSocketDebuggerUrl": "ws://127.0.0.1:1234/test"},
                              "gemini", "#model", "#editor")
        connection.close.assert_called_once()
        return result

    def test_non_authenticated_google_cookies_are_insufficient(self):
        self.assertIsNone(self.capture([{"name": "NID", "value": "test", "domain": ".google.com"}],
                                       {"login": False, "editor": True, "model": "Pro"}))

    def test_sign_in_or_missing_composer_prevents_capture(self):
        cookies = [{"name": "SID", "value": "test", "domain": ".google.com"}]
        for state in ({"login": True, "editor": True}, {"login": False, "editor": False}):
            with self.subTest(state=state):
                self.assertIsNone(self.capture(cookies, state))

    def test_authenticated_session_and_model_are_captured(self):
        cookies = [{"name": "SID", "value": "test", "domain": ".google.com"},
                   {"name": "unrelated", "value": "other", "domain": ".unrelated.example"}]
        captured, model = self.capture(cookies, {"login": False, "editor": True, "model": "Pro"})
        self.assertEqual(captured, cookies[:1])
        self.assertEqual(model, "Pro")


@unittest.skipUnless(os.environ.get("SETH_LOGIN_TESTS") == "1", "Set SETH_LOGIN_TESTS=1 for the optional native login helper")
class LoginHelperIntegrationTests(unittest.TestCase):
    def test_real_browser_saves_session_and_closes_after_model_selection(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Set-Cookie", "SID=local-login-test; HttpOnly; Path=/")
                self.end_headers()
                self.wfile.write(b'<div id="model"><button aria-label="Open mode picker, currently Gemini Argon">'
                                 b'<span>Gemini</span><span>Argon</span></button></div>'
                                 b'<div id="editor" role="textbox" contenteditable="true"></div>')
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                config = {"web": {"state_dir": temporary, "sites": {"gemini": {
                    "model_label": "Argon", "selectors": {"model": "#model", "editor": "#editor"}}}}}
                client = WebInferenceClient(config)
                site = SITES["gemini"] | {"url": f"http://127.0.0.1:{server.server_port}/",
                                           "cookie_domains": ("127.0.0.1",)}
                with patch.dict(SITES, {"gemini": site}), patch("web_login.Path.home", return_value=Path(temporary)):
                    result = login(client, "gemini", timeout=15, headless=True)
                self.assertEqual(result["status"], "session_imported")
                self.assertEqual(result["model_label"], "GeminiArgon Open mode picker, currently Gemini Argon")
                saved = json.loads(client.cookie_file("gemini").read_text())
                self.assertTrue(any(c["name"] == "SID" and c["value"] == "local-login-test" for c in saved))
                self.assertEqual(client.cookie_file("gemini").stat().st_mode & 0o777, 0o600)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
