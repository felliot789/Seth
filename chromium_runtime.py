"""Optional installed Chromium runtime using the same bounded, direct CDP client."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

from inference import AIError
from lightpanda_runtime import Lightpanda, write_private_json

_MANAGED_PROCESSES = {}


def retain_process(process):
    _MANAGED_PROCESSES[process.pid] = process


def find_chromium(executable=None):
    return str(Path(executable).expanduser()) if executable else (
        shutil.which("chromium") or shutil.which("google-chrome"))


class Chromium(Lightpanda):
    label = "Chromium"

    def __init__(self, *, profile, executable=None, headless=True, state_file=None, keep_open=False, **kwargs):
        self.profile = Path(profile).expanduser().resolve()
        self.headless = headless
        self.state_file = Path(state_file) if state_file else None
        self.keep_open = keep_open
        self.reuse_page = False
        self._file_chooser_node = None
        super().__init__(executable=find_chromium(executable) or "/nonexistent/chromium", **kwargs)

    def __enter__(self):
        import websocket
        self.remaining()
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise AIError("Configure web.chromium_executable_path to an installed Chromium/Chrome browser.",
                          "browser_install")
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile.chmod(0o700)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        command = [str(self.executable), "--user-data-dir=" + str(self.profile),
                   "--no-first-run", "--no-default-browser-check", "--disable-background-mode",
                   "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(port), "about:blank"]
        if self.headless:
            command.insert(1, "--headless=new")
        environment = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY")}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            if self.keep_open and self.state_file and self.state_file.is_file():
                state = managed_browser(self.state_file, self.profile)
                if state:
                    with opener.open(f"http://127.0.0.1:{state['port']}/json/version",
                                     timeout=min(2, self.remaining())) as response:
                        endpoint = json.load(response)["webSocketDebuggerUrl"]
                    self._connect(endpoint, state["target_id"])
                    self.reuse_page = True
                    return self
            self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            env=environment, start_new_session=True)
            startup_deadline = min(self.deadline, time.monotonic() + 10)
            while True:
                self.remaining()
                if self.process.poll() is not None:
                    raise AIError("Chromium exited during startup. Close the dedicated login window and retry.",
                                  "browser_start")
                try:
                    with opener.open(f"http://127.0.0.1:{port}/json/version",
                                     timeout=min(0.3, self.remaining())) as response:
                        endpoint = json.load(response)["webSocketDebuggerUrl"]
                    # Construct the address ourselves; only the browser's path is used.
                    path = urlsplit(endpoint).path
                    if not path.startswith("/devtools/browser/"):
                        raise ValueError
                    self.ws = websocket.create_connection(f"ws://127.0.0.1:{port}{path}", timeout=0.2,
                                                           suppress_origin=True, http_proxy_host=None)
                    break
                except (OSError, ValueError, KeyError, websocket.WebSocketException):
                    if time.monotonic() >= startup_deadline:
                        raise AIError("Chromium did not start its local control endpoint.", "browser_start") from None
                    self.pause(0.05)
            target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            self.call("Page.enable")
            self.call("Runtime.enable")
            self._keep_page_active()
            if self.keep_open and self.state_file:
                write_private_json(self.state_file, {"pid": self.process.pid, "port": port,
                    "profile": str(self.profile), "target_id": target})
            if self.cookies and self.cookies.is_file():
                records = json.loads(self.cookies.read_text())
                keys = {"name", "value", "url", "domain", "path", "secure", "httpOnly", "sameSite",
                        "expires", "priority", "sameParty", "sourceScheme", "sourcePort", "partitionKey"}
                cookies = [{k: v for k, v in record.items() if k in keys and not (
                    k == "expires" and v <= 0)} for record in records]
                self.call("Network.setCookies", {"cookies": cookies})
            return self
        except (OSError, ValueError, TypeError, KeyError, websocket.WebSocketException):
            self.close()
            raise AIError("Chromium's saved session is unavailable. Run --login to restore it.", "browser_start") from None
        except BaseException:
            self.close()
            raise

    def _connect(self, endpoint, target):
        import websocket
        address = urlsplit(endpoint)
        if address.scheme != "ws" or address.hostname not in ("127.0.0.1", "localhost"):
            raise AIError("Invalid local browser endpoint.", "browser_start")
        self.ws = websocket.create_connection(endpoint, timeout=0.2, suppress_origin=True, http_proxy_host=None)
        info = self.call("Target.getTargetInfo", {"targetId": target}).get("targetInfo", {})
        if info.get("type") != "page":
            raise AIError("The managed chat tab was closed. Run --login to reopen it.", "browser_login")
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.call("Page.enable")
        self.call("Runtime.enable")
        self._keep_page_active()

    def _keep_page_active(self):
        if self.keep_open:
            # A minimized/occluded native window suspends animation frames that
            # chat upload menus and image preprocessing need. This keeps the tab
            # rendering without restoring the window or taking desktop focus.
            self.call("Emulation.setFocusEmulationEnabled", {"enabled": True})

    @contextmanager
    def intercept_file_chooser(self):
        """Keep programmatic uploads from opening an OS file picker."""
        import websocket
        self._file_chooser_node = None
        try:
            self.call("Page.setInterceptFileChooserDialog", {"enabled": True})
            yield
        finally:
            # Restore manual uploads even if a cancelled/timed-out operation
            # cannot make another deadline-bound call. A later call can discard
            # this cleanup command's response, just like other unmatched events.
            if self.ws is not None:
                try:
                    self.counter += 1
                    self.ws.settimeout(0.2)
                    self.ws.send(json.dumps({"id": self.counter,
                        "method": "Page.setInterceptFileChooserDialog",
                        "params": {"enabled": False}, "sessionId": self.session}))
                except (OSError, websocket.WebSocketException):
                    pass
            self._file_chooser_node = None

    def _handle_event(self, event):
        if event.get("method") == "Page.fileChooserOpened" and event.get("sessionId") == self.session:
            node = event.get("params", {}).get("backendNodeId")
            if type(node) is int and node > 0:
                self._file_chooser_node = node

    def set_chooser_file(self, image_path):
        """Assign the input Chromium actually opened, even if the DOM changed."""
        def chooser():
            if self._file_chooser_node is None:
                self.evaluate("true")  # Receive a chooser event queued after the click reply.
            return self._file_chooser_node

        node = self.wait(chooser)
        remote = self.call("DOM.resolveNode", {"backendNodeId": node})["object"]["objectId"]
        # Re-selecting the same file must still emit change. Clear the chooser's
        # actual input without dispatching an empty change event to its handler.
        self.call("Runtime.callFunctionOn", {"objectId": remote,
            "functionDeclaration": "function(){this.value='';return true}", "returnByValue": True})
        self.call("DOM.setFileInputFiles", {
            "backendNodeId": node, "files": [str(Path(image_path).resolve())]})

    def download_text(self, trigger, *, max_bytes=65536):
        """Read one UI-initiated text download privately, including virtualized code."""
        import websocket
        with tempfile.TemporaryDirectory(prefix="seth-answer-") as directory:
            try:
                session, self.session = self.session, None
                try:
                    self.call("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": directory})
                finally:
                    self.session = session
                trigger()  # Exactly once; an uncertain click is never retried.
                ready_until = time.monotonic() + 5

                def downloaded():
                    files = [path for path in Path(directory).iterdir()
                             if path.is_file() and not path.name.endswith(".crdownload")]
                    if len(files) > 1:
                        raise AIError("The answer produced multiple downloads; refusing to choose one.", "browser_layout")
                    if files:
                        path = files[0]
                        if path.is_symlink() or path.stat().st_size > max_bytes:
                            raise AIError("The answer download exceeded its size limit or was not a regular file.", "browser_error")
                        path.chmod(0o600)
                        try:
                            text = path.read_text(encoding="utf-8-sig")
                        except UnicodeError:
                            raise AIError("The answer download was not UTF-8 text.", "browser_error") from None
                        if not text.strip():
                            raise AIError("The answer download was empty.", "browser_error")
                        return text
                    if time.monotonic() >= ready_until:
                        raise AIError("The complete code answer could not be downloaded.", "browser_layout")

                return self.wait(downloaded)
            finally:
                # Do not leave this persistent browser downloading into a deleted
                # directory, even after cancellation or an expired deadline.
                if self.ws is not None:
                    try:
                        self.counter += 1
                        self.ws.settimeout(0.2)
                        self.ws.send(json.dumps({"id": self.counter, "method": "Browser.setDownloadBehavior",
                                                 "params": {"behavior": "default"}}))
                    except (OSError, websocket.WebSocketException):
                        pass

    def close(self):
        if self.keep_open and self.ws is not None:
            # Disconnect the controller; keep this dedicated browser/session alive.
            if self.process is not None:
                retain_process(self.process)
            self.process = None
            super().close()
            return
        # Allow Chromium to flush its dedicated profile before process cleanup.
        # Cleanup must work even when inference has exhausted its deadline.
        pid = self.process.pid if self.process is not None else None
        if self.ws is not None and self.process is not None and self.process.poll() is None:
            try:
                self.counter += 1
                self.ws.settimeout(0.2)
                self.ws.send(json.dumps({"id": self.counter, "method": "Browser.close", "params": {}}))
            except Exception:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        super().close()
        if pid is not None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def navigate(self, url):
        super().navigate(url)
        # Chrome acknowledges navigation before the new document commits.
        # Do not mistake the target's initial about:blank for a logged-out page.
        def committed():
            try:
                return self.evaluate("location.href !== 'about:blank' && document.readyState !== 'loading'")
            except AIError as error:
                if error.code != "browser_protocol":
                    raise
                return False  # The old execution context can disappear at commit.
        self.wait(committed)


def managed_browser(state_file, profile):
    """Trust only a live browser owned by this user with this exact profile/port."""
    try:
        state = json.loads(Path(state_file).read_text())
        pid, port = state["pid"], state["port"]
        if type(pid) is not int or pid <= 1 or type(port) is not int or not 0 < port < 65536:
            return None
        if state["profile"] != str(Path(profile).resolve()):
            return None
        process = Path(f"/proc/{pid}")
        if process.stat().st_uid != os.getuid():
            return None
        command = (process / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        if (not re.search(r"(?<!\S)--user-data-dir=" + re.escape(state["profile"]) + r"(?=\s|$)", command)
                or not re.search(r"(?<!\S)--remote-debugging-port=" + str(port) + r"(?=\s|$)", command)):
            return None
        return state
    except (OSError, ValueError, TypeError, KeyError):
        return None


def close_managed_browser(state_file, profile):
    state = managed_browser(state_file, profile)
    if state:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        import websocket
        connection = None
        try:
            with opener.open(f"http://127.0.0.1:{state['port']}/json/version", timeout=2) as response:
                endpoint = json.load(response)["webSocketDebuggerUrl"]
            address = urlsplit(endpoint)
            if address.scheme != "ws" or address.hostname not in ("127.0.0.1", "localhost") or address.port != state["port"]:
                raise AIError("Invalid local browser endpoint.", "browser_start")
            connection = websocket.create_connection(endpoint, timeout=2, suppress_origin=True, http_proxy_host=None)
            connection.send(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
        finally:
            if connection:
                connection.close()
        deadline = time.monotonic() + 3
        while managed_browser(state_file, profile) and time.monotonic() < deadline:
            time.sleep(0.05)
        process = _MANAGED_PROCESSES.pop(state["pid"], None)
        if process is not None:
            process.wait(timeout=3)
    Path(state_file).unlink(missing_ok=True)
