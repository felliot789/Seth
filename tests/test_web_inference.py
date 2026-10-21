"""Offline Chromium tests: local HTTP only, no real provider or VM calls."""

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
from inference import AIError, build_prompt, parse_decision, create_client
from cdp import write_private_json
from web_inference import SITES, SiteAdapter, WebInferenceClient


class WebClientTests(unittest.TestCase):
    def test_selected_provider_sends_original_bytes_and_never_hands_off(self):
        for provider in ("qwen", "gemini"):
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / "question.png"
                Image.new("RGB", (320, 200), "white").save(path)
                client = WebInferenceClient({}, provider=provider)
                expected = '{"status":"uncertain","confidence":0.2,"answer":""}'
                seen = []
                def run(operation):
                    adapter = MagicMock()
                    adapter.exchange.side_effect = lambda page, upload, prompt, report: (
                        seen.append((upload.read_bytes(), prompt)) or expected)
                    return operation(adapter, object())
                with patch.object(client, "_run", side_effect=run) as operation:
                    self.assertEqual(client.analyze_screenshot(path, "table"), expected)
                    operation.assert_called_once()
                self.assertEqual(client.provider, provider)
                self.assertEqual(seen[0][0], path.read_bytes())
                self.assertIn("L or R", seen[0][1])
                self.assertNotIn("target_id", seen[0][1])

    def test_session_lock_does_not_allow_overlapping_requests(self):
        with tempfile.TemporaryDirectory() as root:
            client = WebInferenceClient({"web": {"state_dir": root}})
            with client.session_lock("qwen"):
                with self.assertRaises(AIError):
                    with client.session_lock("qwen"):
                        self.fail("Session lock acquired twice")


