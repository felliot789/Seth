"""Browser screenshot inference with per-site adapters and bounded fallback."""

import argparse
from contextlib import contextmanager
import fcntl
import http.cookiejar
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

from chromium_runtime import Chromium, close_managed_browser, find_chromium
from inference import AIError, DecisionClient, build_prompt, screenshot_size
from lightpanda_runtime import DEFAULT_BINARY, Lightpanda, install_lightpanda, write_private_json
from settings import load_config

LOG = logging.getLogger("seth")
SITES = {
    "gemini": {
        "url": "https://gemini.google.com/app",
        "cookie_domains": ("google.com", "googleusercontent.com"),
        "model_label": "Pro",
        "selectors": {
            "editor": 'rich-textarea [contenteditable="true"][role="textbox"]',
            "model": '[data-test-id="bard-mode-menu-button"]',
            "file": 'input[type="file"][accept*="image"]',
            "upload": 'button[aria-label="Upload & tools"], button[aria-label="Upload and tools"], button[aria-label="Open upload file menu"]',
            "upload_item": '[role="menuitem"]',
            "attachment": 'uploader-file-preview',
            "upload_busy": 'uploader-file-preview [role="progressbar"], uploader-file-preview mat-spinner, uploader-file-preview mat-progress-spinner',
            "send": 'button[aria-label="Send message"], button[aria-label="Submit"]',
            "user": 'user-query',
            "response": 'model-response',
            "answer": '.markdown',
            "complete": 'button[aria-label="Copy"], button[aria-label="Copy response"]',
            "stop": 'button[aria-label="Stop response"], button[aria-label="Stop generating"]',
            "error": '[role="alert"], .error-message',
            "new_chat": 'a[aria-label="New chat"], button[aria-label="New chat"]',
        },
    },
    # These adapters are available for account-specific configuration and testing.
    # They are not enabled by default until a model label and cookies are supplied.
    "chatgpt": {
        "url": "https://chatgpt.com/",
        "cookie_domains": ("chatgpt.com", "openai.com"),
        "model_label": "",
        "selectors": {
            "editor": '#prompt-textarea, #mobile-composer-prompt',
            "model": '[data-testid="model-switcher-dropdown-button"]',
            "file": 'input[type="file"][accept*="image"], #octane-mobile-composer-photos-input',
            "upload": 'button[aria-label="Add files and more"], button[aria-label="Add photos & files"]',
            "upload_item": '[role="menuitem"]',
            "attachment": '[data-testid="attachment-preview"]',
            "upload_busy": '[data-testid="attachment-preview"] [role="progressbar"]',
            "send": 'button[data-testid="send-button"], button[aria-label="Send message"]',
            "user": '[data-message-author-role="user"]',
            "response": 'article[data-turn="assistant"]',
            "answer": '.markdown',
            "complete": 'button[data-testid="copy-turn-action-button"]',
            "stop": 'button[data-testid="stop-button"], button[aria-label="Stop streaming"]',
            "error": '[role="alert"]',
        },
    },
    "qwen": {
        "url": "https://chat.qwen.ai/",
        "cookie_domains": ("qwen.ai", "qwenlm.ai"),
        "model_label": "",
        "selectors": {
            "editor": 'textarea, [contenteditable="true"][role="textbox"]',
            "model": '[data-testid="model-selector"]',
            "file": 'input[type="file"]',
            "upload": 'button[aria-label="Upload files"]',
            "upload_item": '[role="menuitem"]',
            "attachment": '[data-testid="attachment-preview"]',
            "upload_busy": '[data-testid="attachment-preview"] [role="progressbar"]',
            "send": 'button[aria-label="Send message"]',
            "user": '[data-message-author-role="user"]',
            "response": '[data-message-author-role="assistant"]',
            "answer": '.markdown',
            "complete": 'button[aria-label="Copy"]',
            "stop": 'button[aria-label="Stop generating"]',
            "error": '[role="alert"]',
        },
    },
}

# Lightpanda computes styles but does not provide desktop layout geometry.
# Treat elements hidden by their own or ancestor styles as unavailable.
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


