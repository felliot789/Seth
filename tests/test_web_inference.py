"""Offline Lightpanda tests: local HTTP only, no real provider or VM calls."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from ai_client import SethAIClient
from gemini_web import GeminiWebClient
from inference import AIError, build_prompt, create_client
from lightpanda_runtime import DEFAULT_BINARY, Lightpanda, write_private_json
from web_inference import SITES, SiteAdapter, WebInferenceClient


class WebClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "question.png"
        Image.new("RGB", (320, 200), "white").save(self.image)
        self.config = {"inference": {"backend": "web"}, "web": {
            "state_dir": str(self.root / "state"), "executable_path": sys.executable,
            "providers": ["gemini", "chatgpt", "qwen"],
            "timeout_seconds": 20, "attempt_timeout_seconds": 10,
            "sites": {provider: {"model_label": "Pro"} for provider in SITES}}}
        self.client = WebInferenceClient(self.config)
        for provider in self.config["web"]["providers"]:
            write_private_json(self.client.cookie_file(provider), [])

    def test_factory_and_gemini_alias(self):
        self.assertIsInstance(create_client({}), SethAIClient)
        self.assertIsInstance(create_client(self.config), WebInferenceClient)
        alias = create_client(self.config | {"inference": {"backend": "gemini_web"}})
        self.assertIsInstance(alias, GeminiWebClient)
        self.assertEqual(alias.web["providers"], ["gemini"])
        self.assertEqual(self.config["web"]["providers"], ["gemini", "chatgpt", "qwen"])
        with self.assertRaises(AIError):
            create_client({"inference": {"backend": "typo"}})

    def test_browser_validation_needs_no_api_credentials(self):
        self.client.validate_config()
        for field, value in (("timeout_seconds", 0), ("timeout_seconds", float("nan")),
                             ("attempt_timeout_seconds", 301), ("providers", []),
                             ("providers", ["gemini", "gemini"]), ("providers", ["unknown"])):
            with self.subTest(field=field, value=value), self.assertRaises(AIError):
                WebInferenceClient({"web": self.config["web"] | {field: value}}).validate_config()

    def test_fallback_is_ordered_and_each_provider_is_attempted_once(self):
        calls, deadlines = [], []
        raw = '{"action":"none"}'
        def run(provider, deadline, operation):
            calls.append(provider)
            deadlines.append(deadline)
            if provider == "gemini":
                raise AIError("Quota", "browser_limit")
            return raw
        with patch.object(self.client, "_run", side_effect=run):
            self.assertEqual(self.client.analyze_screenshot(self.image), raw)
        self.assertEqual(calls, ["gemini", "chatgpt"])
        self.assertEqual(self.client.last_provider, "chatgpt")
        self.assertEqual(self.client.last_attempts, [{"provider": "gemini", "error_code": "browser_limit"}])

    def test_invalid_answer_falls_back_without_exposing_it_to_clicking(self):
        with patch.object(self.client, "_run", side_effect=['{"action":"click"}', '{"action":"none"}']) as run:
            self.assertEqual(self.client.analyze_screenshot(self.image), '{"action":"none"}')
            self.assertEqual(run.call_count, 2)
        self.assertEqual(self.client.last_provider, "chatgpt")

    def test_total_deadline_is_shared_between_fallbacks(self):
        self.client.web.update(timeout_seconds=5, attempt_timeout_seconds=10)
        deadlines = []
        def run(provider, deadline, operation):
            deadlines.append(deadline)
            raise AIError("No response", "browser_error")
        with patch.object(self.client, "_run", side_effect=run), self.assertRaises(AIError):
            self.client.analyze_screenshot(self.image)
        self.assertEqual(len(deadlines), 3)
        self.assertEqual(len(set(deadlines)), 1)

    def test_missing_sessions_are_skipped_and_cancellation_stops_fallback(self):
        self.client.cookie_file("gemini").unlink()
        with patch.object(self.client, "_run", side_effect=AIError("Cancel", "cancelled")) as run:
            with self.assertRaises(AIError) as caught:
                self.client.analyze_screenshot(self.image)
            self.assertEqual(caught.exception.code, "cancelled")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], "chatgpt")

    def test_unconfigured_model_is_skipped_before_starting_browser(self):
        self.client.web["sites"]["gemini"]["model_label"] = ""
        with patch.object(self.client, "_run", return_value='{"action":"none"}') as run:
            self.client.analyze_screenshot(self.image)
        self.assertEqual(run.call_args.args[0], "chatgpt")
        self.assertEqual(self.client.last_attempts, [{"provider": "gemini", "error_code": "browser_model"}])

    def test_cookie_import_filters_domains_and_keeps_private_files(self):
        source = self.root / "export.json"
        source.write_text(json.dumps({"cookies": [
            {"name": "session", "value": "test-secret", "domain": ".google.com", "path": "/"},
            {"name": "unrelated", "value": "other", "domain": ".unrelated.example", "path": "/"}]}))
        result = self.client.import_cookies("gemini", source)
        destination = self.client.cookie_file("gemini")
        self.assertEqual(result["cookies_imported"], 1)
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertEqual(json.loads(destination.read_text())[0]["name"], "session")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(destination.parent.stat().st_mode & 0o777, 0o700)

    def test_netscape_cookie_import_and_profile_lock(self):
        source = self.root / "cookies.txt"
        source.write_text('# Netscape HTTP Cookie File\n#HttpOnly_.google.com\tTRUE\t/\tTRUE\t0\tsession\ttest\n')
        self.client.import_cookies("gemini", source)
        self.assertTrue(json.loads(self.client.cookie_file("gemini").read_text())[0]["httpOnly"])
        with self.client.session_lock("gemini"):
            with self.assertRaises(AIError) as caught:
                with self.client.session_lock("gemini"):
                    self.fail("Session lock acquired twice")
            self.assertEqual(caught.exception.code, "browser_busy")

    def test_invalid_cookie_export_preserves_previous_session(self):
        destination = self.client.cookie_file("gemini")
        before = destination.read_bytes()
        source = self.root / "bad.json"
        source.write_text('{"invalid":"sensitive-value"}')
        with self.assertRaises(AIError) as caught:
            self.client.import_cookies("gemini", source)
        self.assertNotIn("sensitive-value", str(caught.exception))
        self.assertEqual(before, destination.read_bytes())


FIXTURE = r'''<!doctype html><html><body>
<button id=model>Pro</button>
<div id=editor contenteditable=true role=textbox></div>
<input type=file id=file>
<button id=send disabled>Send</button>
<script>
const scenario=SCENARIO;
const answer=JSON.stringify({action:'click',coordinate_space:'normalized_1000',
 click_x:500,click_y:500,confidence:0.99,explanation:'Center'});
window.submissions=0;
if(scenario==='login')document.body.insertAdjacentHTML('beforeend','<button>Sign in</button>');
if(scenario==='wrong_model')document.querySelector('#model').textContent='Fast';
if(scenario==='stale')document.body.insertAdjacentHTML('beforeend','<div class=response>Old answer</div>');
document.querySelector('#file').onchange=async()=>{
 const file=document.querySelector('#file').files[0];
 const preview=document.createElement('div');preview.className='attachment';
 preview.textContent=scenario==='wrong_file'?'other.png':file.name;
 preview.insertAdjacentHTML('beforeend','<span class=uploading>Uploading</span>');
 document.body.append(preview);
 const result=await fetch('/upload',{method:'POST',body:file});
 if(!result.ok||scenario==='upload_error'){
   document.body.insertAdjacentHTML('beforeend','<div role=alert>Upload failed</div>');return;
 }
 setTimeout(()=>{if(scenario!=='stuck_upload'){
   preview.querySelector('.uploading').remove();document.querySelector('#send').disabled=false;
 }},120);
};
document.querySelector('#send').onclick=()=>{
 window.submissions++;
 const user=document.createElement('div');user.className='user';
 user.textContent=scenario==='wrong_turn'?'Unrelated request':document.querySelector('#editor').textContent;
 document.body.append(user);
 if(scenario==='quota'){
   document.body.insertAdjacentHTML('beforeend','<div role=alert>Usage limit reached</div>');return;
 }
 const response=document.createElement('div');response.className='response';
 response.innerHTML='<div class=markdown></div><button class=copy>Copy</button>';
 document.body.append(response);response.querySelector('.markdown').textContent=answer;
 const stop=document.createElement('button');stop.id='stop';document.body.append(stop);
 setTimeout(()=>{
   if(scenario==='generating')return;
   if(scenario==='interrupted'){response.querySelector('.copy').remove();stop.remove();return;}
   if(scenario==='code'){
      response.querySelector('.markdown').innerHTML='<pre><code></code></pre>';
      response.querySelector('code').textContent=answer;
   }
   if(scenario==='invalid')response.querySelector('.markdown').textContent='Here is your answer: '+answer;
   if(scenario==='second_turn')document.body.append(user.cloneNode(true));
   if(scenario==='model_changed')document.querySelector('#model').textContent='Fast';
   window.completed=true;stop.remove();
 },250);
};
</script></body></html>'''

TEST_SELECTORS = {
    "editor": "#editor", "model": "#model", "file": "#file", "upload": "#upload",
    "upload_item": "#upload-item", "attachment": ".attachment", "upload_busy": ".uploading",
    "send": "#send", "user": ".user", "response": ".response", "answer": ".markdown",
    "complete": ".copy", "stop": "#stop", "error": '[role="alert"]',
}


@unittest.skipUnless(os.environ.get("SETH_LIGHTPANDA_TESTS") == "1", "Set SETH_LIGHTPANDA_TESTS=1 for offline Lightpanda integration")
class LightpandaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uploads = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                scenario = self.path.strip("/") or "normal"
                html = FIXTURE.replace("SCENARIO", json.dumps(scenario)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Set-Cookie", "seth_test=restored; Path=/")
                self.end_headers()
                self.wfile.write(html)
            def do_POST(self):
                cls.uploads.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "question.png"
        Image.new("RGB", (320, 200), "white").save(self.image)
        self.stop = threading.Event()

    def browser(self, timeout=8, cookies=None):
        return Lightpanda(executable=os.environ.get("SETH_TEST_LIGHTPANDA", str(DEFAULT_BINARY)),
                          deadline=time.monotonic() + timeout, stop_event=self.stop, cookies=cookies)

    def adapter(self, scenario="normal"):
        adapter = SiteAdapter("gemini", {"selectors": TEST_SELECTORS})
        adapter.site = adapter.site | {"url": f"http://127.0.0.1:{self.server.server_port}/{scenario}"}
        return adapter

    def test_real_png_upload_and_completed_answer(self):
        for scenario in ("normal", "code"):
            with self.subTest(scenario=scenario), self.browser() as page:
                before = len(self.uploads)
                raw = self.adapter(scenario).exchange(page, self.image, build_prompt(320, 200), lambda label: None)
                self.assertTrue(page.evaluate("window.completed"))
                self.assertEqual(page.evaluate("window.submissions"), 1)
                self.assertEqual(len(self.uploads), before + 1)
                self.assertEqual(hashlib.sha256(self.uploads[-1]).digest(), hashlib.sha256(self.image.read_bytes()).digest())
                decision = WebInferenceClient({}).parse_coordinates(raw, 320, 200)
                self.assertEqual((decision["click_x"], decision["click_y"]), (160, 100))

    def test_login_model_and_restored_conversation_fail_before_upload(self):
        for scenario, code in (("login", "browser_login"), ("wrong_model", "browser_model"), ("stale", "browser_layout")):
            with self.subTest(scenario=scenario), self.browser() as page:
                before = len(self.uploads)
                with self.assertRaises(AIError) as caught:
                    self.adapter(scenario).exchange(page, self.image, "prompt", lambda label: None)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(page.evaluate("window.submissions"), 0)
                self.assertEqual(len(self.uploads), before)

    def test_failed_and_stalled_uploads_never_submit(self):
        for scenario, code in (("upload_error", "browser_error"), ("stuck_upload", "browser_timeout"), ("wrong_file", "browser_timeout")):
            with self.subTest(scenario=scenario), self.browser(timeout=1.5) as page:
                with self.assertRaises(AIError) as caught:
                    self.adapter(scenario).exchange(page, self.image, "prompt", lambda label: None)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(page.evaluate("window.submissions") if code != "browser_timeout" else 0, 0)

    def test_incomplete_response_never_returns_even_when_json_is_valid(self):
        for scenario in ("generating", "interrupted"):
            with self.subTest(scenario=scenario), self.browser(timeout=1.5) as page:
                with self.assertRaises(AIError) as caught:
                    self.adapter(scenario).exchange(page, self.image, "prompt", lambda label: None)
                self.assertEqual(caught.exception.code, "browser_timeout")

    def test_quota_wrong_turn_and_model_changes_are_not_retried(self):
        for scenario, code in (("quota", "browser_limit"), ("wrong_turn", "browser_layout"),
                               ("second_turn", "browser_layout"), ("model_changed", "browser_model")):
            with self.subTest(scenario=scenario), self.browser() as page:
                with self.assertRaises(AIError) as caught:
                    self.adapter(scenario).exchange(page, self.image, "prompt", lambda label: None)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(page.evaluate("window.submissions"), 1)

    def test_cookie_persistence_across_real_browser_restarts(self):
        cookies = self.root / "cookies.json"
        with self.browser() as page:
            page.navigate(self.adapter().site["url"])
            page.wait(lambda: page.evaluate('document.querySelector("#editor")!==null'))
            page.save_cookies(cookies)
        self.assertEqual(cookies.stat().st_mode & 0o777, 0o600)
        with self.browser(cookies=cookies) as page:
            page.navigate(self.adapter().site["url"])
            self.assertIn("seth_test=restored", page.wait(lambda: page.evaluate("document.cookie")))

    def test_cancellation_terminates_waiting_and_browser_process(self):
        runtime = self.browser()
        timer = threading.Timer(0.7, self.stop.set)
        with self.assertRaises(AIError) as caught:
            with runtime as page:
                timer.start()
                try:
                    self.adapter("generating").exchange(page, self.image, "prompt", lambda label: None)
                finally:
                    timer.cancel()
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertIsNone(runtime.process)


if __name__ == "__main__":
    unittest.main()
