"""Launch lifecycle tests use local HTTP, isolated profiles, and no live VM/API."""

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_service import BrowserManager, configured_providers, status_file
from chromium_runtime import Chromium, managed_browser
from inference import AIError
from cdp import write_private_json
from service_runtime import notify
import seth_cli
from web_inference import SITES


class LaunchTests(unittest.TestCase):
    def test_up_repeats_start_jobs_for_missing_children_even_when_target_is_active(self):
        with patch.object(seth_cli, "systemctl", return_value=subprocess.CompletedProcess([], 0)) as control, \
                patch.object(seth_cli, "collect_status", return_value={}), \
                patch.object(seth_cli, "print_status"), patch.object(seth_cli, "import_desktop_environment"):
            for _ in range(2):
                self.assertEqual(seth_cli.run_command("up", argparse.Namespace(), {}, {}), 0)
        self.assertEqual([call.args for call in control.call_args_list if call.args[0] == "start"],
                         [("start", *seth_cli.UNITS)] * 2)
        self.assertFalse(any(call.args[0] in ("stop", "restart") for call in control.call_args_list))

    def test_listener_restart_does_not_restart_browser_and_down_cancels_listener_first(self):
        with patch.object(seth_cli, "systemctl") as control, \
                patch.object(seth_cli, "collect_status", return_value={}), patch.object(seth_cli, "print_status"):
            seth_cli.run_command("restart", argparse.Namespace(component="listener"), {}, {})
            control.assert_called_once_with("restart", "seth-listener.service")
            control.reset_mock()
            seth_cli.run_command("down", argparse.Namespace(), {}, {})
            self.assertEqual([call.args for call in control.call_args_list], [
                ("stop", "seth-listener.service"), ("stop", "seth.target", "seth-browser.service")])

    def test_install_renders_this_checkout_and_enables_autostart_without_stopping_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(seth_cli, "config_home", return_value=directory / "config"), \
                    patch.object(seth_cli.Path, "home", return_value=directory), \
                    patch.object(seth_cli, "unit_state", return_value={"ActiveState": "active"}), \
                    patch.object(seth_cli, "systemctl") as control:
                self.assertEqual(seth_cli.install(seth_cli.ROOT / "config.json"), 0)
                self.assertTrue((directory / ".local/bin/seth").is_symlink())
                listener = (directory / "config/systemd/user/seth-listener.service").read_text()
                self.assertIn(f'"{seth_cli.ROOT / ".venv/bin/python"}"', listener)
                self.assertIn("Type=notify", listener)
                self.assertNotIn("@ROOT@", listener)
                self.assertTrue(seth_cli.installation()["restart_listener"])
            self.assertEqual([call.args for call in control.call_args_list], [
                ("daemon-reload",), ("enable", "seth.target")])

    def test_systemd_paths_escape_specifiers_and_exec_environment_expansion(self):
        path = '/tmp/a b/$HOME/%name/"quote"'
        self.assertEqual(seth_cli.systemd_argument(path), '"/tmp/a b/$$HOME/%%name/\\"quote\\""')
        self.assertEqual(seth_cli.systemd_argument(path, command=False), '/tmp/a b/$HOME/%%name/"quote"')
        with self.assertRaises(ValueError):
            seth_cli.systemd_argument("/tmp/path\nExecStart=/bad")

    def test_status_does_not_trust_old_manager_or_stale_cached_provider_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = {"web": {"state_dir": temporary, "sites": {"qwen": {}}}}
            write_private_json(status_file(config), {"updated_at": time.time(), "manager_pid": 12,
                               "providers": {"qwen": {"status": "ready"}}})
            with patch.object(seth_cli, "unit_state", return_value={"ActiveState": "active", "MainPID": "34"}):
                self.assertEqual(seth_cli.collect_status(config)["providers"]["qwen"]["status"], "starting")

    def test_managed_listener_refuses_to_launch_its_own_chromium(self):
        with tempfile.TemporaryDirectory() as temporary, patch("chromium_runtime.subprocess.Popen") as spawn:
            browser = Chromium(profile=temporary, keep_open=True, require_existing=True,
                               deadline=time.monotonic() + 5)
            with self.assertRaisesRegex(AIError, "seth up"):
                browser.__enter__()
            spawn.assert_not_called()

    def test_notification_is_sent_to_systemd_only_after_explicit_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "notify")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
                server.bind(path)
                server.settimeout(1)
                with patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                    notify("READY=1\nSTATUS=Ready")
                self.assertEqual(server.recv(1024), b"READY=1\nSTATUS=Ready")

    def test_doctor_reads_keyboard_capabilities_without_grabbing_or_reading_events(self):
        import evdev
        keyboard = SimpleNamespace(name="Physical keyboard", close=Mock(), capabilities=lambda: {
            evdev.ecodes.EV_KEY: [97, 32, 33, 18, 19, 16, 17, 34, 44]})
        status = {"services": {"listener": {"ActiveState": "active"}},
                  "providers": {"qwen": {"status": "ready", "detail": ""}},
                  "api": {"status": "reachable", "detail": ""}}
        with patch.object(seth_cli, "collect_status", return_value=status), \
                patch.object(seth_cli, "print_status"), patch.object(seth_cli.os, "access", return_value=True), \
                patch.object(evdev, "InputDevice", return_value=keyboard):
            self.assertEqual(seth_cli.doctor({"hotkey": {"devices": ["/dev/test-keyboard"]}}), 0)
        keyboard.close.assert_called_once()

    def test_manual_provider_choices_share_one_profile_and_manager_is_singleton(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = {"web": {"state_dir": temporary, "sites": {
                "gemini": {"browser_profile": "qwen"}, "qwen": {}}}}
            self.assertEqual(configured_providers(config), ["qwen", "gemini"])
            manager = BrowserManager(config)
            self.assertEqual(len(manager.groups), 1)
            with manager.lock():
                with self.assertRaisesRegex(AIError, "already running"):
                    with BrowserManager(config).lock():
                        self.fail("Second manager acquired the lock")


class APICheckTests(unittest.TestCase):
    def test_connectivity_check_uses_head_without_api_credentials_or_inference(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            code = 401
            def log_message(self, *args):
                pass
            def do_HEAD(self):
                requests.append((self.command, self.path, self.headers.get("Authorization")))
                self.send_response(self.code)
                self.end_headers()
            def do_POST(self):
                requests.append((self.command, self.path, self.headers.get("Authorization")))
                self.send_response(500)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = {"api": {"base_url": f"http://127.0.0.1:{server.server_port}/v1",
                              "api_key": "must-not-be-transmitted", "model": "test"}}
            self.assertEqual(seth_cli.api_status(config, probe=True)["status"], "reachable")
            Handler.code = 530
            self.assertEqual(seth_cli.api_status(config, probe=True)["status"], "unavailable")
            self.assertEqual(requests, [("HEAD", "/v1/chat/completions", None)] * 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


@unittest.skipUnless(os.environ.get("SETH_CHROMIUM_TESTS") == "1", "Set SETH_CHROMIUM_TESTS=1 for isolated browser lifecycle tests")
class BrowserLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                requests.append(self.command)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Set-Cookie", "SID=local-launch-test; Path=/")
                self.end_headers()
                self.wfile.write(b'<div id="model">Pro</div><div id="editor" contenteditable="true"></div>')
            def do_POST(self):
                requests.append(self.command)
                self.send_response(500)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        sites = {provider: SITES[provider] | {"url": f"http://127.0.0.1:{self.server.server_port}/{provider}",
                                            "login_domains": ("127.0.0.1",)} for provider in ("gemini", "qwen")}
        self.site_patch = patch.dict(SITES, sites)
        self.site_patch.start()
        self.addCleanup(self.site_patch.stop)
        options = {"model_label": "Pro",
                   "selectors": {"model": "#model", "editor": "#editor"}}
        self.config = {"web": {"state_dir": self.temporary.name, "sites": {
            "gemini": options | {"browser_profile": "qwen"}, "qwen": options.copy()}}}
        self.manager = BrowserManager(self.config, headless=True)
        self.addCleanup(self.manager.close)

    def ready(self):
        until = time.monotonic() + 12
        while True:
            snapshot = self.manager.refresh()
            if all(p["status"] == "ready" for p in snapshot["providers"].values()):
                return snapshot
            if time.monotonic() >= until:
                self.fail(f"Provider readiness timed out: {snapshot}")
            time.sleep(0.1)

    @contextmanager
    def page(self, provider):
        with Chromium(profile=self.manager.client.login_profile(provider),
                      state_file=self.manager.client.native_state_file(provider), keep_open=True,
                      require_existing=True, deadline=time.monotonic() + 8) as browser:
            yield browser

    def test_stale_records_recover_and_repeated_launch_preserves_both_tabs_and_drafts(self):
        for provider in ("gemini", "qwen"):
            write_private_json(self.manager.client.native_state_file(provider), {
                "pid": 2**30, "port": 1, "profile": str(self.manager.client.login_profile(provider)), "target_id": "stale"})
        self.ready()
        states = {p: json.loads(self.manager.client.native_state_file(p).read_text()) for p in ("gemini", "qwen")}
        self.assertEqual(states["gemini"]["pid"], states["qwen"]["pid"])
        self.assertNotEqual(states["gemini"]["target_id"], states["qwen"]["target_id"])
        with self.page("gemini") as page:
            page.evaluate("window.sessionProof='preserved';document.querySelector('#editor').textContent='unsent draft'")
            pages = page.call("Target.getTargets")["targetInfos"]
            self.assertEqual(len([p for p in pages if p["type"] == "page"]), 2)
        self.ready()
        with self.page("gemini") as page:
            self.assertEqual(page.evaluate("window.sessionProof"), "preserved")
            self.assertEqual(page.evaluate("document.querySelector('#editor').textContent"), "unsent draft")
        for provider, state in states.items():
            self.assertEqual(json.loads(self.manager.client.native_state_file(provider).read_text()), state)
            self.assertEqual(self.manager.client.native_state_file(provider).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("POST", self.requests)

    def test_closed_tab_is_repaired_without_restarting_shared_browser_or_other_tab(self):
        self.ready()
        old = json.loads(self.manager.client.native_state_file("qwen").read_text())
        with self.page("gemini") as page:
            page.evaluate("window.sessionProof='preserved'")
            page.call("Target.closeTarget", {"targetId": old["target_id"]})
        self.ready()
        current = json.loads(self.manager.client.native_state_file("qwen").read_text())
        self.assertEqual(current["pid"], old["pid"])
        self.assertNotEqual(current["target_id"], old["target_id"])
        with self.page("gemini") as page:
            self.assertEqual(page.evaluate("window.sessionProof"), "preserved")

    def test_closed_window_is_reopened_and_reaped(self):
        self.ready()
        old = json.loads(self.manager.client.native_state_file("qwen").read_text())
        with self.page("qwen") as page:
            page.call("Browser.close")
        until = time.monotonic() + 5
        while managed_browser(self.manager.client.native_state_file("qwen"), self.manager.client.login_profile("qwen")):
            if time.monotonic() >= until:
                self.fail("Browser did not close")
            time.sleep(0.05)
        self.ready()
        current = json.loads(self.manager.client.native_state_file("qwen").read_text())
        self.assertNotEqual(current["pid"], old["pid"])
        self.assertFalse(Path(f"/proc/{old['pid']}").exists())

    def test_new_manager_adopts_live_browser_and_login_failure_is_isolated(self):
        self.ready()
        old = json.loads(self.manager.client.native_state_file("qwen").read_text())
        with self.page("gemini") as page:
            page.evaluate("window.sessionProof='preserved';const b=document.createElement('button');"
                          "b.textContent='Sign in';document.body.appendChild(b)")
        adopted = BrowserManager(self.config, headless=True)
        snapshot = adopted.refresh()
        self.assertEqual(snapshot["providers"]["gemini"]["status"], "needs login")
        self.assertEqual(snapshot["providers"]["qwen"]["status"], "ready")
        self.assertEqual(next(iter(snapshot["browsers"].values()))["pid"], old["pid"])
        self.assertFalse(adopted.owned_pids)
        with self.page("gemini") as page:
            self.assertEqual(page.evaluate("window.sessionProof"), "preserved")

    def test_shutdown_flushes_browser_and_clears_both_provider_endpoints(self):
        self.ready()
        self.manager.close()
        for provider in ("gemini", "qwen"):
            self.assertIsNone(managed_browser(self.manager.client.native_state_file(provider),
                                              self.manager.client.login_profile(provider)))
            self.assertFalse(self.manager.client.native_state_file(provider).exists())


if __name__ == "__main__":
    unittest.main()
