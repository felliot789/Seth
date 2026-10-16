"""Local sign-in helper for dedicated Chromium profiles and Lightpanda exports.

A normal installed Chromium browser handles interactive sign-in. Its dedicated
profile and loopback DevTools endpoint are used only to save this site's cookies.
The selected tab is handed off live when configured; otherwise the window closes
normally after the selected model matches configuration.
"""

import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

from inference import AIError
from lightpanda_runtime import write_private_json
from chromium_runtime import close_managed_browser, retain_process
from web_inference import SITES

LOG = logging.getLogger("seth")


def provider_cookies(provider, cookies):
    """Select only the explicitly requested site's domains, without logging values."""
    domains = SITES[provider]["cookie_domains"]
    return [cookie for cookie in cookies if any(
        cookie.get("domain", "").lstrip(".") == domain
        or cookie.get("domain", "").lstrip(".").endswith("." + domain)
        for domain in domains)]


def _capture(target, provider, model_selector, editor_selector):
    import websocket
    connection = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=2,
                                             suppress_origin=True, http_proxy_host=None)
    try:
        def call(identifier, method, params):
            connection.send(json.dumps({"id": identifier, "method": method, "params": params}))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = json.loads(connection.recv())
                if result.get("id") == identifier:
                    if result.get("error"):
                        return None
                    return result.get("result", {})
            return None
        result = call(1, "Network.getAllCookies", {})
        if not result:
            return None
        cookies = provider_cookies(provider, result.get("cookies", []))
        if not cookies:
            return None
        if provider == "gemini" and not any(c["name"] in ("SID", "__Secure-1PSID") for c in cookies):
            return None
        state = call(2, "Runtime.evaluate", {"expression": """(()=>{
const visible=e=>{for(let p=e;p&&p.nodeType===1;p=p.parentElement){const s=getComputedStyle(p);
 if(p.hidden||p.getAttribute('aria-hidden')==='true'||s.display==='none'||s.visibility==='hidden')return false;}return true;};
const login=Array.from(document.querySelectorAll('a,button')).filter(visible)
 .some(e=>/^(sign in|log in|login)$/i.test((e.textContent||'').trim())||
 /^(sign in|log in|login)$/i.test(e.getAttribute('aria-label')||''));
const model=Array.from(document.querySelectorAll(SELECTOR)).filter(visible);
return {login,editor:Array.from(document.querySelectorAll(EDITOR)).some(visible),
 model:model.length===1?[(model[0].textContent||'').trim(),model[0].getAttribute('aria-label')||'',
 ...Array.from(model[0].querySelectorAll('[aria-label]')).map(e=>e.getAttribute('aria-label'))].filter(Boolean).join(' '):null};
})()""".replace("SELECTOR", json.dumps(model_selector)).replace("EDITOR", json.dumps(editor_selector)), "returnByValue": True})
        if not state or state.get("exceptionDetails"):
            return None
        state = state.get("result", {}).get("value")
        if not isinstance(state, dict) or state.get("login") or not state.get("editor"):
            return None
        return cookies, state.get("model")
    finally:
        connection.close()


def _close_browser(target, process):
    """Flush the native profile through Chrome's normal shutdown path."""
    import websocket
    connection = None
    try:
        connection = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=1,
                                                 suppress_origin=True, http_proxy_host=None)
        connection.send(json.dumps({"id": 1, "method": "Browser.close"}))
        process.wait(timeout=3)
    except (OSError, websocket.WebSocketException, subprocess.TimeoutExpired):
        process.terminate()
        process.wait(timeout=3)
    finally:
        if connection is not None:
            connection.close()


