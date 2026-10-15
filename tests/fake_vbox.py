"""Small host SDK fixture; never imports or connects to real VirtualBox."""

import io
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

from PIL import Image, ImageDraw


TEST_DISPLAY = dict(vm_uuid="fake-vm-uuid", screen=0, width=320, height=200,
                    x_origin=0, y_origin=0, state_changed=12345, session_pid=42,
                    monitor_status=1)


def record(kind, **data):
    if path := os.environ.get("SETH_TEST_VBOX_LOG"):
        with Path(path).open("a") as stream:
            stream.write(json.dumps({"kind": kind, **data}) + "\n")


class FakeDisplay:
    def __init__(self):
        self.size = (320, 200)
        self.origin = (0, 0)
        self.status = 1
        self.after_capture = None

    def getScreenResolution(self, screen):
        return (*self.size, 32, *self.origin, self.status)

    def takeScreenShotToArray(self, screen, width, height, bitmap_format):
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        draw.text((20, 30), "Test question: select B", fill="black")
        draw.rectangle((120, 80, 180, 120), outline="black")
        draw.text((145, 100), "B", fill="black")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        if self.after_capture:
            self.after_capture()
        record("capture")
        return stream.getvalue()


class FakeMouse:
    def __init__(self):
        self.absoluteSupported = True
        self.events = []
        self.failure = None
        self.after_move = None
        self.min_hold_seconds = 0
        self.pressed_at = None

    def putMouseEventAbsolute(self, *args):
        self.events.append(args)
        record("mouse", args=args)
        if args[-1] == 1:
            self.pressed_at = time.monotonic()
        elif self.pressed_at is not None:
            held = time.monotonic() - self.pressed_at
            self.pressed_at = None
            if held < self.min_hold_seconds:
                raise RuntimeError("Virtual mouse missed a press shorter than its polling interval")
        if len(self.events) == 1 and self.after_move:
            self.after_move()
        if (self.failure == "press" and args[-1] == 1
                or self.failure == "release" and len(self.events) == 3):
            raise RuntimeError("Simulated input failure")


class FakeManager:
    def __init__(self, *args):
        self.constants = SimpleNamespace(MachineState_Running=5, LockType_Shared=1,
                                         GuestMonitorStatus_Disabled=0, GuestMonitorStatus_Blank=2,
                                         BitmapFormat_PNG=541544016)
        self.mouse = FakeMouse()
        self.console = SimpleNamespace(mouse=self.mouse, display=FakeDisplay())
        self.machine = SimpleNamespace(id=TEST_DISPLAY["vm_uuid"], state=5,
                                        graphicsAdapter=SimpleNamespace(monitorCount=1),
                                        lastStateChange=12345, sessionPID=42,
                                        lockMachine=lambda session, lock: record("lock"))
        self.unlocked = False
        self.closed = False

    def getVirtualBox(self):
        return SimpleNamespace(findMachine=lambda target: self.machine)

    def getSessionObject(self):
        return SimpleNamespace(console=self.console, unlockMachine=self.unlock)

    def unlock(self):
        self.unlocked = True
        record("unlock")

    def deinit(self):
        self.closed = True
        record("deinit")
