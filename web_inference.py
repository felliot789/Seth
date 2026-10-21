"""One selected Chromium provider; no fallback, parsing or control mapping."""

from contextlib import contextmanager
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

from chromium_runtime import Chromium, find_chromium
from inference import AIError, build_prompt, image_batch, screenshot_info
from settings import load_config

LOG = logging.getLogger("seth")
SITES = {
    "gemini": {
        "url": "https://gemini.google.com/app",
        "login_domains": ("google.com", "googleusercontent.com"),
        "model_label": "Pro",
        "selectors": {
            "editor": 'rich-textarea [contenteditable="true"][role="textbox"]',
            "model": '[data-test-id="bard-mode-menu-button"]',
            "file": 'input[type="file"][accept*="image"]',
            "upload": 'button[aria-label="Upload & tools"], button[aria-label="Upload and tools"], button[aria-label="Open upload file menu"]',
            "upload_item": '[role="menuitem"]',
            "attachment": 'uploader-file-preview',
            "remove_attachment": 'uploader-file-preview button[aria-label="close attachment"]',
            "upload_busy": 'uploader-file-preview [role="progressbar"], uploader-file-preview mat-spinner, uploader-file-preview mat-progress-spinner',
            "send": 'button[aria-label="Send message"], button[aria-label="Submit"]',
            "user": 'user-query',
            "response": 'model-response',
            "answer": '.markdown',
            "complete": 'button[aria-label="Copy"], button[aria-label="Copy response"]',
            "stop": 'button[aria-label="Stop response"], button[aria-label="Stop generating"]',
            "error": '[role="alert"], .error-message',
            # Expanded navigation also labels the Gemini home/logo link "New chat".
            # Use the chat route so that link cannot make this control ambiguous.
            "new_chat": 'a[aria-label="New chat"][href="/app"], button[aria-label="New chat"]',
        },
    },
    "qwen": {
        "url": "https://chat.qwen.ai/",
        "login_domains": ("qwen.ai", "qwenlm.ai"),
        "model_label": "",
        "selectors": {
            "editor": 'textarea.message-input-textarea',
            "model": '[role="button"][aria-label="Select Model"]',
            "thinking": '.qwen-thinking-selector .qwen-chat-v2-dropdown-menu-select-label',
            "file": '#filesUpload[type="file"]',
            "upload": '[role="button"][aria-label="Select Mode"]',
            "upload_item": '[role="menuitem"]:not([aria-disabled="true"]):not(.qwen-chat-v2-dropdown-menu-item-disabled) .mode-select-dropdown-item-name',
            "attachment": '.message-input-column-file .vision-item-container',
            "upload_busy": '.message-input-column-file .vision-item-container-uploading, .message-input-column-file .vision-item-loading-icon, .message-input-column-file [role="progressbar"], .message-input-column-file .ant-spin-spinning',
            "send": 'button.send-button[aria-label="Send"]',
            "user": '.qwen-chat-message-user',
            "response": '.qwen-chat-message-assistant',
            "answer": '.response-message-content.phase-answer .qwen-markdown',
            "complete": '.response-message-footer button[aria-label="Copy"]',
            "stop": 'button.stop-button[aria-label="Stop"]',
            "error": '[role="alert"], .ant-message-error',
            "new_chat": '[role="button"][aria-label="New Chat"]',
        },
    },
}

# Ignore controls hidden by their own or ancestor styles.
DOM_HELPERS = r"""
const visible=e=>{for(let p=e;p&&p.nodeType===1;p=p.parentElement){
    const s=getComputedStyle(p);if(p.hidden||p.getAttribute('aria-hidden')==='true'||s.display==='none'||s.visibility==='hidden')return false;
}return true;};
const all=(s,scope=document)=>Array.from(scope.querySelectorAll(s)).filter(visible);
const text=e=>e?(e.textContent||'').trim():'';
const described=e=>(e.getAttribute('aria-describedby')||'').split(/\s+/).map(id=>text(document.getElementById(id))).join(' ');
const label=e=>[text(e),e.getAttribute('aria-label')||'',e.getAttribute('title')||'',
    described(e),...Array.from(e.querySelectorAll('[aria-label],[title],img[alt],[aria-describedby]')).map(x=>
    [x.getAttribute('aria-label')||'',x.getAttribute('title')||'',x.getAttribute('alt')||'',described(x)].join(' '))].join(' ');
"""


