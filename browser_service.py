"""Own persistent Chromium processes and inspect provider tabs without submitting."""

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from chromium_runtime import Chromium, close_managed_browser, managed_browser, reap_processes
from inference import AIError
from cdp import write_private_json
from service_runtime import notify
from settings import load_config
from web_inference import SITES, WebInferenceClient

LOG = logging.getLogger("seth")


def configured_providers(config):
    """Keep only the two manually selected web providers."""
    sites = config.get("web", {}).get("sites", {})
    return [provider for provider in ("qwen", "gemini") if provider in sites]


def status_file(config):
    return WebInferenceClient(config).state_dir / "service-status.json"


def endpoint(state):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{state['port']}/json/version", timeout=2) as response:
        address = urlsplit(json.load(response)["webSocketDebuggerUrl"])
    if (address.scheme != "ws" or address.hostname not in ("127.0.0.1", "localhost")
            or address.port != state["port"] or not address.path.startswith("/devtools/browser/")):
        raise AIError("Invalid local browser endpoint.", "browser_start")
    return f"ws://127.0.0.1:{state['port']}{address.path}"


def matches_site(provider, url):
    host = urlsplit(url).hostname or ""
    expected = urlsplit(SITES[provider]["url"]).hostname
    return host == expected or any(host == domain or host.endswith("." + domain)
                                   for domain in SITES[provider]["login_domains"])


