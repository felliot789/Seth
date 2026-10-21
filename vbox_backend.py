"""VirtualBox framebuffer access and button-free scrolling in an SDK helper."""

from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time


class VMError(RuntimeError):
    pass


@dataclass(frozen=True)
class GuestDisplay:
    vm_uuid: str
    screen: int
    width: int
    height: int
    x_origin: int
    y_origin: int
    state_changed: int
    session_pid: int
    monitor_status: int


class VirtualBoxBackend:
    def __init__(self, config):
        vm = config.get("vm", {})
        self.target = vm.get("uuid") or vm.get("name")
        self.screen = vm.get("screen", 0)
        self.python = vm.get("sdk_python") or sys.executable
        self._process = None
        self._request_lock = threading.Lock()

    def _request(self, operation, **data):
        if not isinstance(self.target, str) or not self.target.strip():
            raise VMError("Set vm.name or vm.uuid to a registered VirtualBox VM.")
        if type(self.screen) is not int or self.screen < 0:
            raise VMError("vm.screen must be a nonnegative integer.")
        if not isinstance(self.python, str) or not self.python:
            raise VMError("vm.sdk_python must name a Python interpreter with the VirtualBox SDK installed.")
        request = {"operation": operation, "target": data.get("expected", {}).get("vm_uuid", self.target),
                   "screen": self.screen, **data}
        helper_env = {name: value for name, value in os.environ.items() if not name.endswith("API_KEY")}
        with self._request_lock:
            try:
                if self._process is None:
                    self._process = subprocess.Popen(
                        [self.python, str(Path(__file__).resolve()), "--worker"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True, bufsize=1, env=helper_env,
                    )
                process = self._process
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
                with selectors.DefaultSelector() as ready:
                    ready.register(process.stdout, selectors.EVENT_READ)
                    if not ready.select(30):
                        self.close()
                        raise VMError("VirtualBox SDK capture/status timed out.")
                response = json.loads(process.stdout.readline())
            except (OSError, ValueError):
                self.close()
                raise VMError("VirtualBox SDK capture/status helper disconnected.") from None
        if not isinstance(response, dict):
            raise VMError("VirtualBox SDK helper returned an invalid response.")
        if response.get("ok") is not True:
            raise VMError(response.get("error") or "VirtualBox SDK helper failed.")
        return response["result"]

    def close(self):
        process, self._process = self._process, None
        if process is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()

    def status(self):
        return self._request("status")

    def capture(self, path):
        result = self._request("capture", path=str(Path(path).resolve()))
        return GuestDisplay(**result["display"]), result["captured_at"]

    def scroll(self, frame, ticks, stop_event=None):
        if stop_event is not None and stop_event.is_set():
            raise VMError("Scroll capture cancelled.")
        return self._request("scroll", expected=asdict(frame.display),
                             captured_at=frame.captured_at, ticks=ticks)

def _snapshot(machine, console, constants, screen):
    if machine.state != constants.MachineState_Running:
        raise VMError("Target VM must be running.")
    if type(screen) is not int or not 0 <= screen < machine.graphicsAdapter.monitorCount:
        raise VMError("vm.screen is outside this VM's configured monitor range.")
    width, height, _, x_origin, y_origin, status = console.display.getScreenResolution(screen)
    if width <= 0 or height <= 0 or status == constants.GuestMonitorStatus_Disabled:
        raise VMError("Target guest display is disabled or has no usable resolution.")
    return GuestDisplay(machine.id, screen, width, height, x_origin, y_origin,
                        machine.lastStateChange, machine.sessionPID, status)


def sdk_operation(request, manager_factory=None, *, close_manager=True):
    """Only status, capture and button-free scrolling are supported."""
    if request.get("operation") not in ("status", "capture", "scroll"):
        raise VMError("Seth only captures and scrolls; answer/input operations are disabled.")
    if request["operation"] == "scroll" and (type(request.get("ticks")) is not int
                                             or not 1 <= abs(request["ticks"]) <= 4):
        raise VMError("Scroll requires one to four signed wheel ticks.")
    if manager_factory is None:
        try:
            from vboxapi import VirtualBoxManager
        except ImportError:
            raise VMError("VirtualBox's host Python SDK (vboxapi) is unavailable. Use a venv with --system-site-packages or set vm.sdk_python.") from None
        manager_factory = VirtualBoxManager
    manager = session = None
    locked = False
    try:
        manager = manager_factory(None, None)
        try:
            machine = manager.getVirtualBox().findMachine(request["target"])
        except Exception:
            raise VMError("Cannot find the VirtualBox VM; check vm.name or vm.uuid.") from None
        constants = manager.constants
        if machine.state != constants.MachineState_Running:
            raise VMError("Target VM must be running.")
        session = manager.getSessionObject()
        machine.lockMachine(session, constants.LockType_Shared)
        locked = True
        console = session.console
        screen = request["screen"]
        display = _snapshot(machine, console, constants, screen)
        if request["operation"] == "status":
            return {"display": asdict(display),
                    "display_blank": display.monitor_status == constants.GuestMonitorStatus_Blank}
        if display.monitor_status == constants.GuestMonitorStatus_Blank:
            raise VMError("Guest display is blank/asleep; wake it in VirtualBox and retry capture.")
        if request["operation"] == "scroll":
            if display != GuestDisplay(**request["expected"]):
                raise VMError("VM or display changed before scrolling.")
            age = time.monotonic() - request["captured_at"]
            if not 0 <= age <= 3:
                raise VMError("Scroll capture is stale; no scroll sent.")
            mouse = console.mouse
            if not mouse.absoluteSupported:
                raise VMError("Scroll mode needs an absolute pointing device (VirtualBox USB Tablet).")
            # The page has one whole-window scrollbar. Position over its right
            # margin; no button or keyboard event is ever sent. SDK pixels are 1-based.
            x, y = display.x_origin + display.width - 24, display.y_origin + display.height // 2 + 1
            mouse.putMouseEventAbsolute(x, y, request["ticks"], 0, 0)
            return {"scroll_ticks": request["ticks"]}
        captured_at = time.monotonic()
        png = console.display.takeScreenShotToArray(screen, display.width, display.height,
                                                   constants.BitmapFormat_PNG)
        Path(request["path"]).write_bytes(bytes(png))
        if _snapshot(machine, console, constants, screen) != display:
            raise VMError("VM or guest display changed during capture; try again.")
        return {"display": asdict(display), "captured_at": captured_at}
    except VMError:
        raise
    except Exception:
        raise VMError("VirtualBox SDK capture/status failed; check the VM display and host SDK.") from None
    finally:
        try:
            if locked:
                session.unlockMachine()
        finally:
            if manager is not None and close_manager:
                manager.deinit()


def _worker():
    manager = None
    try:
        with redirect_stdout(sys.stderr):
            from vboxapi import VirtualBoxManager
            manager = VirtualBoxManager(None, None)
        for line in sys.stdin:
            try:
                request = json.loads(line)
                with redirect_stdout(sys.stderr):
                    result = sdk_operation(request, lambda *_: manager, close_manager=False)
                response = {"ok": True, "result": result}
            except Exception as error:
                message = str(error) if isinstance(error, VMError) else "VirtualBox SDK helper failed."
                response = {"ok": False, "error": message}
            print(json.dumps(response), flush=True)
    finally:
        if manager is not None:
            manager.deinit()
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker())
