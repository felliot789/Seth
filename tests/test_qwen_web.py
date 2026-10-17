"""Offline Qwen DOM regressions; no provider account or VM is contacted."""

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

from PIL import Image

from chromium_runtime import Chromium, close_managed_browser, find_chromium
from inference import AIError
from web_inference import WebInferenceClient


QWEN_FIXTURE = r'''<!doctype html><html><body>
<div role="button" aria-label="New Chat">New Chat</div>
<div role="button" aria-label="Select Model">Qwen3.7-Plus</div>
<div class="qwen-thinking-selector"><span class="qwen-chat-v2-dropdown-menu-select-label">Thinking</span></div>
<textarea class="message-input-textarea"></textarea>
<input id="filesUpload" type="file" hidden>
<div role="button" aria-label="Select Mode">Add</div>
<div class="message-input-column-file"></div>
<button class="send-button" aria-label="Send" disabled>Send</button>
<script>
const scenario=SCENARIO;
const editor=document.querySelector('textarea'),input=document.querySelector('#filesUpload');
const send=document.querySelector('.send-button'),files=document.querySelector('.message-input-column-file');
const nativeValue=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value');
let tracked='',draft='',armed=false,uploadReady=false;
window.events=[];window.submissions=0;window.menuSelections=0;window.newChats=0;
window.unarmedChanges=0;window.completed=false;window.submittedPrompts=[];
if(scenario==='hydrating'||scenario==='signed_out'){
 const login=document.createElement('button');login.textContent='Log in';document.body.append(login);
 if(scenario==='hydrating')setTimeout(()=>login.remove(),1100);
}
// Like a controlled React input, assigning e.value updates the tracker without
// changing component state. The native setter plus a bubbling input event must work.
Object.defineProperty(editor,'value',{
 get(){return nativeValue.get.call(this);},
 set(value){tracked=String(value);nativeValue.set.call(this,value);}
});
editor.addEventListener('input',()=>{
 const value=nativeValue.get.call(editor);
 if(value!==tracked){tracked=value;draft=value;window.events.push('input');}
});
document.querySelector('[aria-label="New Chat"]').onclick=()=>{
 window.newChats++;window.events.push('new-chat');
 document.querySelectorAll('.qwen-chat-message-user,.qwen-chat-message-assistant,.stop-button').forEach(e=>e.remove());
 files.replaceChildren();editor.value='';draft='';uploadReady=false;send.disabled=true;
 window.completed=false;
 // Deliberately retain input.value and accept: repeating a file needs a reset,
 // and a previously initialized input still needs another menu selection.
};
document.querySelector('[aria-label="Select Mode"]').onclick=()=>{
 const menu=document.createElement('div');menu.id='upload-menu';
 menu.innerHTML='<div role="menuitem"><span class="mode-select-dropdown-item-name">Upload attachment</span><span>file, image, video, audio</span></div>';
 menu.querySelector('[role="menuitem"]').onclick=()=>{
   window.menuSelections++;menu.remove();
   let picker=input;
   if(scenario==='replacement_input'){
     input.removeEventListener('change',uploadFile);
     picker=document.createElement('input');picker.type='file';picker.hidden=true;
     picker.addEventListener('change',uploadFile);document.body.append(picker);
   }
   const activate=()=>{armed=true;picker.accept='image/png,image/jpeg';
     window.events.push('armed');picker.click();};
   if(scenario==='late_input')setTimeout(activate,150);
   else if(scenario==='late_handler'){
     input.accept='image/png,image/jpeg';requestAnimationFrame(activate);
   }else activate();
 };
 document.body.append(menu);
};
async function uploadFile(event){
 if(!armed){window.unarmedChanges++;return;}
 armed=false;window.events.push('change');
 const file=event.target.files[0];if(!file)return;
 uploadReady=false;send.disabled=true;
 const result=await fetch('/upload',{method:'POST',body:file});
 if(!result.ok)return;
 window.events.push('received');
 const preview=document.createElement('div');preview.className='vision-item-container vision-item-container-uploading';
 const image=document.createElement('img');image.className='vision-item-image';
 image.alt=scenario==='wrong_file'?'other.png':file.name;
 const progress=document.createElement('span');progress.className='anticon anticon-spin vision-item-loading-icon';
 preview.append(image,progress);files.replaceChildren(preview);send.disabled=!draft;
 // Even a preview and enabled Send can precede completion of image processing.
 setTimeout(()=>{uploadReady=true;preview.classList.remove('vision-item-container-uploading');
   progress.remove();window.events.push('ready');},200);
}
input.addEventListener('change',uploadFile);
send.onclick=()=>{
 if(!uploadReady||!draft)throw new Error('Submission before upload/component readiness');
 window.submissions++;window.events.push('submit');window.submittedPrompts.push(draft);
 const user=document.createElement('div');user.className='qwen-chat-message-user';user.textContent=draft;
 const assistant=document.createElement('div');assistant.className='qwen-chat-message-assistant';
 assistant.innerHTML='<div class="qwen-chat-thinking-status-card-completed"><div class="qwen-markdown"></div><button aria-label="Copy">Copy thinking</button></div>';
 assistant.querySelector('.qwen-markdown').textContent=JSON.stringify({action:'none',explanation:'Thinking, not the final answer'});
 const stop=document.createElement('button');stop.className='stop-button';stop.setAttribute('aria-label','Stop');
 document.body.append(user,assistant,stop);files.replaceChildren();send.disabled=true;
 setTimeout(()=>{
   const final=document.createElement('div');final.className='response-message-content phase-answer';
   final.innerHTML='<div class="qwen-markdown"><pre><code></code></pre></div>';
   final.querySelector('code').textContent=JSON.stringify({action:'click',coordinate_space:'normalized_1000',
     click_x:500,click_y:500,confidence:0.99,explanation:'Final answer'});
   assistant.append(final);
   if(scenario==='interrupted'){stop.remove();return;}
   // Keep valid final JSON visible for longer than the stability window while
   // generating. Neither that JSON nor the thinking copy button means complete.
   setTimeout(()=>{
     const footer=document.createElement('div');footer.className='response-message-footer';
     footer.innerHTML='<button aria-label="Copy">Copy answer</button>';assistant.append(footer);
     stop.remove();window.completed=true;window.events.push('final');
   },1250);
 },100);
};
</script></body></html>'''


