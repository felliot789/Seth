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
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw
from ai_client import SethAIClient
from chromium_runtime import Chromium, close_managed_browser, find_chromium
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

    def test_shared_native_profile_keeps_provider_tabs_and_cookie_files_separate(self):
        client = WebInferenceClient({'web': self.config['web'] | {'providers':['gemini','qwen'],
            'sites': {'gemini': {'engine':'chromium','browser_profile':'qwen','model_label':'Argon'},
                      'qwen': {'engine':'chromium','model_label':'Qwen3.8-Omni-Flash'}}}})
        with patch('web_inference.find_chromium', return_value=sys.executable):
            client.validate_config()
        self.assertEqual(client.login_profile('gemini'), client.login_profile('qwen'))
        self.assertNotEqual(client.native_state_file('gemini'), client.native_state_file('qwen'))
        self.assertNotEqual(client.cookie_file('gemini'), client.cookie_file('qwen'))
        for value in ('../elsewhere', '', None, [], 1):
            client.web['sites']['gemini']['browser_profile'] = value
            with self.subTest(profile=value), self.assertRaises(AIError):
                client.validate_config()
        client.web['sites']['gemini'].update(browser_profile='qwen', engine='lightpanda')
        with self.assertRaises(AIError):
            client.validate_config()

    def test_only_configured_engines_need_installed_binaries(self):
        self.client.web["executable_path"] = "/nonexistent/lightpanda"
        self.client.web["sites"] = {"gemini": {"engine": "chromium", "model_label": "Argon"}}
        with patch("web_inference.find_chromium", return_value=sys.executable):
            self.client.validate_config()
        self.client.web["sites"]["gemini"]["engine"] = "unknown"
        with self.assertRaises(AIError):
            self.client.validate_config()

    def test_gemini_preview_renaming_stays_bound_to_the_original_filename(self):
        adapter = SiteAdapter("gemini")
        for label in ("question.png", "Preview question_456f82.png close attachment"):
            self.assertTrue(adapter.attachment_matches(label, self.image))
        for label in ("other.png", "otherquestion.png", "question.png.extra", "question_wrong.png", "question_zzzzzz.png"):
            self.assertFalse(adapter.attachment_matches(label, self.image))
        self.assertFalse(SiteAdapter("chatgpt").attachment_matches("question_456f82.png", self.image))

    def test_selected_model_and_thinking_level_must_both_match(self):
        adapter = SiteAdapter("gemini", {"model_label": "Argon", "thinking_label": "Medium"})
        adapter.verify_model({"modelCount": 1, "model": "ArgonMedium Open mode picker, currently Argon Medium"})
        for model in ("Argon High", "Flash Medium", "Argon MediumPlus"):
            with self.subTest(model=model), self.assertRaises(AIError) as caught:
                adapter.verify_model({"modelCount": 1, "model": model})
            self.assertEqual(caught.exception.code, "browser_model")

    def test_qwen_requires_one_matching_separate_thinking_control(self):
        adapter = SiteAdapter("qwen", {"model_label": "Qwen3.7-Plus", "thinking_label": "Thinking"})
        state = {"modelCount": 1, "model": "Qwen3.7-Plus", "thinkCount": 1, "thinking": "Thinking"}
        adapter.verify_model(state)
        for change in ({"thinking": "Fast"}, {"thinking": "ThinkingPlus"},
                       {"thinking": ""}, {"thinking": "   "},
                       {"thinkCount": 0}, {"thinkCount": 2}, {"model": "Other Model"}):
            with self.subTest(change=change), self.assertRaises(AIError) as caught:
                adapter.verify_model(state | change)
            self.assertEqual(caught.exception.code, "browser_model")
        # A model label mentioning Thinking cannot substitute for the separate control.
        with self.assertRaises(AIError) as caught:
            adapter.verify_model({"modelCount": 1, "model": "Qwen3.7-Plus Thinking"})
        self.assertEqual(caught.exception.code, "browser_model")

    def test_qwen_unconfigured_thinking_does_not_require_a_control(self):
        adapter = SiteAdapter("qwen", {"model_label": "Qwen3.7-Plus", "thinking_label": ""})
        adapter.verify_model({"modelCount": 1, "model": "Qwen3.7-Plus"})

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

    def test_scroll_context_skips_unsupported_sites_and_keeps_original_live_coordinates(self):
        self.client.web['providers'] = ['chatgpt', 'qwen']
        with Image.open(self.image) as image:
            draw = ImageDraw.Draw(image)
            draw.ellipse((169, 89, 190, 110), outline="#aaa", width=2)
            draw.rectangle((20, 170, 33, 183), outline="#aaa", width=2)
            image.save(self.image)
        original = self.image.read_bytes()
        contexts = [{"path": self.image, "box": (0, 0, 160, 80), "label": "Top of question"}]
        uploads = []
        def run(provider, deadline, operation):
            self.assertEqual(provider, "qwen")
            adapter = MagicMock()
            def exchange(page, upload, prompt, report):
                uploads.append(upload)
                with Image.open(upload) as image:
                    self.assertEqual(image.width, 320)
                    self.assertGreater(image.height, 200)
                self.assertIn("ONE question at different scroll positions", prompt)
                self.assertIn("outside it, return action none", prompt)
                self.assertIn('"01": "radio"', prompt)
                self.assertNotIn('"02":', prompt)
                return '{"action":"click","target_id":"01","confidence":0.95,"explanation":"B"}'
            adapter.exchange.side_effect = exchange
            return operation(adapter, None)
        with patch.object(self.client, "_run", side_effect=run):
            raw = self.client.analyze_screenshot(self.image, context_images=contexts,
                                                 control_regions=[(160, 40, 240, 150)])
        result = self.client.parse_coordinates(raw, 320, 200)
        self.assertEqual((result["click_x"], result["click_y"]), (180, 100))
        self.assertEqual(self.client.last_provider, "qwen")
        self.assertEqual(self.client.last_attempts, [{"provider": "chatgpt", "error_code": "browser_context"}])
        self.assertEqual(self.image.read_bytes(), original)
        self.assertFalse(uploads[0].exists())

    def test_gemini_scroll_context_uses_the_same_numbered_controls_and_live_pixels(self):
        self.client.web['providers'] = ['gemini']
        with Image.open(self.image) as image:
            ImageDraw.Draw(image).ellipse((169, 89, 190, 110), outline='#aaa', width=2)
            image.save(self.image)
        contexts = [{'path': self.image, 'box': (0, 0, 160, 80), 'label': 'Question top'}]
        uploads = []
        def run(provider, deadline, operation):
            self.assertEqual(provider, 'gemini')
            adapter = MagicMock()
            def exchange(page, upload, prompt, report):
                uploads.append(upload)
                with Image.open(upload) as image:
                    self.assertGreater(image.height, 200)
                self.assertIn('LIVE ANSWER VIEW', prompt)
                self.assertIn('"01": "radio"', prompt)
                return '{"action":"click","target_id":"01","confidence":0.95,"explanation":"B"}'
            adapter.exchange.side_effect = exchange
            return operation(adapter, None)
        with patch.object(self.client, '_run', side_effect=run):
            raw = self.client.analyze_screenshot(self.image, context_images=contexts,
                                                 control_regions=[(160, 40, 240, 150)])
        result = self.client.parse_coordinates(raw, 320, 200)
        self.assertEqual((result['click_x'], result['click_y']), (180, 100))
        self.assertTrue(self.client.last_grounded)
        self.assertEqual(self.client.last_provider, 'gemini')
        self.assertFalse(uploads[0].exists())

    def test_explicit_qwen_coordinate_prompt_still_grounds_nearby_controls(self):
        self.client.web["providers"] = ["qwen"]
        with Image.open(self.image) as image:
            ImageDraw.Draw(image).ellipse((169, 89, 190, 110), outline="#aaa", width=2)
            image.save(self.image)
        raw = json.dumps({"action": "click", "coordinate_space": "pixels", "click_x": 167, "click_y": 97,
                          "confidence": .95, "explanation": "Option D."})
        with patch.object(self.client, "_run", return_value=raw):
            grounded = self.client.parse_coordinates(self.client.analyze_screenshot(
                self.image, prompt="Return original pixel coordinates."), 320, 200)
        self.assertEqual((grounded["click_x"], grounded["click_y"]), (180, 100))
        self.assertEqual(grounded["model_point"], {"space": "pixels", "x": 167, "y": 97})
        self.assertEqual(grounded["target_control"], {"kind": "radio", "box": [169, 89, 191, 111]})

    def test_default_qwen_upload_uses_ids_and_local_pixels_and_cleans_annotated_copy(self):
        self.client.web["providers"] = ["qwen"]
        with Image.open(self.image) as image:
            draw = ImageDraw.Draw(image)
            draw.ellipse((169, 39, 190, 60), outline="#aaa", width=2)
            draw.ellipse((169, 89, 190, 110), outline="#aaa", width=2)
            image.save(self.image)
        original = self.image.read_bytes()
        uploads = []

        def run(provider, deadline, operation):
            adapter = MagicMock()

            def exchange(page, upload, prompt, report):
                uploads.append(upload)
                self.assertNotEqual(upload, self.image)
                self.assertEqual(upload.stat().st_mode & 0o777, 0o600)
                self.assertEqual(upload.parent.stat().st_mode & 0o777, 0o700)
                self.assertIn('"02": "radio"', prompt)
                self.assertIn("Do not estimate or return coordinates", prompt)
                with Image.open(upload) as image:
                    self.assertEqual(image.size, (320, 200))
                return json.dumps({"action": "click", "target_id": "02", "confidence": .95,
                                   "explanation": "The second option is correct.",
                                   "coordinate_space": "pixels", "click_x": 90, "click_y": 40})

            adapter.exchange.side_effect = exchange
            return operation(adapter, None)

        with patch.object(self.client, "_run", side_effect=run):
            grounded = self.client.parse_coordinates(self.client.analyze_screenshot(self.image), 320, 200)
        self.assertEqual((grounded["click_x"], grounded["click_y"]), (180, 100))
        self.assertEqual(grounded["target_id"], "02")
        self.assertEqual(grounded["target_control"], {"kind": "radio", "box": [169, 89, 191, 111]})
        self.assertEqual(self.image.read_bytes(), original)
        self.assertFalse(uploads[0].exists())

    def test_missing_qwen_control_rejects_the_plan_and_allows_fallback(self):
        self.client.web["providers"] = ["qwen", "gemini"]
        raw = json.dumps({"action": "click", "coordinate_space": "pixels", "click_x": 167, "click_y": 97,
                          "confidence": .99, "explanation": "Option D."})
        with patch.object(self.client, "_run", side_effect=[raw, '{"action":"none"}']):
            self.assertEqual(self.client.analyze_screenshot(self.image), '{"action":"none"}')
        self.assertEqual(self.client.last_provider, "gemini")
        self.assertEqual(self.client.last_attempts, [{"provider": "qwen", "error_code": "browser_grounding"}])
        self.client.web["providers"] = ["qwen"]
        with patch.object(self.client, "_run", return_value=raw), self.assertRaises(AIError) as caught:
            self.client.analyze_screenshot(self.image)
        self.assertEqual(caught.exception.code, "browser_grounding")

    def test_qwen_abstention_and_other_provider_coordinates_are_preserved(self):
        for provider, raw in (("qwen", '{"action":"none"}'), ("gemini", json.dumps({
                "action": "click", "coordinate_space": "pixels", "click_x": 167, "click_y": 97,
                "confidence": .95, "explanation": "Option D."}))):
            with self.subTest(provider=provider):
                self.client.web["providers"] = [provider]
                with patch.object(self.client, "_run", return_value=raw):
                    self.assertEqual(self.client.analyze_screenshot(self.image), raw)

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

    def test_unconfigured_fallbacks_do_not_hide_an_actionable_login_failure(self):
        for provider in ("chatgpt", "qwen"):
            self.client.web["sites"][provider]["model_label"] = ""
        with patch.object(self.client, "_run", side_effect=AIError("Sign in", "browser_login")) as run:
            with self.assertRaises(AIError) as caught:
                self.client.analyze_screenshot(self.image)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(caught.exception.code, "browser_login")

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

    def test_failed_page_preserves_imported_session(self):
        destination = self.client.cookie_file("gemini")
        write_private_json(destination, [{"name": "SID", "value": "local-test-session"}])
        before = destination.read_bytes()
        page = MagicMock()
        page.save_cookies.side_effect = lambda path: write_private_json(path, [])
        operation = MagicMock(side_effect=AIError("Signed out", "browser_login"))
        with patch("web_inference.Lightpanda") as runtime:
            runtime.return_value.__enter__.return_value = page
            with self.assertRaises(AIError):
                self.client._run("gemini", time.monotonic() + 10, operation)
        page.save_cookies.assert_not_called()
        self.assertEqual(destination.read_bytes(), before)

    def test_successful_operation_survives_cookie_refresh_failure(self):
        page = MagicMock()
        page.save_cookies.side_effect = AIError("Deadline", "browser_timeout")
        with patch("web_inference.Lightpanda") as runtime:
            runtime.return_value.__enter__.return_value = page
            self.assertEqual(self.client._run("gemini", time.monotonic() + 10,
                                             lambda adapter, browser: "complete answer"), "complete answer")
        page.save_cookies.assert_called_once_with(self.client.cookie_file("gemini"))