FIXTURE = r'''<!doctype html><html><body>
<button id=new-chat>New chat</button>
<button id=model>Pro</button>
<div id=editor contenteditable=true role=textbox></div>
<input type=file id=file>
<button id=send disabled>Send</button>
<script>
const scenario=SCENARIO;
const answer=JSON.stringify({status:'answer',confidence:0.99,answer:scenario==='batch'?'L, R, L':'2'});
window.submissions=0;window.attachmentRemovals=0;
document.querySelector('#new-chat').onclick=()=>{
 document.querySelectorAll('.user,.response,#stop').forEach(e=>e.remove());
 if(scenario!=='draft_persists')document.querySelectorAll('.attachment').forEach(e=>e.remove());
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
 preview.dataset.filename=file.name;
 preview.textContent=scenario==='wrong_file'?'other.png':file.name;
 if(scenario==='suffix'||scenario==='jpeg_suffix'||scenario==='draft_persists'){
   preview.textContent='';preview.setAttribute('aria-describedby','preview-description');
   const description=document.createElement('span');description.id='preview-description';description.hidden=true;
   description.textContent=file.name.replace('.png',scenario==='suffix'?'_456f82.png':'_456f82.jpg');document.body.append(description);
 }
 const remove=document.createElement('button');remove.className='remove';remove.textContent='Remove attachment';
 remove.onclick=()=>{window.attachmentRemovals++;preview.remove();};preview.append(document.createTextNode(' '),remove);
 preview.insertAdjacentHTML('beforeend','<span class=uploading>Uploading</span>');
 if(scenario==='batch_drop')document.querySelectorAll('.attachment').forEach(e=>e.remove());
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
 window.submittedFiles=Array.from(document.querySelectorAll('.attachment')).map(e=>e.dataset.filename);
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
    "remove_attachment": ".attachment .remove",
    "send": "#send", "user": ".user", "response": ".response", "answer": ".markdown",
    "complete": ".copy", "stop": "#stop", "error": '[role="alert"]',
    "new_chat": "#new-chat",
}


@unittest.skipUnless(os.environ.get("SETH_CHROMIUM_TESTS") == "1", "Set SETH_CHROMIUM_TESTS=1 for offline Chromium integration")
class ChromiumIntegrationTests(unittest.TestCase):
    def test_ordered_batch_uploads_every_image_before_one_submission(self):
        paths = [self.root / f"section_{n}.png" for n in range(3)]
        for n, path in enumerate(paths):
            Image.new("RGB", (320, 200), (n * 80, 20, 40)).save(path)
        before = len(self.uploads)
        with self.browser(timeout=15) as page:
            raw = self.adapter("batch").exchange(page, paths, build_prompt("table", 3), lambda _: None)
            self.assertEqual(parse_decision(raw, "table")["answer"], "L, R, L")
            self.assertEqual(page.evaluate("window.submissions"), 1)
            self.assertEqual(page.evaluate("window.submittedFiles"), [path.name for path in paths])
        self.assertEqual(self.uploads[before:], [path.read_bytes() for path in paths])

    def test_replaced_earlier_attachment_prevents_submission(self):
        second = self.root / "second.png"
        Image.new("RGB", (320, 200), "red").save(second)
        with self.browser(timeout=15) as page:
            with self.assertRaises(AIError) as caught:
                self.adapter("batch_drop").exchange(page, [self.image, second], "One question", lambda _: None)
            self.assertEqual(caught.exception.code, "browser_layout")
            self.assertEqual(page.evaluate("window.submissions"), 0)

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

    def browser(self, timeout=8):
        return Chromium(profile=self.root / "profile", deadline=time.monotonic() + timeout, stop_event=self.stop)

    def adapter(self, scenario="normal"):
        selectors = TEST_SELECTORS | ({"file": 'input[type="file"][accept*="image"]'} if scenario in ("menu", "raf_menu") else {})
        if scenario in ("sidebar", "ambiguous_new_chat"):
            selectors["new_chat"] = SITES["gemini"]["selectors"]["new_chat"]
        adapter = SiteAdapter("gemini", {"selectors": selectors})
        adapter.site = adapter.site | {"url": f"http://127.0.0.1:{self.server.server_port}/{scenario}"}
        return adapter

    def test_real_png_upload_and_completed_answer(self):
        for scenario in ("normal", "code", "suffix", "jpeg_suffix", "menu"):
            with self.subTest(scenario=scenario), self.browser() as page:
                before = len(self.uploads)
                raw = self.adapter(scenario).exchange(page, self.image, build_prompt("mc"), lambda label: None)
                self.assertTrue(page.evaluate("window.completed"))
                self.assertEqual(page.evaluate("window.submissions"), 1)
                self.assertEqual(len(self.uploads), before + 1)
                self.assertEqual(hashlib.sha256(self.uploads[-1]).digest(), hashlib.sha256(self.image.read_bytes()).digest())
                decision = parse_decision(raw, "mc")
                self.assertEqual(decision["answer"], "2")

    def test_unsent_attachment_from_an_earlier_attempt_is_cleared_before_a_new_upload(self):
        adapter = self.adapter('draft_persists')
        with self.browser(timeout=12) as page:
            adapter.prepare(page)
            page.set_file('#file',self.image)
            adapter.wait_attachment(page,self.image)
            self.assertEqual(page.evaluate('window.submissions'),0)
            self.assertEqual(len(adapter.snapshot(page)['attachments']),1)
            page.evaluate("""(()=>{const s=document.createElement('style');
s.textContent='.attachment .remove{visibility:hidden}.attachment:hover .remove{visibility:visible}';
document.head.append(s);})()""")
            self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('.attachment .remove')).visibility"),'hidden')
            before = len(self.uploads)
            page.reuse_page = True
            raw = adapter.exchange(page,self.image,'Fresh question',lambda label:None)
            self.assertEqual(page.evaluate('window.submissions'),1)
            self.assertEqual(page.evaluate('window.attachmentRemovals'),1)
            self.assertEqual(len(self.uploads),before+1)
            self.assertIn('Fresh question',adapter.snapshot(page)['users'][0])
            self.assertEqual(parse_decision(raw, "mc")["answer"],"2")

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
                self.assertEqual(parse_decision(raw, "mc")["answer"], "2")
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
                        self.assertEqual(parse_decision(raw, "mc")["answer"], "2")
                        if scenario == "sidebar":
                            self.assertEqual(page.evaluate("window.brandClicks"), 0)
                            self.assertEqual(page.evaluate("window.newChatClicks"), 1)
                    self.assertEqual(state.stat().st_mode & 0o777, 0o600)
                finally:
                    close_managed_browser(state, profile)