class ChromiumUploadCleanupTests(unittest.TestCase):
    def page(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        page = Chromium(profile=Path(temporary.name) / "profile", deadline=time.monotonic() + 10,
                        stop_event=threading.Event())
        page.ws = MagicMock()
        page.session = "fixture-session"
        page.counter = 17
        return page

    def assert_cleanup_command(self, page):
        page.ws.settimeout.assert_called_once_with(0.2)
        page.ws.send.assert_called_once()
        self.assertEqual(json.loads(page.ws.send.call_args.args[0]), {
            "id": 18, "method": "Page.setInterceptFileChooserDialog",
            "params": {"enabled": False}, "sessionId": "fixture-session"})

    def test_file_chooser_cleanup_survives_expired_deadline_and_cancellation(self):
        page = self.page()
        original = AIError("Original operation timed out", "browser_timeout")
        with patch.object(page, "call", return_value={}) as call:
            with self.assertRaises(AIError) as caught:
                with page.intercept_file_chooser():
                    page.deadline = time.monotonic() - 1
                    page.stop_event.set()
                    raise original
        self.assertIs(caught.exception, original)
        call.assert_called_once_with("Page.setInterceptFileChooserDialog", {"enabled": True})
        self.assert_cleanup_command(page)

    def test_socket_cleanup_failure_preserves_original_operation_error(self):
        import websocket
        for failure in (OSError("Closed socket"), websocket.WebSocketConnectionClosedException("Closed socket")):
            with self.subTest(failure=type(failure).__name__):
                page = self.page()
                page.ws.send.side_effect = failure
                original = AIError("Required upload control is ambiguous", "browser_layout")
                with patch.object(page, "call", return_value={}):
                    with self.assertRaises(AIError) as caught:
                        with page.intercept_file_chooser():
                            raise original
                self.assertIs(caught.exception, original)
                self.assert_cleanup_command(page)


@unittest.skipUnless(os.environ.get("SETH_CHROMIUM_TESTS") == "1",
                     "Set SETH_CHROMIUM_TESTS=1 for offline Qwen Chromium integration")
class QwenChromiumIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uploads = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                scenario = self.path.strip("/") or "normal"
                html = QWEN_FIXTURE.replace("SCENARIO", json.dumps(scenario)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html)

            def do_POST(self):
                cls.uploads.append(self.rfile.read(int(self.headers["Content-Length"])))
                time.sleep(0.15)
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
        self.client = WebInferenceClient({"web": {"sites": {"qwen": {
            "model_label": "Qwen3.7-Plus", "thinking_label": "Thinking"}}}})

    def adapter(self, scenario="normal"):
        adapter = self.client.adapter("qwen")
        adapter.site = adapter.site | {"url": f"http://127.0.0.1:{self.server.server_port}/{scenario}"}
        return adapter

    def browser(self, *, persistent=False):
        return Chromium(executable=os.environ.get("SETH_TEST_CHROMIUM") or find_chromium(),
                        profile=self.root / "profile", state_file=self.root / "browser.json",
                        keep_open=persistent, deadline=time.monotonic() + 12)

    def assert_uploaded_image(self, before):
        self.assertEqual(len(self.uploads), before + 1)
        self.assertEqual(hashlib.sha256(self.uploads[-1]).digest(),
                         hashlib.sha256(self.image.read_bytes()).digest())

    def test_menu_armed_upload_and_controlled_textarea_return_only_completed_final(self):
        before = len(self.uploads)
        with self.browser() as page:
            choosers, receive = [], page.ws.recv

            def record_chooser():
                raw = receive()
                message = json.loads(raw)
                if message.get("method") == "Page.fileChooserOpened":
                    choosers.append(message["params"])
                return raw

            with patch.object(page.ws, "recv", side_effect=record_chooser):
                raw = self.adapter("hydrating").exchange(page, self.image, "Find the center.", lambda label: None)
            self.assertEqual(len(choosers), 1)
            self.assertTrue(page.evaluate("window.completed"))
            self.assertEqual(page.evaluate("window.submissions"), 1)
            self.assertEqual(page.evaluate("window.menuSelections"), 1)
            self.assertEqual(page.evaluate("window.unarmedChanges"), 0)
            self.assertEqual(page.evaluate("window.events"),
                             ["input", "armed", "change", "received", "ready", "submit", "final"])
            self.assertIn("Find the center.", page.evaluate("window.submittedPrompts[0]"))
            decision = self.client.parse_coordinates(raw, 320, 200)
            self.assertEqual((decision["click_x"], decision["click_y"]), (160, 100))
            self.assertEqual(decision["explanation"], "Final answer")
        self.assert_uploaded_image(before)

    def test_persistent_sign_in_prompt_fails_before_upload_after_hydration_grace(self):
        before = len(self.uploads)
        with self.browser() as page:
            with self.assertRaises(AIError) as caught:
                self.adapter("signed_out").exchange(page, self.image, "Question", lambda label: None)
            self.assertEqual(caught.exception.code, "browser_login")
            self.assertEqual(page.evaluate("window.submissions"), 0)
            self.assertEqual(page.evaluate("window.menuSelections"), 0)
        self.assertEqual(len(self.uploads), before)

    def test_menu_selection_waits_until_the_image_input_is_armed(self):
        for scenario in ("late_input", "late_handler"):
            with self.subTest(scenario=scenario):
                before = len(self.uploads)
                with self.browser() as page:
                    self.adapter(scenario).exchange(page, self.image, "Question", lambda label: None)
                    self.assertEqual(page.evaluate("window.unarmedChanges"), 0)
                    self.assertEqual(page.evaluate("window.menuSelections"), 1)
                    self.assertEqual(page.evaluate("window.submissions"), 1)
                self.assert_uploaded_image(before)

    def test_upload_uses_the_native_chooser_when_the_page_input_is_stale(self):
        before = len(self.uploads)
        with self.browser() as page:
            self.adapter("replacement_input").exchange(page, self.image, "Question", lambda label: None)
            self.assertEqual(page.evaluate("window.submissions"), 1)
            self.assertEqual(page.evaluate("document.querySelector('#filesUpload').files.length"), 0)
        self.assert_uploaded_image(before)

    def test_reconnecting_rearms_upload_for_same_file_and_starts_fresh_chat(self):
        adapter = self.adapter()
        try:
            with self.browser(persistent=True) as page:
                adapter.exchange(page, self.image, "First question", lambda label: None)
                page.evaluate("window.sessionProof='same page'")
            before = len(self.uploads)
            with self.browser(persistent=True) as page:
                self.assertTrue(page.reuse_page)
                adapter.exchange(page, self.image, "Second question", lambda label: None)
                self.assertEqual(page.evaluate("window.sessionProof"), "same page")
                self.assertEqual(page.evaluate("window.newChats"), 1)
                self.assertEqual(page.evaluate("window.menuSelections"), 2)
                self.assertEqual(page.evaluate("window.submissions"), 2)
                self.assertEqual(page.evaluate("window.unarmedChanges"), 0)
                state = adapter.snapshot(page)
                self.assertEqual(len(state["users"]), 1)
                self.assertIn("Second question", state["users"][0])
                self.assertNotIn("First question", state["users"][0])
            self.assert_uploaded_image(before)
        finally:
            close_managed_browser(self.root / "browser.json", self.root / "profile")

    def test_wrong_attachment_never_submits_and_thinking_copy_cannot_complete_interrupted_answer(self):
        for scenario, submissions in (("wrong_file", 0), ("interrupted", 1)):
            with self.subTest(scenario=scenario), self.browser() as page:
                page.deadline = time.monotonic() + 2.5
                with self.assertRaises(AIError) as caught:
                    self.adapter(scenario).exchange(page, self.image, "Question", lambda label: None)
                self.assertEqual(caught.exception.code, "browser_timeout")
                page.deadline = time.monotonic() + 2
                self.assertEqual(page.evaluate("window.submissions"), submissions)
                self.assertEqual(page.evaluate("window.menuSelections"), 1)
                self.assertFalse(page.evaluate("window.completed"))


if __name__ == "__main__":
    unittest.main()
