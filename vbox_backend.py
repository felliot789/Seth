"""Direct host VirtualBox SDK access, isolated from listener threads in a helper.

Only VM metadata, file paths, and pixel coordinates cross the helper boundary.
Inference credentials are never passed to it. Nothing runs inside the guest.
"""

from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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


def validate_age(captured_at, max_age):
    if (type(max_age) not in (int, float) or not math.isfinite(max_age)
            or not 0 < max_age <= 300):
        raise VMError("capture.max_age_seconds must be greater than 0 and at most 300.")
    if (type(captured_at) not in (int, float) or not math.isfinite(captured_at)
            or not 0 <= time.monotonic() - captured_at <= max_age):
        raise VMError("Screenshot is too old or has an invalid timestamp; no click.")


class VirtualBoxBackend:
    def __init__(self, config):
        vm = config.get("vm", {})
        self.target = vm.get("uuid") or vm.get("name")
        self.screen = vm.get("screen", 0)
        self.python = vm.get("sdk_python") or sys.executable

    def _request(self, operation, **data):
        if not isinstance(self.target, str) or not self.target.strip():
            raise VMError("Set vm.name or vm.uuid to a registered VirtualBox VM; X11 window names are no longer used.")
        if type(self.screen) is not int or self.screen < 0:
            raise VMError("vm.screen must be a nonnegative integer.")
        if not isinstance(self.python, str) or not self.python:
            raise VMError("vm.sdk_python must name a Python interpreter with the VirtualBox SDK installed.")
        # Pin input to the captured UUID, even if the original VM name is reused.
        target = data.get("expected", {}).get("vm_uuid", self.target)
        request = {"operation": operation, "target": target, "screen": self.screen, **data}
        credential_names = {"INFERENCE_API_KEY", "SETH_API_KEY", "SETTH_API_KEY", "OPENAI_API_KEY"}
        helper_env = {name: value for name, value in os.environ.items() if name not in credential_names}
        try:
            process = subprocess.run(
                [self.python, str(Path(__file__).resolve()), "--worker"],
                input=json.dumps(request), capture_output=True, text=True, timeout=30, env=helper_env,
            )
        except subprocess.TimeoutExpired:
            message = "VirtualBox input timed out; completion is unknown." if operation in ("click", "drag", "move", "wheel") else "VirtualBox SDK request timed out."
            raise VMError(message) from None
        except OSError:
            raise VMError("Cannot start the VirtualBox SDK helper; check vm.sdk_python.") from None
        try:
            response = json.loads(process.stdout)
        except (TypeError, ValueError):
            raise VMError("VirtualBox SDK helper failed to return JSON; check its Python/SDK installation.") from None
        if not isinstance(response, dict):
            raise VMError("VirtualBox SDK helper returned an invalid response.")
        if response.get("ok") is not True or process.returncode != 0:
            raise VMError(response.get("error") or "VirtualBox SDK helper failed.")
        return response["result"]

    def status(self):
        return self._request("status")

    def capture(self, path):
        result = self._request("capture", path=str(Path(path).resolve()))
        return GuestDisplay(**result["display"]), result["captured_at"]

    def click(self, frame, x, y, max_age, stop_event=None):
        return self._input("click", frame, max_age, stop_event, x=x, y=y)

    def drag(self, frame, x, y, end_x, end_y, max_age, stop_event=None):
        return self._input("drag", frame, max_age, stop_event,
                           x=x, y=y, end_x=end_x, end_y=end_y)

    def park_pointer(self, frame, max_age, stop_event=None):
        # Avoid desktop corners and answer rows. This is a button-free move in
        # guest pixels, never a host cursor operation or an extra answer click.
        return self._input("move", frame, max_age, stop_event,
                           x=min(8, frame.display.width - 1), y=frame.display.height // 2)

    def wheel(self, frame, x, y, ticks, max_age, stop_event=None):
        """Button-free guest wheel input; positive ticks scroll downward."""
        return self._input("wheel", frame, max_age, stop_event, x=x, y=y, ticks=ticks)

    def _input(self, operation, frame, max_age, stop_event, **coordinates):
        def send(**extra):
            return self._request(operation, expected=asdict(frame.display), captured_at=frame.captured_at,
                                 max_age=max_age, **coordinates, **extra)

        if stop_event is None:
            return send()
        if stop_event.is_set():
            raise VMError("Cycle cancelled; no click.")
        # Carry cancellation into a helper that may be waiting on native SDK I/O.
        with tempfile.TemporaryDirectory(prefix="seth-input-") as directory:
            marker = Path(directory) / "cancelled"
            finished = threading.Event()

            def relay_cancel():
                while not finished.wait(0.02):
                    if stop_event.is_set():
                        marker.touch(mode=0o600)
                        return

            relay = threading.Thread(target=relay_cancel, name="seth-input-cancel")
            relay.start()
            try:
                return send(cancel_path=str(marker))
            finally:
                finished.set()
                relay.join()


def _snapshot(machine, console, constants, screen):
    if machine.state != constants.MachineState_Running:
        raise VMError("Target VM must be running; no input was sent.")
    if type(screen) is not int or not 0 <= screen < machine.graphicsAdapter.monitorCount:
        raise VMError("vm.screen is outside this VM's configured monitor range.")
    width, height, _, x_origin, y_origin, status = console.display.getScreenResolution(screen)
    if width <= 0 or height <= 0 or status == constants.GuestMonitorStatus_Disabled:
        raise VMError("Target guest display is disabled or has no usable resolution.")
    return GuestDisplay(machine.id, screen, width, height, x_origin, y_origin,
                        machine.lastStateChange, machine.sessionPID, status)


def sdk_operation(request, manager_factory=None):
    """Execute one operation in a fresh host process with one shared VM session."""
    if manager_factory is None:
        try:
            from vboxapi import VirtualBoxManager
        except ImportError:
            raise VMError("VirtualBox's host Python SDK (vboxapi) is unavailable. Use a venv with --system-site-packages or set vm.sdk_python to the SDK's Python.") from None
        manager_factory = VirtualBoxManager
    manager = None
    session = None
    locked = False
    try:
        operation = request["operation"]
        if operation not in ("status", "capture", "click", "drag", "move", "wheel"):
            raise VMError("Unsupported VirtualBox operation.")
        manager = manager_factory(None, None)
        try:
            machine = manager.getVirtualBox().findMachine(request["target"])
        except Exception:
            raise VMError("Cannot find the VirtualBox VM; check vm.name or vm.uuid and run as its host owner.") from None
        constants = manager.constants
        if machine.state != constants.MachineState_Running:
            raise VMError("Target VM must be running; no input was sent.")
        session = manager.getSessionObject()
        machine.lockMachine(session, constants.LockType_Shared)
        locked = True
        console = session.console
        screen = request["screen"]
        display = _snapshot(machine, console, constants, screen)
        if operation == "status":
            return {"display": asdict(display), "absolute_mouse": bool(console.mouse.absoluteSupported),
                    "display_blank": display.monitor_status == constants.GuestMonitorStatus_Blank}
        if operation == "capture":
            if display.monitor_status == constants.GuestMonitorStatus_Blank:
                raise VMError("Guest display is blank/asleep; wake it in VirtualBox and retry capture.")
            captured_at = time.monotonic()
            png = console.display.takeScreenShotToArray(screen, display.width, display.height,
                                                       constants.BitmapFormat_PNG)
            Path(request["path"]).write_bytes(bytes(png))
            if _snapshot(machine, console, constants, screen) != display:
                raise VMError("VM or guest display changed during capture; try again.")
            return {"display": asdict(display), "captured_at": captured_at}

        expected = GuestDisplay(**request["expected"])

        def check_cancelled():
            if request.get("cancel_path") and Path(request["cancel_path"]).exists():
                raise VMError("Cycle cancelled; no click.")

        def check_target():
            check_cancelled()
            validate_age(request["captured_at"], request["max_age"])
            if _snapshot(machine, console, constants, screen) != expected:
                raise VMError("VM session or guest display changed after capture; no click.")
            if not console.mouse.absoluteSupported:
                raise VMError("VM has no absolute pointing device. Configure a VirtualBox USB Tablet while powered off.")
            check_cancelled()

        x, y = request["x"], request["y"]
        if type(x) is not int or type(y) is not int or not (0 <= x < display.width and 0 <= y < display.height):
            raise VMError("Click lies outside the captured guest display.")
        if operation == "drag":
            end_x, end_y = request["end_x"], request["end_y"]
            if (type(end_x) is not int or type(end_y) is not int
                    or not (0 <= end_x < display.width and 0 <= end_y < display.height)):
                raise VMError("Drag endpoint lies outside the captured guest display.")
        if operation == "wheel":
            ticks = request.get("ticks")
            if type(ticks) is not int or not 1 <= abs(ticks) <= 32:
                raise VMError("Wheel input must contain one to 32 signed ticks.")
        check_target()
        # The SDK takes guest desktop pixels starting at (1, 1), not host pixels.
        guest_x, guest_y = display.x_origin + x + 1, display.y_origin + y + 1
        mouse = console.mouse
        mouse.putMouseEventAbsolute(guest_x, guest_y, 0, 0, 0)
        check_target()
        if operation == "move":
            return {"pointer_x": x, "pointer_y": y, "vm_uuid": display.vm_uuid, "screen": screen}
        if operation == "wheel":
            for _ in range(abs(ticks)):
                check_target()
                mouse.putMouseEventAbsolute(guest_x, guest_y, 1 if ticks > 0 else -1, 0, 0)
                time.sleep(.04)
            return {"wheel_ticks": ticks, "vm_uuid": display.vm_uuid, "screen": screen}
        if operation == "drag":
            last_x, last_y = guest_x, guest_y
            try:
                mouse.putMouseEventAbsolute(guest_x, guest_y, 0, 0, 1)
                for step in range(1, 11):
                    check_target()
                    last_x = display.x_origin + round(x + (end_x - x) * step / 10) + 1
                    last_y = display.y_origin + round(y + (end_y - y) * step / 10) + 1
                    mouse.putMouseEventAbsolute(last_x, last_y, 0, 0, 1)
                    time.sleep(0.025)
            finally:
                mouse.putMouseEventAbsolute(last_x, last_y, 0, 0, 0)
            return {"start": [x, y], "end": [end_x, end_y], "vm_uuid": display.vm_uuid, "screen": screen}
        try:
            mouse.putMouseEventAbsolute(guest_x, guest_y, 0, 0, 1)
            # Let the guest's virtual input device poll the pressed state before
            # release. Back-to-back states can collapse into an unpressed mouse.
            time.sleep(0.08)
        finally:
            # A failed press can still have reached the VM. Always attempt release.
            # Never retry a press, including when its completion is uncertain.
            mouse.putMouseEventAbsolute(guest_x, guest_y, 0, 0, 0)
        return {"click_x": x, "click_y": y, "vm_uuid": display.vm_uuid, "screen": screen}
    except VMError:
        raise
    except Exception:
        if request.get("operation") in ("click", "drag", "move", "wheel"):
            raise VMError("VirtualBox SDK input failed; completion may be unknown. Check the VM and host SDK.") from None
        raise VMError("VirtualBox SDK capture/status failed; check the VM display and host SDK.") from None
    finally:
        try:
            if locked:
                session.unlockMachine()
        finally:
            if manager is not None:
                manager.deinit()


def _worker():
    try:
        request = json.load(sys.stdin)
        # Some SDK versions print diagnostics during initialization.
        with redirect_stdout(sys.stderr):
            result = sdk_operation(request)
        response, code = {"ok": True, "result": result}, 0
    except Exception as error:
        message = str(error) if isinstance(error, VMError) else "VirtualBox SDK helper failed."
        response, code = {"ok": False, "error": message}, 1
    print(json.dumps(response))
    return code


if __name__ == "__main__":
    raise SystemExit(_worker())
