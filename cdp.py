"""Bounded Chromium DevTools connection and private state records."""

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from inference import AIError


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


class CDP:
    label = "Chromium"

    def __init__(self, *, executable, deadline, stop_event=None):
        self.executable = Path(executable).expanduser()
        self.deadline = deadline
        self.stop_event = stop_event
        self.process = None
        self.ws = None
        self.counter = 0
        self.session = None

    def remaining(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise AIError("Web inference cancelled; no further action will be attempted.", "cancelled")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AIError(f"{self.label} cycle timed out.", "browser_timeout")
        return remaining

    def pause(self, seconds=0.15):
        seconds = min(seconds, self.remaining())
        if self.stop_event is not None:
            self.stop_event.wait(seconds)
        else:
            time.sleep(seconds)
        self.remaining()

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
                    raise AIError(f"{self.label}'s control connection closed.", "browser_closed")
                if len(raw) > 4 * 1024 * 1024:
                    raise AIError(f"{self.label} exceeded the control response limit.", "browser_error")
                response = json.loads(raw)
                if response.get("id") != self.counter:
                    self._handle_event(response)
                    continue
                if response.get("error"):
                    raise AIError(f"{self.label} does not support this operation: {method}.", "browser_protocol")
                return response.get("result", {})
        except (OSError, websocket.WebSocketException, ValueError):
            self.remaining()
            raise AIError(f"{self.label}'s control connection failed.", "browser_error") from None

    def _handle_event(self, event):
        pass

    def evaluate(self, expression, *, user_gesture=False):
        parameters = {"expression": expression, "returnByValue": True}
        if user_gesture:
            parameters["userGesture"] = True
        result = self.call("Runtime.evaluate", parameters)
        if result.get("exceptionDetails"):
            raise AIError(f"{self.label} could not execute a page operation; the website may use unsupported browser features.",
                          "browser_javascript")
        return result.get("result", {}).get("value")

    def navigate(self, url):
        result = self.call("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise AIError(f"{self.label} could not load the provider page.", "browser_navigation")

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