def dom(page, body, *arguments, user_gesture=False):
    expression = "((args)=>{" + DOM_HELPERS + body + "})(" + json.dumps(arguments) + ")"
    return page.evaluate(expression, user_gesture=True) if user_gesture else page.evaluate(expression)


class SiteAdapter:
    def __init__(self, name, options=None):
        if name not in SITES:
            raise AIError("Unknown web inference provider.")
        self.name = name
        self.site = SITES[name]
        self.options = options or {}
        self.selectors = self.site["selectors"] | self.options.get("selectors", {})
        self.model_label = self.options.get("model_label", self.site["model_label"])
        self.thinking_label = self.options.get("thinking_label", "")

    def snapshot(self, page):
        return dom(page, """
const s=args[0], models=all(s.model),users=all(s.user),responses=all(s.response);
const thinking=s.thinking?all(s.thinking):[];
const response=responses.length===1?responses[0]:null;
const answers=response?all(s.answer,response):[];
let answer='',oversize=false;
if(answers.length===1){
    const a=answers[0], pres=a.querySelectorAll('pre');
    const copy=a.cloneNode(true);copy.querySelectorAll('pre').forEach(e=>e.remove());
    const codes=pres.length===1?pres[0].querySelectorAll('code'):[];
    answer=pres.length===1&&codes.length===1&&!text(copy)?text(codes[0]):text(a);
    oversize=answer.length>65536;if(oversize)answer='';
}
return {url:location.href,editors:all(s.editor).length,
 model:models.length===1?label(models[0]):'',modelCount:models.length,
 ...(s.thinking?{thinking:thinking.length===1?label(thinking[0]):'',thinkCount:thinking.length}:{}),
 users:users.map(text),responses:responses.length,answer,oversize,
 attachments:all(s.attachment).map(label),uploading:all(s.upload_busy).length>0,
 send:all(s.send).length===1&&!all(s.send)[0].disabled&&all(s.send)[0].getAttribute('aria-disabled')!=='true',
 complete:response&&all(s.complete,response).length>0,stopped:all(s.stop).length===0,
 errors:all(s.error).map(text),
 login:all('a,button').some(e=>/^(sign in|log in|login)$/i.test(text(e))||
 /^(sign in|log in|login)$/i.test(e.getAttribute('aria-label')||'')),
 challenge:!!document.querySelector('#challenge-running,#challenge-form,[data-testid="challenge"]')};
""", self.selectors)

    def check_state(self, state):
        actual = urlsplit(state["url"])
        expected = urlsplit(self.site["url"])
        if actual.scheme != expected.scheme or actual.netloc != expected.netloc or state["login"]:
            raise AIError(f"{self.name}: sign in in the existing browser tab.", "browser_login")
        if state["challenge"]:
            raise AIError(f"{self.name}: the website requires account verification.", "browser_verification")
        for message in state["errors"]:
            if re.search(r"limit|quota|too many|try again later", message, re.I):
                raise AIError(f"{self.name}: usage limit reached.", "browser_limit")
            if re.search(r"error|failed|went wrong|couldn.t|unable|stopped unexpectedly", message, re.I):
                raise AIError(f"{self.name}: page or upload error.", "browser_error")

    def verify_model(self, state):
        if not isinstance(self.model_label, str) or not self.model_label.strip():
            raise AIError(f"Set web.sites.{self.name}.model_label to your selected vision model.", "browser_model")
        if state["modelCount"] != 1 or not re.search(
                r"(?<!\w)" + re.escape(self.model_label.strip()) + r"(?!\w)", state["model"], re.I):
            raise AIError(f"{self.name}: the selected model does not match the configured model label.", "browser_model")
        if self.thinking_label.strip():
            separate = bool(self.selectors.get("thinking"))
            selected = state.get("thinking", "") if separate else state["model"]
            if (separate and state.get("thinkCount", 0) != 1) or not re.search(
                    r"(?<!\w)" + re.escape(self.thinking_label.strip()) + r"(?!\w)", selected, re.I):
                raise AIError(f"{self.name}: the thinking level does not match the configured label.", "browser_model")

    def wait_state(self, page, condition):
        def poll():
            state = self.snapshot(page)
            self.check_state(state)
            return state if condition(state) else None
        return page.wait(poll)

    def prepare(self, page):
        starting_new = False
        if getattr(page, "reuse_page", False):
            state = self.snapshot(page)
            self.check_state(state)
            if state["users"] or state["responses"] or state["attachments"]:
                self.click(page, self.selectors.get("new_chat", '[data-seth-missing-new-chat]'))
                starting_new = True
        else:
            page.navigate(self.site["url"])
        state = self.wait_state(page, lambda s: s["editors"] and s["modelCount"] and (
            not starting_new or not (s["users"] or s["responses"]) and (
                self.name == "gemini" or not s["attachments"])))
        # Gemini can retain an unsent attachment even when New chat is clicked.
        # Clear that one draft explicitly; never submit it or duplicate an upload.
        if starting_new and self.name == "gemini" and state["attachments"]:
            if len(state["attachments"]) > 16:
                raise AIError(f"{self.name}: too many pending draft attachments.", "browser_layout")
            while state["attachments"]:
                count = len(state["attachments"])
                self.reveal_draft_remove(page)
                removed = dom(page, """
const buttons=all(args[0]);if(!buttons.length)return false;
buttons[0].click();return true;
""", self.selectors["remove_attachment"])
                if not removed:
                    raise AIError(f"{self.name}: pending attachment removal is unavailable.", "browser_layout")
                state = self.wait_state(page, lambda s: len(s["attachments"]) == count - 1)
            self.fill_composer(page, "")
            state = self.wait_state(page, lambda s: s["editors"] and s["modelCount"]
                                    and not (s["users"] or s["responses"] or s["attachments"]))
        if state["editors"] != 1 or state["users"] or state["responses"] or state["attachments"]:
            raise AIError(f"{self.name}: expected one empty chat and no existing attachment.", "browser_layout")
        self.verify_model(state)
        return state

    def reveal_draft_remove(self, page):
        selector = self.selectors["remove_attachment"]
        if dom(page, "return all(args[0]).length;", selector) != 0:
            return
        position = dom(page, """
const e=all(args[0]);if(!e.length)return null;
const b=e[0].getBoundingClientRect();
return b.width>0&&b.height>0?{x:b.x+b.width/2,y:b.y+b.height/2}:null;
""", self.selectors["attachment"])
        if position is None:
            raise AIError(f"{self.name}: pending draft attachment is not available.", "browser_layout")
        page.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "buttons": 0, **position})
        deadline = time.monotonic() + 3

        def revealed():
            if dom(page, "return all(args[0]).length;", selector):
                return True
            if time.monotonic() >= deadline:
                raise AIError(f"{self.name}: draft attachment removal did not become available.", "browser_layout")
            return False
        page.wait(revealed)

    def click(self, page, selector, matching_text=None):
        result = dom(page, """
const matches=all(args[0]).filter(e=>args[1]===null||text(e).toLowerCase()===args[1].toLowerCase());
if(matches.length!==1||matches[0].disabled||matches[0].getAttribute('aria-disabled')==='true')return false;
matches[0].click();return true;
""", selector, matching_text, user_gesture=self.name == "qwen")
        if not result:
            raise AIError(f"{self.name}: a required page control is missing, ambiguous, or disabled.", "browser_layout")

    def attach(self, page, image_path, previous=()):
        def select_input():
            return dom(page, """
const inputs=Array.from(document.querySelectorAll(args[0]));
if(inputs.length!==1)return null;
inputs[0].value='';
inputs[0].setAttribute('data-seth-upload',args[1]);return '[data-seth-upload="'+args[1]+'"]';
""", self.selectors["file"], uuid.uuid4().hex)
        selector = select_input()
        if not selector:
            self.click(page, self.selectors["upload"])
            upload_text = self.options.get("upload_text", "Upload files")
            def menu_ready():
                selected = select_input()
                if selected:
                    return selected
                ready = dom(page, "return all(args[0]).filter(e=>text(e).toLowerCase()===args[1].toLowerCase()).length===1;",
                            self.selectors["upload_item"], upload_text)
                return "menu" if ready else None
            selector = page.wait(menu_ready)
            if selector == "menu":
                self.click(page, self.selectors["upload_item"], upload_text)
                selector = page.wait(select_input)
        page.set_file(selector, image_path)
        self.wait_attachment(page, image_path, previous)

    def has_batch(self, state, paths):
        return len(state["attachments"]) == len(paths) and all(
            self.attachment_matches(label, path) for label, path in zip(state["attachments"], paths))

    def wait_attachment(self, page, image_path, previous=()):
        def ready(state):
            if previous and not state["uploading"] and state["send"] and any(
                    self.attachment_matches(label, image_path) for label in state["attachments"]):
                if not self.has_batch(state, (*previous, image_path)):
                    raise AIError(f"{self.name}: screenshot batch is missing sections or out of order; nothing sent.", "browser_layout")
            return self.has_batch(state, (*previous, image_path)) and not state["uploading"] and state["send"]
        state = self.wait_state(page, ready)
        self.verify_model(state)

    def attachment_matches(self, label, image_path):
        # Web image preprocessing may append six hex characters and transcode
        # PNG/JPEG previews. Keep the full original stem and bounded image suffix
        # rather than accepting a generic attachment or another question's file.
        name = re.escape(image_path.name)
        if self.name in ("gemini", "qwen") and image_path.suffix.lower() in (".png", ".jpg", ".jpeg"):
            name = re.escape(image_path.stem) + r"(?:_[0-9a-f]{6})?\.(?i:png|jpe?g)"
        return bool(re.search(r"(?<![\w.-])" + name + r"(?![\w.-])", label))

    def fill_composer(self, page, text):
        filled = dom(page, """
const inputs=all(args[0]);if(inputs.length!==1)return false;
const e=inputs[0];e.focus();
if(e.tagName==='TEXTAREA'||e.tagName==='INPUT'){
    const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto,'value').set.call(e,args[1]);
    e.dispatchEvent(new Event('input',{bubbles:true}));
}else{
    e.dispatchEvent(new InputEvent('beforeinput',{bubbles:true,inputType:'insertText',data:args[1]}));
    e.textContent=args[1];
    e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:args[1]}));
}
return true;
""", self.selectors["editor"], text)
        if not filled:
            raise AIError(f"{self.name}: prompt composer is missing or ambiguous.", "browser_layout")

    def exchange(self, page, image_path, prompt, report):
        paths = image_batch(image_path)
        self.prepare(page)
        request_id = "Seth request " + uuid.uuid4().hex
        self.fill_composer(page, request_id + "\n" + prompt)
        report(f"Uploading to {self.name}")
        for index, path in enumerate(paths):
            report(f"Uploading {index + 1}/{len(paths)} to {self.name}")
            self.attach(page, path, paths[:index])
        state = self.snapshot(page)
        self.check_state(state)
        self.verify_model(state)
        if state["users"] or state["responses"] or not self.has_batch(state, paths) or state["uploading"] or not state["send"]:
            raise AIError(f"{self.name}: page changed before submission.", "browser_layout")
        # Never retry a submission to this provider after an ambiguous failure.
        self.click(page, self.selectors["send"])
        report(f"Waiting for {self.name}")
        previous, stable_since = None, None

        def completed():
            nonlocal previous, stable_since
            state = self.snapshot(page)
            self.check_state(state)
            if len(state["users"]) > 1 or state["responses"] > 1:
                raise AIError(f"{self.name}: conversation changed during inference.", "browser_layout")
            if len(state["users"]) != 1 or state["responses"] != 1:
                return None
            if request_id not in state["users"][0]:
                raise AIError(f"{self.name}: the submitted prompt does not match this cycle.", "browser_layout")
            if state["oversize"]:
                raise AIError(f"{self.name}: answer exceeded the response size limit.", "browser_error")
            if not state["complete"] or not state["stopped"] or not state["answer"]:
                previous, stable_since = None, None
                return None
            if previous != state["answer"]:
                previous, stable_since = state["answer"], time.monotonic()
                return None
            if time.monotonic() - stable_since < 1:
                return None
            self.verify_model(state)
            return previous
        return page.wait(completed)