FIXTURE = r'''<!doctype html><html><body>
<button id=new-chat>New chat</button>
<button id=model>Pro</button>
<div id=editor contenteditable=true role=textbox></div>
<input type=file id=file>
<button id=send disabled>Send</button>
<script>
const scenario=SCENARIO;
const answer=JSON.stringify(scenario==='control_ids'
 ?{action:'click',target_id:'01',confidence:0.99,explanation:'Center'}
 :{action:'click',coordinate_space:'normalized_1000',click_x:500,click_y:500,confidence:0.99,explanation:'Center'});
window.submissions=0;
document.querySelector('#new-chat').onclick=()=>{
 document.querySelectorAll('.user,.response,.attachment,#stop').forEach(e=>e.remove());
 document.querySelector('#editor').textContent='';document.querySelector('#send').disabled=true;
 window.completed=false;
};
if(scenario==='sidebar'||scenario==='ambiguous_new_chat'){
 const original=document.querySelector('#new-chat'),reset=original.onclick;
 window.brandClicks=0;window.newChatClicks=0;
 const brand=document.createElement('a');brand.href='/';brand.setAttribute('aria-label','New chat');
 brand.textContent='Gemini';brand.onclick=e=>{e.preventDefault();window.brandClicks++;};
 original.before(brand);
 const chat=document.createElement('a');chat.href='/app';chat.setAttribute('aria-label','New chat');
 chat.textContent='New chat';chat.onclick=e=>{e.preventDefault();window.newChatClicks++;reset();};
 original.replaceWith(chat);
 if(scenario==='ambiguous_new_chat'){
   const duplicate=chat.cloneNode(true);duplicate.onclick=chat.onclick;chat.after(duplicate);
 }
}
if(scenario==='login')document.body.insertAdjacentHTML('beforeend','<button>Sign in</button>');
if(scenario==='wrong_model')document.querySelector('#model').textContent='Fast';
if(scenario==='stale')document.body.insertAdjacentHTML('beforeend','<div class=response>Old answer</div>');
document.querySelector('#file').onchange=async()=>{
 const file=document.querySelector('#file').files[0];
 const preview=document.createElement('div');preview.className='attachment';
 preview.textContent=scenario==='wrong_file'?'other.png':file.name;
 if(scenario==='suffix'){
   preview.textContent='';preview.setAttribute('aria-describedby','preview-description');
   const description=document.createElement('span');description.id='preview-description';description.hidden=true;
   description.textContent=file.name.replace('.png','_456f82.png');document.body.append(description);
 }
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
if(scenario==='menu'||scenario==='raf_menu'){
 const original=document.querySelector('#file'),handler=original.onchange;
 original.id='document-file';original.accept='.pdf';
 const upload=document.createElement('button');upload.id='upload';upload.textContent='Upload';document.body.append(upload);
 const createImageInput=()=>{
   const image=document.createElement('input');image.type='file';image.id='file';image.accept='image/*';
   image.onchange=handler;document.body.append(image);
 };
 upload.onclick=()=>setTimeout(()=>{
   if(scenario==='raf_menu')requestAnimationFrame(createImageInput);else createImageInput();
 },100);
}
</script></body></html>'''