def login(client, provider, executable=None, timeout=600, *, headless=False):
    try:
        import websocket
    except ImportError:
        raise AIError("Install requirements-browser.txt for the optional login helper.", "browser_install") from None
    executable = executable or client.web.get("chromium_executable_path") or shutil.which("chromium") or shutil.which("google-chrome")
    if not executable or not Path(executable).is_file():
        raise AIError("The optional login helper needs an installed Chromium/Chrome browser. Alternatively use --import-cookies.",
                      "browser_install")
    profile = client.login_profile(provider)
    with client.session_lock(provider):
        close_managed_browser(client.native_state_file(provider), profile)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    lock = profile / "SingletonLock"
    if lock.is_symlink():
        try:
            host, pid = os.readlink(lock).rsplit("-", 1)
            if host != socket.gethostname():
                raise AIError("Close the dedicated login browser before starting another login.", "browser_busy")
            os.kill(int(pid), 0)
        except ProcessLookupError:
            pass  # Chromium recovers its own abandoned lock.
        except (ValueError, PermissionError):
            raise AIError("The dedicated login profile is already in use.", "browser_busy") from None
        else:
            raise AIError("Close the dedicated login browser before starting another login.", "browser_busy")
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    command = [executable, "--user-data-dir=" + str(profile), "--no-first-run",
               "--no-default-browser-check", "--disable-background-mode", "--new-window",
               "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(port), SITES[provider]["url"]]
    if headless:
        command.insert(1, "--headless=new")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    with client.session_lock(provider), tempfile.TemporaryDirectory(prefix="login-", dir=profile.parent) as temporary:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        persistent = client.adapter(provider).options.get("keep_browser_open", False)
        LOG.info("Sign into %s and select the configured vision model. %s", provider,
                 "Keep this dedicated window open after sign-in." if persistent else
                 "The window closes after its session is saved.")
        LOG.info("Session cookies will be transferred locally. Do not paste credentials into chat.")
        captured, selected_model, announced, ready_since = None, None, False, None
        kept_open = False
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise AIError("Sign-in timed out. Run the login helper again when ready.", "browser_login")
                try:
                    with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                        targets = json.load(response)
                    if captured is not None and not any(t.get("type") == "page" for t in targets):
                        process.terminate()
                        continue
                    origin = urlsplit(SITES[provider]["url"]).netloc
                    for target in targets:
                        endpoint = urlsplit(target.get("webSocketDebuggerUrl", ""))
                        if (target.get("type") != "page" or urlsplit(target.get("url", "")).netloc != origin
                                or endpoint.scheme != "ws" or endpoint.hostname not in ("127.0.0.1", "localhost")
                                or endpoint.port != port):
                            continue
                        selectors = client.adapter(provider).selectors
                        result = _capture(target, provider, selectors["model"], selectors["editor"])
                        if result:
                            cookies, model = result
                            captured = Path(temporary) / "cookies.json"
                            write_private_json(captured, cookies)
                            # Save immediately while holding this session's lock.
                            # An interrupted helper must not lose a valid login.
                            write_private_json(client.cookie_file(provider), cookies)
                            selected_model = model
                            if not announced:
                                LOG.info("Signed-in session saved locally. Checking the selected model.")
                                announced = True
                            expected = client.adapter(provider).model_label.strip()
                            if expected and model and re.search(r"(?<!\w)" + re.escape(expected) + r"(?!\w)", model, re.I):
                                if ready_since is None:
                                    ready_since = time.monotonic()
                                elif time.monotonic() - ready_since >= 2:
                                    if client.adapter(provider).options.get("keep_browser_open", False):
                                        write_private_json(client.native_state_file(provider), {"pid": process.pid,
                                            "port": port, "profile": str(profile.resolve()), "target_id": target["id"]})
                                        kept_open = True
                                        retain_process(process)
                                        LOG.info("Signed-in browser is ready. Keep its dedicated window open; Seth reconnects automatically.")
                                    else:
                                        _close_browser(target, process)
                                    break
                            else:
                                ready_since = None
                except (OSError, ValueError, websocket.WebSocketException):
                    pass  # Navigation/closing a tab can briefly interrupt DevTools.
                if kept_open:
                    break
                time.sleep(0.75)
            if captured is None:
                raise AIError("The login window closed before a signed-in session was captured. Run --login again.",
                              "browser_login")
            # The session lock is already held here, so write the filtered file
            # directly rather than reacquiring it through import_cookies.
            cookies = provider_cookies(provider, json.loads(captured.read_text()))
            write_private_json(client.cookie_file(provider), cookies)
            return {"status": "session_imported", "provider": provider,
                    "model_label": selected_model, "cookies_imported": len(cookies)}
        finally:
            if not kept_open and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
