"""Capture one explicitly selected host X11 window, with its geometry."""

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import time

from PIL import Image


class DesktopError(RuntimeError):
    pass


def run_xdotool(*args):
    try:
        result = subprocess.run(
            ["xdotool", *map(str, args)], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DesktopError("xdotool is unavailable or timed out; check the host X11 session.") from None
    if result.returncode:
        raise DesktopError("xdotool failed; check DISPLAY, XAUTHORITY, and the target window.")
    return result.stdout.strip()


@dataclass(frozen=True)
class Window:
    id: str
    x: int
    y: int
    width: int
    height: int


def window_geometry(window_id):
    try:
        fields = dict(line.split("=", 1) for line in
                      run_xdotool("getwindowgeometry", "--shell", window_id).splitlines())
        return Window(str(int(window_id, 0)) if window_id.startswith("0x") else str(int(window_id)),
                      *(int(fields[key]) for key in ("X", "Y", "WIDTH", "HEIGHT")))
    except (KeyError, ValueError):
        raise DesktopError("Unable to read target window geometry.") from None


@dataclass(frozen=True)
class Frame:
    path: Path
    window: Window
    captured_at: float


class SethCapture:
    def __init__(self, config):
        self.config = config
        root = Path(config.get("project_dir", Path(__file__).resolve().parent))
        self.output_dir = root / config.get("capture", {}).get("output_dir", "captures")

    def find_window(self):
        vm = self.config.get("vm", {})
        if vm.get("window_id"):
            window_id = str(vm["window_id"])
            if vm.get("window_name", "Existing-Windows-VirtualBox") not in run_xdotool("getwindowname", window_id):
                raise DesktopError("Configured window ID does not match the target name.")
        else:
            name = vm.get("window_name", "Existing-Windows-VirtualBox")
            ids = run_xdotool("search", "--onlyvisible", "--name", re.escape(name)).splitlines()
            if len(ids) != 1:
                raise DesktopError("Expected exactly one target window; set vm.window_id to disambiguate.")
            window_id = ids[0]
        window = window_geometry(window_id)
        if window.width <= 0 or window.height <= 0:
            raise DesktopError("Target window has invalid dimensions.")
        return window

    def capture_frame(self):
        window = self.find_window()
        converter = shutil.which("magick") or shutil.which("convert")
        if converter is None:
            raise DesktopError("Install ImageMagick (magick or convert).")
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, filename = tempfile.mkstemp(prefix="capture_", suffix=".png", dir=self.output_dir)
        os.close(fd)
        path = Path(filename)
        captured_at = time.monotonic()
        try:
            dump = subprocess.run(["xwd", "-id", window.id, "-silent"],
                                  capture_output=True, timeout=15, check=True)
            subprocess.run([converter, "xwd:-", "-define", "png:compression-level=9", str(path)],
                           input=dump.stdout, capture_output=True, timeout=15, check=True)
            with Image.open(path) as image:
                if image.size != (window.width, window.height) or image.format != "PNG":
                    raise DesktopError("Screenshot dimensions do not match target window geometry.")
                image.verify()
            if window_geometry(window.id) != window:
                raise DesktopError("Target moved or resized during capture; try again.")
            return Frame(path, window, captured_at)
        except Exception as error:
            path.unlink(missing_ok=True)
            if isinstance(error, DesktopError):
                raise
            raise DesktopError("Screenshot capture failed; check xwd, ImageMagick, and X11 access.") from None

    def capture_vm(self):
        return str(self.capture_frame().path)