TEST_SELECTORS = {
    "editor": "#editor", "model": "#model", "file": "#file", "upload": "#upload",
    "upload_item": "#upload-item", "attachment": ".attachment", "upload_busy": ".uploading",
    "send": "#send", "user": ".user", "response": ".response", "answer": ".markdown",
    "complete": ".copy", "stop": "#stop", "error": '[role="alert"]',
    "new_chat": "#new-chat",
}


@unittest.skipUnless(os.environ.get("SETH_LIGHTPANDA_TESTS") == "1", "Set SETH_LIGHTPANDA_TESTS=1 for offline Lightpanda integration")
class LightpandaIntegrationTests(unittest.TestCase):
    runtime_engine = 'lightpanda'

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
        selectors = TEST_SELECTORS | ({"file": 'input[type="file"][accept*="image"]'} if scenario in ("menu", "raf_menu") else {})
        if scenario in ("sidebar", "ambiguous_new_chat"):
            selectors["new_chat"] = SITES["gemini"]["selectors"]["new_chat"]
        adapter = SiteAdapter("gemini", {"selectors": selectors})
        adapter.site = adapter.site | {"url": f"http://127.0.0.1:{self.server.server_port}/{scenario}"}
        return adapter

    def test_real_png_upload_and_completed_answer(self):
        for scenario in ("normal", "code", "suffix", "menu"):
            with self.subTest(scenario=scenario), self.browser() as page:
                before = len(self.uploads)
                raw = self.adapter(scenario).exchange(page, self.image, build_prompt(320, 200), lambda label: None)
                self.assertTrue(page.evaluate("window.completed"))
                self.assertEqual(page.evaluate("window.submissions"), 1)
                self.assertEqual(len(self.uploads), before + 1)
                self.assertEqual(hashlib.sha256(self.uploads[-1]).digest(), hashlib.sha256(self.image.read_bytes()).digest())
                decision = WebInferenceClient({}).parse_coordinates(raw, 320, 200)
                self.assertEqual((decision["click_x"], decision["click_y"]), (160, 100))

    def test_client_scroll_context_upload_uses_ids_and_retains_live_coordinates(self):
        with Image.open(self.image) as image:
            ImageDraw.Draw(image).ellipse((169, 89, 190, 110), outline='#aaa', width=2)
            image.save(self.image)
        config = {'web': {'providers': ['gemini'], 'state_dir': str(self.root / 'state'),
                          'executable_path': os.environ.get('SETH_TEST_LIGHTPANDA', str(DEFAULT_BINARY)),
                          'timeout_seconds': 12, 'attempt_timeout_seconds': 12,
                          'sites': {'gemini': {'model_label': 'Pro', 'engine': self.runtime_engine}}}}
        client = WebInferenceClient(config)
        write_private_json(client.cookie_file('gemini'), [])
        before = len(self.uploads)
        with patch.object(client, 'adapter', return_value=self.adapter('control_ids')):
            raw = client.analyze_screenshot(self.image, context_images=[{
                'path': self.image, 'box': (0, 0, 160, 80), 'label': 'Question top'}],
                control_regions=[(160, 40, 240, 150)])
        decision = client.parse_coordinates(raw, 320, 200)
        self.assertEqual((decision['click_x'], decision['click_y']), (180, 100))
        self.assertTrue(client.last_grounded)
        self.assertEqual(len(self.uploads), before + 1)
        import io
        with Image.open(io.BytesIO(self.uploads[-1])) as packed:
            self.assertEqual(packed.width, 320)
            self.assertGreater(packed.height, 200)

    def test_login_model_and_restored_conversation_fail_before_upload(self):
        for scenario, code in (("login", "browser_login"), ("wrong_model", "browser_model"), ("stale", "browser_layout")):
            with self.subTest(scenario=scenario), self.browser() as page:
                before = len(self.uploads)
                with self.assertRaises(AIError) as caught:
                    self.adapter(scenario).exchange(page, self.image, "prompt", lambda label: None)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(page.evaluate("window.submissions"), 0)
                self.assertEqual(len(self.uploads), before)

    def test_ambiguous_new_conversation_controls_fail_before_another_upload(self):
        adapter = self.adapter("ambiguous_new_chat")
        with self.browser() as page:
            adapter.exchange(page, self.image, "First question", lambda label: None)
            before = len(self.uploads)
            page.reuse_page = True
            with self.assertRaises(AIError) as caught:
                adapter.exchange(page, self.image, "Second question", lambda label: None)
            self.assertEqual(caught.exception.code, "browser_layout")
            self.assertEqual(len(self.uploads), before)
            self.assertEqual(page.evaluate("window.submissions"), 1)
            self.assertEqual(page.evaluate("window.brandClicks"), 0)
            self.assertEqual(page.evaluate("window.newChatClicks"), 0)

    def test_failed_and_stalled_uploads_never_submit(self):
        for scenario, code in (("upload_error", "browser_error"), ("stuck_upload", "browser_timeout"), ("wrong_file", "browser_timeout")):
            with self.subTest(scenario=scenario), self.browser() as page:
                # Bound the exchange independently of cold browser startup.
                page.deadline = time.monotonic() + 1.5
                with self.assertRaises(AIError) as caught:
                    self.adapter(scenario).exchange(page, self.image, "prompt", lambda label: None)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(page.evaluate("window.submissions") if code != "browser_timeout" else 0, 0)

    def test_incomplete_response_never_returns_even_when_json_is_valid(self):
        for scenario in ("generating", "interrupted"):
            with self.subTest(scenario=scenario), self.browser() as page:
                page.deadline = time.monotonic() + 1.5
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
            # Check before navigating: the fixture would otherwise set the same
            # cookie again and hide a failed restore.
            restored = page.call("Network.getAllCookies").get("cookies", [])
            self.assertTrue(any(c["name"] == "seth_test" and c["value"] == "restored"
                                for c in restored))

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