class QwenSiteAdapter(SiteAdapter):
    def prepare(self, page):
        if not getattr(page, "reuse_page", False):
            page.navigate(self.site["url"])
            page.reuse_page = True
        # Qwen first renders an anonymous shell, then loads the saved account.
        # Do not mistake that transient Log in button for a lost session.
        login_since = None

        def mounted():
            nonlocal login_since
            state = self.snapshot(page)
            if urlsplit(state["url"]).netloc != urlsplit(self.site["url"]).netloc or state["challenge"]:
                self.check_state(state)
            if state["login"]:
                if login_since is None:
                    login_since = time.monotonic()
                elif time.monotonic() - login_since >= 5:
                    self.check_state(state)
                return None
            login_since = None
            return state if state["editors"] and state["modelCount"] else None

        page.wait(mounted)
        return super().prepare(page)

    def _click_upload_control(self, page, selector, matching_text=None):
        # Qwen can mount/reset the composer before its upload controls. Retry
        # only the explicit no-click result; JS/protocol/connection failures
        # remain ambiguous and must propagate without another interaction.
        ready_until = time.monotonic() + 3

        def ready():
            try:
                self.click(page, selector, matching_text)
            except AIError as error:
                if error.code == "browser_layout" and time.monotonic() < ready_until:
                    return False
                raise
            return True

        page.wait(ready)

    def attach(self, page, image_path, previous=()):
        # The hidden input is always present, but Qwen arms its one-shot upload
        # handler only when Upload attachment is selected, on every upload.
        upload_text = self.options.get("upload_text", "Upload attachment")
        with page.intercept_file_chooser():
            self._click_upload_control(page, self.selectors["upload"])
            opened_at = time.monotonic()
            ready_until = opened_at + 3

            def choose_upload():
                nonlocal opened_at
                try:
                    self.click(page, self.selectors["upload_item"], upload_text)
                    return True
                except AIError as error:
                    # A missing target dispatched no click. New-chat focus or
                    # rendering can dismiss the menu; reopen only a closed menu.
                    if error.code != "browser_layout" or time.monotonic() >= ready_until:
                        raise
                    if time.monotonic() - opened_at >= .3 and dom(page, "return all('[role=menuitem]').length===0;"):
                        self._click_upload_control(page, self.selectors["upload"])
                        opened_at = time.monotonic()
                    return False

            page.wait(choose_upload)
            page.set_chooser_file(image_path)
            self.wait_attachment(page, image_path, previous)

    def exchange(self, page, image_path, prompt, report):
        raw = super().exchange(page, image_path, prompt, report)
        virtualized = dom(page, """
const answers=all(args[0]);if(answers.length!==1)return false;
const answer=answers[0];if(!answer.querySelector('.qwen-markdown-code-editor-viewport'))return false;
const blocks=answer.querySelectorAll('pre.qwen-markdown-code');
const outside=answer.cloneNode(true);outside.querySelectorAll('pre').forEach(e=>e.remove());
return blocks.length===1&&!text(outside)?'download':'ambiguous';
""", self.selectors["answer"])
        if not virtualized:
            return raw
        if virtualized != "download":
            raise AIError("qwen: the complete code answer is unavailable or ambiguous.", "browser_layout")
        before = self.snapshot(page)
        selector = self.selectors["answer"] + ' pre.qwen-markdown-code .qwen-markdown-code-header button[aria-label="Download"]'
        raw = page.download_text(lambda: self.click(page, selector))
        after = self.snapshot(page)
        self.check_state(after)
        self.verify_model(after)
        if (before["users"] != after["users"] or after["responses"] != 1
                or not after["complete"] or not after["stopped"]):
            raise AIError("qwen: conversation changed while reading the complete answer.", "browser_layout")
        return raw