class BrowserManager:
    def __init__(self, config, *, stop_event=None, headless=False):
        self.config = config
        self.client = WebInferenceClient(config)
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.headless = headless
        self.groups = {}
        self.browsers = {}
        self.owned_pids = set()
        self.providers = {}
        for provider in configured_providers(config):
            self.groups.setdefault(self.client.login_profile(provider), []).append(provider)

    @contextmanager
    def lock(self):
        self.client.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.client.state_dir / ".browser-manager.lock").open("a") as stream:
            os.chmod(stream.name, 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AIError("Another Seth browser manager is already running.", "browser_busy") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _tab(self, browser, provider, pages, state, primary, used):
        saved = managed_browser(self.client.native_state_file(provider), self.client.login_profile(provider))
        reserved = {record["target_id"] for other in SITES if other != provider
                    if (record := managed_browser(self.client.native_state_file(other),
                                                  self.client.login_profile(other)))}
        target = next((p for p in pages if saved and p["targetId"] == saved.get("target_id")
                       and p["targetId"] not in used and (saved.get("owner") == "seth-browser.service"
                       or matches_site(provider, p.get("url", "")))), None)
        if target is None:
            target = next((p for p in pages if p["targetId"] not in used
                           and p["targetId"] not in reserved
                           and matches_site(provider, p.get("url", ""))), None)
        if target is None and provider == primary:
            target = next((p for p in pages if p["targetId"] == state.get("target_id")
                           and p.get("url") == "about:blank" and p["targetId"] not in used), None)
            if target is not None:
                session = browser.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})["sessionId"]
                browser.session = session
                try:
                    browser.call("Page.navigate", {"url": SITES[provider]["url"]})
                finally:
                    browser.session = None
                    browser.call("Target.detachFromTarget", {"sessionId": session})
        if target is None:
            target = {"targetId": browser.call("Target.createTarget", {
                "url": SITES[provider]["url"], "background": True})["targetId"]}
        used.add(target["targetId"])
        record = {k: state[k] for k in ("pid", "port", "profile")}
        record.update(target_id=target["targetId"], owner="seth-browser.service")
        if saved != record:
            write_private_json(self.client.native_state_file(provider), record)
        return record

    def _inspect(self, browser, provider, state):
        session = browser.call("Target.attachToTarget", {"targetId": state["target_id"], "flatten": True})["sessionId"]
        browser.session = session
        try:
            browser.call("Runtime.enable")
            browser._keep_page_active()
            adapter = self.client.adapter(provider)
            snapshot = adapter.snapshot(browser)
            adapter.check_state(snapshot)
            if snapshot["editors"] != 1 or snapshot["modelCount"] == 0:
                return {"status": "starting", "detail": "Waiting for the chat page"}
            adapter.verify_model(snapshot)
            return {"status": "ready", "detail": "Login, model and thinking selection verified"}
        except AIError as error:
            label = {"browser_login": "needs login", "browser_model": "wrong model",
                     "browser_verification": "needs verification", "browser_timeout": "starting"}.get(error.code, "failed")
            # Avoid page text, URLs, account information, and cookies in service status.
            detail = {"needs login": "Sign in in the existing browser tab",
                      "wrong model": "Select the model and thinking level from config.json",
                      "needs verification": "Complete verification in the existing browser tab",
                      "starting": "Waiting for the chat page"}.get(label, f"Browser check failed ({error.code})")
            return {"status": label, "detail": detail}
        finally:
            browser.session = None
            browser.call("Target.detachFromTarget", {"sessionId": session})

    def _refresh_group(self, profile, providers):
        with ExitStack() as locks:
            for provider in sorted(providers):
                locks.enter_context(self.client.session_lock(provider))
            primary = providers[0]
            state = next((record for provider in providers
                          if (record := managed_browser(self.client.native_state_file(provider), profile))), None)
            browser = Chromium(profile=profile, executable=self.client.web.get("chromium_executable_path"),
                               headless=self.headless, state_file=self.client.native_state_file(primary),
                               keep_open=True, deadline=time.monotonic() + 12, stop_event=self.stop_event)
            created = state is None
            try:
                if state:
                    browser._connect(endpoint(state))
                else:
                    browser.__enter__()
                    state = json.loads(self.client.native_state_file(primary).read_text())
                    self.owned_pids.add(state["pid"])
                    browser.session = None
                    LOG.info("Started shared browser for %s", ", ".join(providers))
                self.browsers[profile] = state
                pages = [p for p in browser.call("Target.getTargets")["targetInfos"] if p.get("type") == "page"]
                used = set()
                for provider in providers:
                    browser.deadline = time.monotonic() + 5
                    record = self._tab(browser, provider, pages, state, primary, used)
                    self.providers[provider] = self._inspect(browser, provider, record)
                if created:
                    for page in pages:
                        if page.get("url") == "about:blank" and page["targetId"] not in used:
                            browser.call("Target.closeTarget", {"targetId": page["targetId"]})
            finally:
                browser.close()

    def refresh(self):
        reap_processes()
        browsers = {}
        for profile, providers in self.groups.items():
            try:
                self._refresh_group(profile, providers)
                browsers[str(profile)] = {"status": "ready", "pid": self.browsers[profile]["pid"]}
            except Exception as error:
                if self.stop_event.is_set():
                    break
                busy = isinstance(error, AIError) and error.code == "browser_busy"
                label = "busy" if busy else "failed"
                detail = "Provider operation in progress" if busy else "Browser unavailable; inspect seth logs browser"
                for provider in providers:
                    self.providers[provider] = {"status": label, "detail": detail}
                browsers[str(profile)] = {"status": label}
                LOG.warning("Browser for %s: %s (%s)", ", ".join(providers), detail, type(error).__name__)
        snapshot = {"updated_at": time.time(), "manager_pid": os.getpid(),
                    "browsers": browsers, "providers": self.providers}
        write_private_json(status_file(self.config), snapshot)
        return snapshot

    def close(self, *, owned_only=False):
        for profile, providers in self.groups.items():
            try:
                with ExitStack() as locks:
                    for provider in sorted(providers):
                        locks.enter_context(self.client.session_lock(provider))
                    for provider in providers:
                        state = managed_browser(self.client.native_state_file(provider), profile)
                        if state and (not owned_only or state["pid"] in self.owned_pids):
                            close_managed_browser(self.client.native_state_file(provider), profile)
                            break
                    for provider in providers:
                        if not managed_browser(self.client.native_state_file(provider), profile):
                            self.client.native_state_file(provider).unlink(missing_ok=True)
            except (AIError, OSError) as error:
                LOG.warning("Could not close browser for %s (%s)", ", ".join(providers), type(error).__name__)
        write_private_json(status_file(self.config), {"updated_at": time.time(), "manager_pid": os.getpid(),
                           "browsers": {}, "providers": {p: {"status": "stopped", "detail": ""}
                                                         for group in self.groups.values() for p in group}})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        manager = BrowserManager(load_config(args.config), stop_event=stop)
        with manager.lock():
            started = False
            try:
                snapshot = manager.refresh()
                if any(b["status"] != "ready" for b in snapshot["browsers"].values()):
                    raise AIError("Browser startup failed; inspect seth logs browser.", "browser_start")
                notify("READY=1\nSTATUS=Browser manager ready; provider details in seth status")
                started = True
                while not stop.wait(5):
                    manager.refresh()
            finally:
                notify("STOPPING=1")
                manager.close(owned_only=not started and not stop.is_set())
        return 0
    except (AIError, OSError, ValueError, TypeError) as error:
        LOG.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
