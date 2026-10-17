"""Capture the guest framebuffer through the host VirtualBox SDK."""

from dataclasses import dataclass
from pathlib import Path
import os
import tempfile
import time

from PIL import Image

from control_targets import selection_visible
from vbox_backend import GuestDisplay, VMError, VirtualBoxBackend


@dataclass(frozen=True)
class Frame:
    path: Path
    display: GuestDisplay
    captured_at: float


class SethCapture:
    def __init__(self, config, backend=None):
        self.backend = backend if backend is not None else VirtualBoxBackend(config)
        self.max_age = config.get("capture", {}).get("max_age_seconds", 150)
        root = Path(config.get("project_dir", Path(__file__).resolve().parent))
        self.output_dir = root / config.get("capture", {}).get("output_dir", "captures")

    def capture_frame(self):
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, filename = tempfile.mkstemp(prefix="capture_", suffix=".png", dir=self.output_dir)
        os.close(fd)
        path = Path(filename)
        try:
            display, captured_at = self.backend.capture(path)
            with Image.open(path) as image:
                if image.size != (display.width, display.height) or image.format != "PNG":
                    raise VMError("Screenshot dimensions do not match the guest display.")
                image.verify()
            return Frame(path, display, captured_at)
        except Exception as error:
            path.unlink(missing_ok=True)
            if isinstance(error, VMError):
                raise
            raise VMError("Guest framebuffer capture failed or returned an invalid PNG.") from None

    def capture_vm(self):
        return str(self.capture_frame().path)

    def verify_target(self, frame, x, y):
        """Check that a remaining control still looks like the inferred target."""
        fresh = self.capture_frame()
        if fresh.display != frame.display:
            raise VMError("VM or guest display changed during selection plan; no further input.")
        box = (max(0, x - 24), max(0, y - 24),
               min(frame.display.width, x + 25), min(frame.display.height, y + 25))
        with Image.open(frame.path) as before, Image.open(fresh.path) as after:
            if before.convert("RGB").crop(box).tobytes() != after.convert("RGB").crop(box).tobytes():
                raise VMError("Remaining answer control changed; no further input. Start a fresh cycle.")

    def verify_selection(self, frame, target, stop_event=None):
        """Confirm a grounded control became selected; never retry the click."""
        if stop_event is not None and stop_event.is_set():
            raise VMError("Cycle cancelled while confirming selection; no further input.")
        # The table highlights the entire hovered row. Clear hover before the
        # strict control/background check and the next question-identity check.
        self.backend.park_pointer(frame, self.max_age, stop_event)
        deadline = time.monotonic() + 1.5
        while True:
            if stop_event is not None and stop_event.is_set():
                raise VMError("Cycle cancelled while confirming selection; no further input.")
            fresh = self.capture_frame()
            if fresh.display != frame.display:
                raise VMError("VM or guest display changed while confirming selection; no further input.")
            with Image.open(frame.path) as before, Image.open(fresh.path) as after:
                if selection_visible(before, after, target["target_control"]):
                    return
            if time.monotonic() >= deadline:
                raise VMError("Input was sent, but the answer control did not become visibly selected; no further input.")
            if stop_event is not None:
                stop_event.wait(.1)
            else:
                time.sleep(.1)