def dom(page, body, *arguments):
    return page.evaluate("((args)=>{" + DOM_HELPERS + body + "})(" + json.dumps(arguments) + ")")


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
            raise AIError(f"{self.name}: sign in and import a fresh local session.", "browser_login")
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
        if self.thinking_label.strip() and not re.search(
                r"(?<!\w)" + re.escape(self.thinking_label.strip()) + r"(?!\w)", state["model"], re.I):
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
            if state["users"] or state["responses"]:
                self.click(page, self.selectors.get("new_chat", '[data-seth-missing-new-chat]'))
                starting_new = True
        else:
            page.navigate(self.site["url"])
        state = self.wait_state(page, lambda s: s["editors"] and s["modelCount"] and (
            not starting_new or not (s["users"] or s["responses"])))
        if state["editors"] != 1 or state["users"] or state["responses"] or state["attachments"]:
            raise AIError(f"{self.name}: expected one empty chat and no existing attachment.", "browser_layout")
        self.verify_model(state)
        return state

    def click(self, page, selector, matching_text=None):
        result = dom(page, """
const matches=all(args[0]).filter(e=>args[1]===null||text(e).toLowerCase()===args[1].toLowerCase());
if(matches.length!==1||matches[0].disabled||matches[0].getAttribute('aria-disabled')==='true')return false;
matches[0].click();return true;
""", selector, matching_text)
        if not result:
            raise AIError(f"{self.name}: a required page control is missing, ambiguous, or disabled.", "browser_layout")

    def attach(self, page, image_path):
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
        state = self.wait_state(page, lambda s: len(s["attachments"]) == 1
                                and self.attachment_matches(s["attachments"][0], image_path)
                                and not s["uploading"] and s["send"])
        self.verify_model(state)

    def attachment_matches(self, label, image_path):
        # Gemini's image preprocessing adds six hexadecimal characters before the extension.
        # Require the complete original stem and extension, not an arbitrary image.
        name = re.escape(image_path.name)
        if self.name == "gemini":
            name = re.escape(image_path.stem) + r"(?:_[0-9a-f]{6})?" + re.escape(image_path.suffix)
        return bool(re.search(r"(?<![\w.-])" + name + r"(?![\w.-])", label))

    def exchange(self, page, image_path, prompt, report):
        self.prepare(page)
        request_id = "Seth request " + uuid.uuid4().hex
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
""", self.selectors["editor"], request_id + "\n" + prompt)
        if not filled:
            raise AIError(f"{self.name}: prompt composer is missing or ambiguous.", "browser_layout")
        report(f"Uploading to {self.name}")
        self.attach(page, image_path)
        state = self.snapshot(page)
        self.check_state(state)
        self.verify_model(state)
        if state["users"] or state["responses"] or len(state["attachments"]) != 1 or not state["send"]:
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


class WebInferenceClient(DecisionClient):
    def __init__(self, config=None, *, stop_event=None, progress=None):
        self.config = config if config is not None else load_config()
        self.web = self.config.get("web", {})
        self.stop_event = stop_event
        self.progress = progress
        self.last_provider = None
        self.last_attempts = []

    @property
    def state_dir(self):
        return Path(self.web.get("state_dir", str(Path.home() / ".local/share/seth/web"))).expanduser().resolve()

    def cookie_file(self, provider):
        if provider not in SITES:
            raise AIError("Unknown web inference provider.")
        return self.state_dir / provider / "cookies.json"

    def adapter(self, provider):
        return SiteAdapter(provider, self.web.get("sites", {}).get(provider, {}))

    def engine(self, provider):
        return self.web.get("sites", {}).get(provider, {}).get("engine", self.web.get("engine", "lightpanda"))

    def login_profile(self, provider):
        return self.state_dir / provider / "native-profile"

    def native_state_file(self, provider):
        return self.state_dir / provider / "native-browser.json"

    def validate_config(self):
        self.last_provider, self.last_attempts = None, []
        providers = self.web.get("providers", ["gemini"])
        if (not isinstance(providers, list) or not providers or any(not isinstance(p, str) or p not in SITES for p in providers)
                or len(set(providers)) != len(providers)):
            raise AIError("web.providers must be a unique ordered list of gemini, chatgpt, or qwen.")
        for setting, default in (("timeout_seconds", 240), ("attempt_timeout_seconds", 150)):
            value = self.web.get(setting, default)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
                raise AIError(f"web.{setting} must be greater than 0 and at most 300.")
        if not isinstance(self.web.get("sites", {}), dict):
            raise AIError("web.sites must be an object.")
        for provider in providers:
            options = self.web.get("sites", {}).get(provider, {})
            if not isinstance(options, dict):
                raise AIError(f"web.sites.{provider} must be an object.")
            if self.engine(provider) not in ("lightpanda", "chromium"):
                raise AIError(f"web.sites.{provider}.engine must be lightpanda or chromium.")
            if not isinstance(options.get("model_label", SITES[provider]["model_label"]), str):
                raise AIError(f"web.sites.{provider}.model_label must be a string.")
            if not isinstance(options.get("thinking_label", ""), str):
                raise AIError(f"web.sites.{provider}.thinking_label must be a string.")
            if type(options.get("keep_browser_open", False)) is not bool:
                raise AIError(f"web.sites.{provider}.keep_browser_open must be true or false.")
            overrides = options.get("selectors", {})
            if (not isinstance(overrides, dict) or set(overrides) - set(SITES[provider]["selectors"])
                    or any(not isinstance(s, str) or not s.strip() for s in overrides.values())):
                raise AIError(f"web.sites.{provider}.selectors contains an invalid selector override.")
        configured = [p for p in providers if self.adapter(p).model_label.strip()]
        if any(self.engine(p) == "lightpanda" for p in configured):
            executable = Path(self.web.get("executable_path", str(DEFAULT_BINARY))).expanduser()
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise AIError("Install Lightpanda: .venv/bin/python web_inference.py --install", "browser_install")
        if any(self.engine(p) == "chromium" for p in configured):
            executable = find_chromium(self.web.get("chromium_executable_path"))
            if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
                raise AIError("Configure an installed Chromium/Chrome browser for the web backend.", "browser_install")
        try:
            import websocket  # noqa: F401 -- optional for the API backend
        except ImportError:
            raise AIError("Install requirements-browser.txt for the Lightpanda controller.", "browser_install") from None

    @contextmanager
    def session_lock(self, provider):
        directory = self.cookie_file(provider).parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        with (directory / ".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AIError(f"{provider}: another process is using this session.", "browser_busy") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def import_cookies(self, provider, source):
        source = Path(source).expanduser()
        try:
            raw = source.read_text()
            if raw.lstrip().startswith(("[", "{")):
                records = json.loads(raw)
                if isinstance(records, dict):
                    records = records.get("cookies")
            else:
                jar = http.cookiejar.MozillaCookieJar(str(source))
                jar.load(ignore_discard=True, ignore_expires=True)
                records = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                            "secure": c.secure, "expires": c.expires or -1,
                            "httpOnly": c.has_nonstandard_attr("HTTPOnly")} for c in jar]
            if not isinstance(records, list):
                raise ValueError
            domains = SITES[provider]["cookie_domains"]
            selected = []
            for record in records:
                if not isinstance(record, dict) or not all(isinstance(record.get(k), str) for k in ("name", "value")):
                    raise ValueError
                domain = record.get("domain", urlsplit(record.get("url", "")).hostname or "").lstrip(".")
                if any(domain == d or domain.endswith("." + d) for d in domains):
                    selected.append(record)
            if not selected:
                raise ValueError
        except (ValueError, TypeError, KeyError, http.cookiejar.LoadError):
            raise AIError("Cookie file must be CDP JSON, Playwright storage state, or Netscape format with cookies for the selected site.") from None
        with self.session_lock(provider):
            write_private_json(self.cookie_file(provider), selected)
        return {"provider": provider, "cookies_imported": len(selected), "path": str(self.cookie_file(provider))}

    def _report(self, label):
        LOG.info("%s", label)
        if self.progress:
            self.progress(label)

    def _run(self, provider, deadline, operation):
        with self.session_lock(provider):
            options = {"deadline": deadline, "stop_event": self.stop_event, "cookies": self.cookie_file(provider)}
            runtime = (Chromium(executable=self.web.get("chromium_executable_path"),
                                profile=self.login_profile(provider),
                                state_file=self.native_state_file(provider),
                                keep_open=self.adapter(provider).options.get("keep_browser_open", False),
                                **(options | {"cookies": None}))
                       if self.engine(provider) == "chromium" else
                       Lightpanda(executable=self.web.get("executable_path"), **options))
            with runtime as browser:
                result = operation(self.adapter(provider), browser)
                # A failed/anonymous page must not replace a working login export.
                # Refresh cookies only after the operation succeeds, and never let
                # cleanup failure mask a complete inference result.
                try:
                    browser.save_cookies(self.cookie_file(provider))
                except AIError:
                    LOG.info("%s: kept previous session cookies", provider)
                return result

    def check(self, provider):
        self.validate_config()
        deadline = time.monotonic() + min(45, self.web.get("timeout_seconds", 240))
        state = self._run(provider, deadline, lambda adapter, page: adapter.prepare(page))
        return {"status": "ready", "provider": provider, "engine": self.engine(provider), "model": state["model"]}

    def analyze_screenshot(self, image_path, prompt=None, response_format=None):
        self.validate_config()
        image_path = Path(image_path).resolve()
        width, height = screenshot_size(image_path)
        deadline = time.monotonic() + self.web.get("timeout_seconds", 240)
        attempted_errors = []
        for provider in self.web.get("providers", ["gemini"]):
            if self.stop_event is not None and self.stop_event.is_set():
                raise AIError("Web inference cancelled.", "cancelled")
            if time.monotonic() >= deadline:
                break
            if not self.cookie_file(provider).is_file():
                self.last_attempts.append({"provider": provider, "error_code": "browser_login"})
                continue
            if not self.adapter(provider).model_label.strip():
                self.last_attempts.append({"provider": provider, "error_code": "browser_model"})
                continue
            self._report(f"Opening {provider}")
            attempt_deadline = min(deadline, time.monotonic() + self.web.get("attempt_timeout_seconds", 150))
            try:
                raw = self._run(provider, attempt_deadline, lambda adapter, page: adapter.exchange(
                    page, image_path, prompt or build_prompt(width, height), self._report))
                self.parse_coordinates(raw, width, height)
                self.last_provider = provider
                LOG.info("Web inference completed using %s", provider)
                return raw
            except AIError as error:
                if error.code == "cancelled":
                    raise
                self.last_attempts.append({"provider": provider, "error_code": error.code})
                attempted_errors.append(error.code)
                LOG.info("%s attempt stopped (%s)", provider, error.code)
        if time.monotonic() >= deadline:
            raise AIError("Web inference exhausted its total time budget.", "browser_timeout")
        codes = set(attempted_errors) or {attempt["error_code"] for attempt in self.last_attempts}
        code = next(iter(codes)) if len(codes) == 1 else "browser_unavailable"
        raise AIError("No configured web provider returned a complete, valid decision. Check local sign-in sessions, model labels, and page compatibility.", code)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--provider", choices=tuple(SITES), default="gemini")
    parser.add_argument("--login-browser", type=Path, help="Installed Chromium/Chrome executable for the optional login helper")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--install", action="store_true", help="Install the official checksum-verified Lightpanda binary")
    modes.add_argument("--import-cookies", type=Path, help="Import a local cookie export; never paste cookies into chat")
    modes.add_argument("--login", action="store_true", help="Sign in interactively using an installed browser and transfer the session locally")
    modes.add_argument("--close-browser", action="store_true", help="Close this provider's managed Chromium session normally")
    modes.add_argument("--check", action="store_true", help="Check session, model and empty chat without uploading")
    modes.add_argument("--image", type=Path, help="Upload a PNG and print a validated decision; never click the VM")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        if args.install:
            result = install_lightpanda()
        else:
            config = load_config(args.config)
            client = WebInferenceClient(config)
            if args.close_browser:
                with client.session_lock(args.provider):
                    close_managed_browser(client.native_state_file(args.provider), client.login_profile(args.provider))
                result = {"status": "browser_closed", "provider": args.provider}
            elif args.login:
                from web_login import login
                result = login(client, args.provider, executable=str(args.login_browser) if args.login_browser else None)
            elif args.import_cookies:
                result = client.import_cookies(args.provider, args.import_cookies)
            elif args.check:
                result = client.check(args.provider)
            else:
                # A CLI probe addresses exactly the requested provider. The main
                # Seth engine uses the full configured fallback list.
                config.setdefault("web", {})["providers"] = [args.provider]
                raw = client.analyze_screenshot(args.image)
                result = {"status": "dry_run", "provider": client.last_provider,
                          "decision": client.parse_coordinates(raw, *screenshot_size(args.image))}
        print(json.dumps(result))
        return 0
    except (AIError, OSError, ValueError, TypeError) as error:
        LOG.error("%s", error)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
