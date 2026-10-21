"""Offline Qwen DOM regressions; no provider account or VM is contacted."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
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

from chromium_runtime import Chromium, close_managed_browser, find_chromium
from inference import AIError, parse_decision
from cdp import write_private_json
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
window.events=[];window.submissions=0;window.menuSelections=0;window.menuOpens=0;window.newChats=0;
window.unarmedChanges=0;window.completed=false;window.submittedPrompts=[];window.downloads=0;
if(scenario==='late_upload_control'){
 const upload=document.querySelector('[aria-label="Select Mode"]');upload.style.display='none';
 setTimeout(()=>upload.style.display='',800);
}
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
 window.menuOpens++;
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
 if(scenario==='closing_menu'&&window.menuOpens===1){
   menu.querySelector('[role="menuitem"]').style.display='none';
   setTimeout(()=>menu.remove(),30);
 }
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
 if(scenario==='jpeg_suffix')image.alt=file.name.replace(/\.(png|jpe?g)$/i,'_16fa84.jpg');
 const progress=document.createElement('span');progress.className='anticon anticon-spin vision-item-loading-icon';
 preview.append(image,progress);
 if(scenario==='batch_drop')files.replaceChildren(preview);else files.append(preview);
 send.disabled=!draft;
 // Even a preview and enabled Send can precede completion of image processing.
 setTimeout(()=>{uploadReady=true;preview.classList.remove('vision-item-container-uploading');
   progress.remove();window.events.push('ready');},200);
}
input.addEventListener('change',uploadFile);
send.onclick=()=>{
 if(!uploadReady||!draft)throw new Error('Submission before upload/component readiness');
 window.submissions++;window.events.push('submit');window.submittedPrompts.push(draft);
 window.submittedFiles=Array.from(files.querySelectorAll('img')).map(e=>e.alt);
 const user=document.createElement('div');user.className='qwen-chat-message-user';user.textContent=draft;
 const assistant=document.createElement('div');assistant.className='qwen-chat-message-assistant';
 assistant.innerHTML='<div class="qwen-chat-thinking-status-card-completed"><div class="qwen-markdown"></div><button aria-label="Copy">Copy thinking</button></div>';
 assistant.querySelector('.qwen-markdown').textContent=JSON.stringify({status:'answer',confidence:0.99,answer:'99'});
 const stop=document.createElement('button');stop.className='stop-button';stop.setAttribute('aria-label','Stop');
 document.body.append(user,assistant,stop);files.replaceChildren();send.disabled=true;
 setTimeout(()=>{
   const final=document.createElement('div');final.className='response-message-content phase-answer';
   final.innerHTML='<div class="qwen-markdown"><pre><code></code></pre></div>';
   const decision={status:'answer',confidence:0.99,answer:scenario==='batch'?'L, R, L':'2'};
   final.querySelector('code').textContent=JSON.stringify(decision);
   if(scenario==='rendered_code'){
     const answer={status:'answer',confidence:0.99,
       answer:'1, 3 café ✓ '+ 'proof '.repeat(100)};
     final.querySelector('.qwen-markdown').innerHTML='<pre class="qwen-markdown-code"><div class="qwen-markdown-code-header"><span>json</span><button aria-label="Download">Download</button></div><div class="qwen-markdown-code-editor-viewport">12345678910111213141516 {"status": "answer", "answer": "</div></pre>';
     final.querySelector('[aria-label="Download"]').onclick=()=>{
       if(!window.completed)throw new Error('Download before completed final answer');
       window.downloads++;window.events.push('download');
       const link=document.createElement('a');link.download='answer.json';
       link.href=URL.createObjectURL(new Blob([JSON.stringify(answer,null,2)],{type:'application/json'}));
       setTimeout(()=>{link.click();URL.revokeObjectURL(link.href);},50);
     };
   }
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

    def test_answer_download_is_private_bounded_and_cleaned_up(self):
        for content, expected_code in (('{"status":"no_action"}', None), ("", "browser_error"),
                                       ("x" * 33, "browser_error"), (b"\xff", "browser_error")):
            with self.subTest(content=content):
                page = self.page()
                directories = []

                def trigger():
                    directory = Path(call.call_args.args[1]["downloadPath"])
                    self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                    directories.append(directory)
                    if isinstance(content, bytes):
                        (directory / "answer.json").write_bytes(content)
                    else:
                        (directory / "answer.json").write_text(content)

                with patch.object(page, "call", return_value={}) as call:
                    if expected_code:
                        with self.assertRaises(AIError) as caught:
                            page.download_text(trigger, max_bytes=32)
                        self.assertEqual(caught.exception.code, expected_code)
                    else:
                        self.assertEqual(page.download_text(trigger, max_bytes=32), content)
                self.assertFalse(directories[0].exists())
                self.assertEqual(page.session, "fixture-session")
                self.assertEqual(json.loads(page.ws.send.call_args.args[0]), {
                    "id": 18, "method": "Browser.setDownloadBehavior", "params": {"behavior": "default"}})

    def test_answer_download_cleanup_preserves_cancellation_and_original_failure(self):
        page = self.page()
        original = AIError("Original cancelled operation", "cancelled")

        def cancelled():
            page.deadline = time.monotonic() - 1
            page.stop_event.set()
            raise original

        with patch.object(page, "call", return_value={}) as call, self.assertRaises(AIError) as caught:
            page.download_text(cancelled)
        self.assertIs(caught.exception, original)
        self.assertFalse(Path(call.call_args.args[1]["downloadPath"]).exists())
        self.assertEqual(json.loads(page.ws.send.call_args.args[0]), {
            "id": 18, "method": "Browser.setDownloadBehavior", "params": {"behavior": "default"}})


class QwenUploadReadinessTests(unittest.TestCase):
    def test_only_an_explicit_no_click_result_is_polled_again(self):
        adapter = WebInferenceClient({}).adapter("qwen")
        page = MagicMock()

        def wait(poll):
            while not poll():
                pass

        page.wait.side_effect = wait
        with patch.object(adapter, "click", side_effect=[AIError("Not mounted", "browser_layout"), None]) as click:
            adapter._click_upload_control(page, "fixture-selector")
        self.assertEqual(click.call_count, 2)
        for code in ("browser_javascript", "browser_protocol", "browser_error", "cancelled"):
            with self.subTest(code=code), patch.object(adapter, "click", side_effect=AIError("Ambiguous failure", code)) as click:
                with self.assertRaises(AIError) as caught:
                    adapter._click_upload_control(page, "fixture-selector")
                self.assertEqual(caught.exception.code, code)
                click.assert_called_once()

    def test_uncertain_upload_item_click_never_reopens_or_repeats_it(self):
        adapter = WebInferenceClient({}).adapter("qwen")
        page = MagicMock()
        page.wait.side_effect = lambda poll: poll()
        for code in ("browser_javascript", "browser_protocol", "browser_error", "cancelled"):
            with self.subTest(code=code), patch.object(adapter, "click", side_effect=[None, AIError("Uncertain item click", code)]) as click:
                with self.assertRaises(AIError) as caught:
                    adapter.attach(page, Path("unused.png"))
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(click.call_count, 2)
                page.set_file.assert_not_called()


@unittest.skipUnless(os.environ.get("SETH_CHROMIUM_TESTS") == "1",
                     "Set SETH_CHROMIUM_TESTS=1 for offline Qwen Chromium integration")
class QwenChromiumIntegrationTests(unittest.TestCase):
    def test_ordered_batch_uploads_every_image_before_one_submission(self):
        paths = [self.root / f"section_{n}.png" for n in range(3)]
        for n, path in enumerate(paths):
            Image.new("RGB", (320, 200), (n * 80, 20, 40)).save(path)
        before = len(self.uploads)
        with self.browser() as page:
            raw = self.adapter("batch").exchange(page, paths, "Read one table across three overlapping sections", lambda _: None)
            self.assertEqual(parse_decision(raw, "table")["answer"], "L, R, L")
            self.assertEqual(page.evaluate("window.submissions"), 1)
            self.assertEqual(page.evaluate("window.submittedFiles"), [path.name for path in paths])
            self.assertEqual(page.evaluate("window.menuSelections"), len(paths))
        self.assertEqual(self.uploads[before:], [path.read_bytes() for path in paths])

    def test_replaced_earlier_attachment_prevents_submission(self):
        second = self.root / "second.png"
        Image.new("RGB", (320, 200), "red").save(second)
        with self.browser() as page:
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
                html = QWEN_FIXTURE.replace("SCENARIO", json.dumps(scenario)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
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
                        keep_open=persistent, deadline=time.monotonic() + 20)

    def assert_uploaded_image(self, before):
        self.assertEqual(len(self.uploads), before + 1)
        self.assertEqual(hashlib.sha256(self.uploads[-1]).digest(),
                         hashlib.sha256(self.image.read_bytes()).digest())

    def test_png_and_jpeg_sources_accept_converted_preview_after_one_upload(self):
        for image_format,suffix in (('PNG','.png'),('JPEG','.jpg'),('JPEG','.jpeg')):
            with self.subTest(image_format=image_format,suffix=suffix):
                self.image=self.image.with_suffix(suffix)
                Image.new('RGB',(320,200),'white').save(self.image,format=image_format)
                before=len(self.uploads)
                with self.browser() as page:
                    raw=self.adapter('jpeg_suffix').exchange(page,self.image,'Read this screenshot.',lambda label:None)
                    self.assertEqual(json.loads(raw)['status'],'answer')
                    self.assertEqual(page.evaluate('window.submissions'),1)
                self.assert_uploaded_image(before)

    def test_pending_unsent_image_is_cleared_before_the_next_cycle(self):
        adapter=self.adapter('jpeg_suffix')
        with self.browser() as page:
            adapter.prepare(page)
            page.evaluate("""(()=>{const e=document.querySelector('textarea');
Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,'Old draft');
e.dispatchEvent(new Event('input',{bubbles:true}));})()""")
            adapter.attach(page,self.image)
            self.assertEqual(page.evaluate('window.submissions'),0)
            before=len(self.uploads)
            raw=adapter.exchange(page,self.image,'Fresh request',lambda label:None)
            self.assertEqual(json.loads(raw)['status'],'answer')
            self.assertEqual(page.evaluate('window.newChats'),1)
            self.assertEqual(page.evaluate('window.submissions'),1)
            self.assertEqual(len(self.uploads),before+1)
            self.assertIn('Fresh request',adapter.snapshot(page)['users'][0])

    def test_upload_control_can_mount_after_the_chat_editor(self):
        before = len(self.uploads)
        adapter = self.adapter("late_upload_control")
        with self.browser() as page:
            raw = adapter.exchange(page, self.image, "Select the answer.", lambda label: None)
            self.assertEqual(json.loads(raw)["status"], "answer")
            self.assertEqual(page.evaluate("window.submissions"), 1)
            self.assertEqual(page.evaluate("window.menuSelections"), 1)
        self.assert_uploaded_image(before)

    def test_menu_closing_before_item_click_is_reopened_without_duplicate_upload(self):
        before = len(self.uploads)
        adapter = self.adapter("closing_menu")
        with self.browser() as page:
            raw = adapter.exchange(page, self.image, "Select the answer.", lambda label: None)
            self.assertEqual(json.loads(raw)["status"], "answer")
            self.assertEqual(page.evaluate("window.menuOpens"), 2)
            self.assertEqual(page.evaluate("window.menuSelections"), 1)
            self.assertEqual(page.evaluate("window.submissions"), 1)
        self.assert_uploaded_image(before)

    def test_virtualized_code_is_read_completely_after_generation_without_clipboard(self):
        adapter = self.adapter("rendered_code")
        with self.browser() as page:
            raw = adapter.exchange(page, self.image, "Select all correct answers.", lambda label: None)
            result = json.loads(raw)
            self.assertEqual(result["status"], "answer")
            self.assertTrue(result["answer"].startswith("1, 3"))
            self.assertIn("café ✓", result["answer"])
            self.assertGreater(len(result["answer"]), 600)
            self.assertEqual(page.evaluate("window.downloads"), 1)
            self.assertEqual(page.evaluate("window.events.slice(-2)"), ["final", "download"])

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
            decision = parse_decision(raw, "mc")
            self.assertEqual(decision["answer"], "2")
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
