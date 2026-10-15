"""Capture the guest framebuffer through the host VirtualBox SDK."""

from dataclasses import dataclass
from pathlib import Path
import os
import tempfile

from PIL import Image

from vbox_backend import GuestDisplay, VMError, VirtualBoxBackend


@dataclass(frozen=True)
class Frame:
    path: Path
    display: GuestDisplay
    captured_at: float


class SethCapture:
    def __init__(self, config, backend=None):
        self.backend = backend if backend is not None else VirtualBoxBackend(config)
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
