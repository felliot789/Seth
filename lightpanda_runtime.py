"""Private Lightpanda process and a small synchronous CDP client.

No desktop browser, Node, Playwright, or AI controller is needed at runtime.
The caller owns the deadline and cancellation event across all operations.
"""

import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request

from inference import AIError

RUNTIME_ROOT = Path.home() / ".local/share/seth/runtime/lightpanda"
DEFAULT_BINARY = RUNTIME_ROOT / "current/lightpanda"


def install_lightpanda():
    """Install an official release asset, verified against GitHub's SHA-256."""
    architecture = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(platform.machine())
    if platform.system() != "Linux" or architecture is None:
        raise AIError("Seth's Lightpanda installer supports Linux x86_64 and aarch64.")
    asset_name = f"lightpanda-{architecture}-linux"
    request = urllib.request.Request("https://api.github.com/repos/lightpanda-io/browser/releases/tags/nightly",
                                     headers={"User-Agent": "Seth-Lightpanda-Installer"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next((a for a in release["assets"] if a["name"] == asset_name), None)
    if not asset or not asset.get("digest", "").startswith("sha256:"):
        raise AIError("The official Lightpanda release has no verifiable binary for this host.")
    digest = asset["digest"][7:]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise AIError("Invalid Lightpanda release checksum.")
    url = asset["browser_download_url"]
    if not url.startswith("https://github.com/lightpanda-io/browser/releases/download/"):
        raise AIError("Unexpected Lightpanda release download URL.")
    destination = RUNTIME_ROOT / digest[:12]
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = destination / "lightpanda"
    if not binary.exists() or hashlib.sha256(binary.read_bytes()).hexdigest() != digest:
        fd, temporary = tempfile.mkstemp(prefix="download-", dir=destination)
        try:
            checksum = hashlib.sha256()
            with os.fdopen(fd, "wb") as output, urllib.request.urlopen(url, timeout=30) as response:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    checksum.update(chunk)
                    output.write(chunk)
            if checksum.hexdigest() != digest:
                raise AIError("Lightpanda download checksum mismatch.")
            os.chmod(temporary, 0o700)
            os.replace(temporary, binary)
        finally:
            Path(temporary).unlink(missing_ok=True)
    metadata = {"asset": asset_name, "url": url, "sha256": digest, "updated_at": asset["updated_at"]}
    (destination / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
    link = RUNTIME_ROOT / "current.new"
    link.unlink(missing_ok=True)
    link.symlink_to(destination.name, target_is_directory=True)
    link.replace(RUNTIME_ROOT / "current")
    return {"binary": str(binary), "sha256": digest}


def write_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Lightpanda:
    def __init__(self, *, executable=None, deadline, stop_event=None, cookies=None):
        self.executable = Path(executable or DEFAULT_BINARY).expanduser()
        self.deadline = deadline
        self.stop_event = stop_event
        self.cookies = Path(cookies) if cookies else None
        self.process = None
        self.ws = None
        self.counter = 0
        self.session = None

    def remaining(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise AIError("Web inference cancelled; no further action will be attempted.", "cancelled")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AIError("Lightpanda cycle timed out; no click was attempted.", "browser_timeout")
        return remaining

    def pause(self, seconds=0.15):
        seconds = min(seconds, self.remaining())
        if self.stop_event is not None:
            self.stop_event.wait(seconds)
        else:
            time.sleep(seconds)
        self.remaining()

    def __enter__(self):
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise AIError("Install Lightpanda first: .venv/bin/python web_inference.py --install", "browser_install")
        try:
            import websocket
        except ImportError:
            raise AIError("Install requirements-browser.txt for Lightpanda's Python controller.", "browser_install") from None
        self.remaining()
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        command = [str(self.executable), "serve", "--host", "127.0.0.1", "--port", str(port),
                   "--http-timeout", str(max(1000, int(self.remaining() * 1000))),
                   "--disable-metrics", "--log-level", "error"]
        for resource in ("stylesheet", "worker", "iframe", "image"):
            command.extend(["--load-resources", resource])
        if self.cookies is not None and self.cookies.exists():
            command.extend(["--cookie", str(self.cookies)])
        environment = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY")}
        environment.update(LIGHTPANDA_DISABLE_TELEMETRY="true", LIGHTPANDA_DISABLE_CORE_DUMP="1")
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            env=environment, start_new_session=True)
            startup_deadline = min(self.deadline, time.monotonic() + 10)
            while True:
                self.remaining()
                if self.process.poll() is not None:
                    raise AIError("Lightpanda exited during startup; check its installation and cookie file.", "browser_start")
                try:
                    self.ws = websocket.create_connection(f"ws://127.0.0.1:{port}/", timeout=0.2,
                                                           suppress_origin=True, http_proxy_host=None)
                    break
                except (OSError, websocket.WebSocketException):
                    if time.monotonic() >= startup_deadline:
                        raise AIError("Lightpanda did not start its local control endpoint.", "browser_start") from None
                    self.pause(0.05)
            target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            self.call("Page.enable")
            self.call("Runtime.enable")
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close(timeout=0)
            except Exception:
                pass
            self.ws = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=3)
            self.process = None

    def call(self, method, parameters=None):
        import websocket
        self.remaining()
        self.counter += 1
        message = {"id": self.counter, "method": method, "params": parameters or {}}
        if self.session is not None:
            message["sessionId"] = self.session
        try:
            self.ws.settimeout(min(0.2, self.remaining()))
            self.ws.send(json.dumps(message))
            while True:
                self.ws.settimeout(min(0.2, self.remaining()))
                try:
                    raw = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise AIError("Lightpanda's control connection closed.", "browser_closed")
                if len(raw) > 4 * 1024 * 1024:
                    raise AIError("Lightpanda exceeded the control response limit.", "browser_error")
                response = json.loads(raw)
                if response.get("id") != self.counter:
                    continue
                if response.get("error"):
                    raise AIError(f"Lightpanda does not support this operation: {method}.", "browser_protocol")
                return response.get("result", {})
        except (OSError, websocket.WebSocketException, ValueError):
            self.remaining()
            raise AIError("Lightpanda's control connection failed.", "browser_error") from None

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise AIError("Lightpanda could not execute a page operation; the website may use unsupported browser features.",
                          "browser_javascript")
        return result.get("result", {}).get("value")

    def navigate(self, url):
        result = self.call("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise AIError("Lightpanda could not load the provider page.", "browser_navigation")

    def wait(self, predicate):
        while True:
            self.remaining()
            value = predicate()
            if value:
                return value
            self.pause()

    def set_file(self, selector, image_path):
        root = self.call("DOM.getDocument")["root"]["nodeId"]
        node = self.call("DOM.querySelector", {"nodeId": root, "selector": selector}).get("nodeId")
        if not node:
            raise AIError("The provider has no matching upload input.", "browser_layout")
        self.call("DOM.setFileInputFiles", {"nodeId": node, "files": [str(Path(image_path).resolve())]})

    def save_cookies(self, path):
        cookies = self.call("Network.getAllCookies").get("cookies")
        if not isinstance(cookies, list):
            raise AIError("Lightpanda could not save its session cookies.", "browser_protocol")
        write_private_json(path, cookies)