class WebInferenceClient:
    def __init__(self, config=None, *, provider="qwen", stop_event=None, progress=None):
        if provider not in SITES:
            raise AIError("Select qwen or gemini.", "browser_model")
        self.config = config if config is not None else load_config()
        self.web = self.config.get("web", {})
        self.provider, self.stop_event, self.progress = provider, stop_event, progress

    @property
    def state_dir(self):
        return Path(self.web.get("state_dir", str(Path.home() / ".local/share/seth/web"))).expanduser().resolve()

    def adapter(self, provider):
        adapter_type = QwenSiteAdapter if provider == "qwen" else SiteAdapter
        return adapter_type(provider, self.web.get("sites", {}).get(provider, {}))

    def login_profile(self, provider):
        owner = self.web.get("sites", {}).get(provider, {}).get("browser_profile", provider)
        if owner not in SITES:
            raise AIError("browser_profile must name qwen or gemini.")
        return self.state_dir / owner / "native-profile"

    def native_state_file(self, provider):
        return self.state_dir / provider / "native-browser.json"

    def validate_config(self):
        timeout = self.web.get("timeout_seconds", 120)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 240:
            raise AIError("web.timeout_seconds must be greater than 0 and at most 240.")
        executable = find_chromium(self.web.get("chromium_executable_path"))
        if not executable or not Path(executable).is_file():
            raise AIError("Configure an installed Chromium/Chrome browser.", "browser_install")
        self.login_profile(self.provider)
        if not self.adapter(self.provider).model_label.strip():
            raise AIError(f"Set web.sites.{self.provider}.model_label.", "browser_model")

    @contextmanager
    def session_lock(self, provider):
        directory = self.state_dir / provider
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (directory / ".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AIError(f"{provider}: another operation is using this session.", "browser_busy") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _report(self, label):
        LOG.info("%s", label)
        if self.progress:
            self.progress(label)

    def _run(self, operation):
        self.validate_config()
        provider = self.provider
        with self.session_lock(provider):
            with Chromium(profile=self.login_profile(provider),
                          executable=self.web.get("chromium_executable_path"),
                          state_file=self.native_state_file(provider), keep_open=True,
                          require_existing=os.environ.get("SETH_MANAGED_BROWSER") == "1",
                          deadline=time.monotonic() + self.web.get("timeout_seconds", 120),
                          stop_event=self.stop_event) as browser:
                return operation(self.adapter(provider), browser)

    def analyze_screenshot(self, image_path, question_type):
        return self.analyze_screenshots([image_path], question_type)

    def analyze_screenshots(self, image_paths, question_type):
        paths = image_batch(image_paths)
        if len({path.name for path in paths}) != len(paths):
            raise AIError("Web screenshot filenames must be distinct.")
        for path in paths:
            screenshot_info(path)
        prompt = build_prompt(question_type, len(paths))
        if len(paths) > 1:
            prompt += "\nCapture order (top to bottom):\n" + "\n".join(
                f"{index}: {path.name}" for index, path in enumerate(paths, 1))
        # Preserve the single-image adapter call used by existing clients.
        images = paths if len(paths) > 1 else paths[0]
        return self._run(lambda adapter, page: adapter.exchange(page, images, prompt, self._report))