@unittest.skipUnless(os.environ.get("SETH_CHROMIUM_TESTS") == "1", "Set SETH_CHROMIUM_TESTS=1 for offline Chromium integration")
class ChromiumIntegrationTests(LightpandaIntegrationTests):
    __unittest_skip__ = os.environ.get("SETH_CHROMIUM_TESTS") != "1"
    runtime_engine = 'chromium'

    def browser(self, timeout=8, cookies=None):
        return Chromium(executable=os.environ.get("SETH_TEST_CHROMIUM") or find_chromium(),
                        profile=self.root / "profile", deadline=time.monotonic() + timeout,
                        stop_event=self.stop, cookies=cookies)

    def test_native_profile_retains_persistent_cookie_without_export_or_import(self):
        profile = self.root / "native-cookie-profile"
        headed = os.environ.get("SETH_CHROMIUM_HEADED_TESTS") == "1"
        for phase, headless in enumerate((not headed, True, not headed)):
            with self.subTest(phase=phase), Chromium(
                    executable=os.environ.get("SETH_TEST_CHROMIUM") or find_chromium(),
                    profile=profile, headless=headless, deadline=time.monotonic() + 10) as page:
                if phase == 0:
                    page.call("Network.setCookies", {"cookies": [{
                        "name": "native_persistence", "value": "synthetic-test",
                        "domain": "example.invalid", "path": "/", "secure": True,
                        "httpOnly": True, "expires": time.time() + 86400,
                    }]})
                # No website navigation or cookie export/import can recreate it.
                saved = page.call("Network.getAllCookies").get("cookies", [])
                self.assertTrue(any(c["name"] == "native_persistence"
                                    and c["value"] == "synthetic-test" for c in saved))

    def test_reconnected_background_chat_renders_upload_menu_without_activating_its_tab(self):
        state = self.root / "background-browser.json"
        profile = self.root / "background-profile"
        adapter = self.adapter("raf_menu")
        def runtime():
            return Chromium(profile=profile, state_file=state, keep_open=True,
                            headless=os.environ.get("SETH_CHROMIUM_HEADED_TESTS") != "1",
                            deadline=time.monotonic() + 10)
        try:
            with runtime() as page:
                page.navigate(adapter.site["url"])
                page.call("Emulation.setFocusEmulationEnabled", {"enabled": False})
                foreground = page.call("Target.createTarget", {
                    "url": "data:text/html,<title>Foreground fixture</title>",
                    "background": False})["targetId"]
                page.wait(lambda: page.evaluate("document.hidden"))
                page.evaluate("window.animationProbe=0;requestAnimationFrame(()=>window.animationProbe++)")
                time.sleep(0.2)
                self.assertEqual(page.evaluate("window.animationProbe"), 0)
            with runtime() as page:
                self.assertTrue(page.reuse_page)
                before = len(self.uploads)
                raw = adapter.exchange(page, self.image, "Background question", lambda label: None)
                self.assertEqual(page.evaluate("window.animationProbe"), 1)
                self.assertEqual(len(self.uploads), before + 1)
                self.assertEqual(page.evaluate("window.submissions"), 1)
                self.assertEqual(WebInferenceClient({}).parse_coordinates(raw, 320, 200)["click_x"], 160)
                # Rendering the original tab must not activate it over the user's tab.
                page.session = page.call("Target.attachToTarget", {
                    "targetId": foreground, "flatten": True})["sessionId"]
                self.assertTrue(page.evaluate("!document.hidden && document.hasFocus()"))
        finally:
            close_managed_browser(state, profile)

    def test_live_session_reconnects_for_two_isolated_image_questions(self):
        for scenario in ("normal", "sidebar"):
            with self.subTest(scenario=scenario):
                state = self.root / f"{scenario}-browser.json"
                profile = self.root / f"{scenario}-persistent-profile"
                adapter = self.adapter(scenario)
                def runtime():
                    return Chromium(profile=profile, state_file=state, keep_open=True,
                                    deadline=time.monotonic() + 10)
                try:
                    with runtime() as page:
                        adapter.exchange(page, self.image, "First question", lambda label: None)
                        page.evaluate("window.liveSessionProof = 'must-survive-without-reload'")
                        pid = json.loads(state.read_text())["pid"]
                    with runtime() as page:
                        self.assertTrue(page.reuse_page)
                        self.assertEqual(page.evaluate("window.liveSessionProof"), 'must-survive-without-reload')
                        raw = adapter.exchange(page, self.image, "Second question", lambda label: None)
                        self.assertEqual(page.evaluate("window.submissions"), 2)
                        self.assertEqual(json.loads(state.read_text())["pid"], pid)
                        self.assertNotIn("First question", adapter.snapshot(page)["users"][0])
                        self.assertEqual(WebInferenceClient({}).parse_coordinates(raw, 320, 200)["click_x"], 160)
                        if scenario == "sidebar":
                            self.assertEqual(page.evaluate("window.brandClicks"), 0)
                            self.assertEqual(page.evaluate("window.newChatClicks"), 1)
                    self.assertEqual(state.stat().st_mode & 0o777, 0o600)
                finally:
                    close_managed_browser(state, profile)


if __name__ == "__main__":
    unittest.main()
