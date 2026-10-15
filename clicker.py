"""One bounded mouse click in the window that produced the screenshot."""

import math
import random
import time

from capture import DesktopError, run_xdotool, window_geometry


class SethClicker:
    def __init__(self, config):
        self.config = config
        self.options = config.get("clicker", {})

    def check_target(self, frame):
        if window_geometry(frame.window.id) != frame.window:
            raise DesktopError("Target moved or resized after capture; no click.")
        max_age = self.config.get("capture", {}).get("max_age_seconds", 90)
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or not 0 < max_age <= 300:
            raise DesktopError("capture.max_age_seconds must be greater than 0 and at most 300.")
        if time.monotonic() - frame.captured_at > max_age:
            raise DesktopError("Screenshot is too old; no click.")
        # Use the WM's active top-level window, with a no-WM fallback for X11 labs.
        try:
            active = run_xdotool("getactivewindow")
        except DesktopError:
            active = run_xdotool("getwindowfocus")
        if active != frame.window.id:
            raise DesktopError("Target is no longer the active window; no click.")

    def human_click(self, frame, x, y, stop_event=None):
        def check():
            if stop_event is not None and stop_event.is_set():
                raise DesktopError("Cycle cancelled; no click.")
            self.check_target(frame)

        window = frame.window
        if type(x) is not int or type(y) is not int or not (0 <= x < window.width and 0 <= y < window.height):
            raise DesktopError("Click lies outside captured window.")
        check()
        low, high = self.options.get("delay_ms", [200, 800])
        if not (0 <= low <= high <= 10000):
            raise DesktopError("clicker.delay_ms must be [minimum, maximum] within 0..10000.")
        jitter = self.options.get("jitter_pixels", 2)
        if type(jitter) is not int or not 0 <= jitter <= 10:
            raise DesktopError("clicker.jitter_pixels must be an integer from 0 to 10.")
        local_x = max(0, min(window.width - 1, x + random.randint(-jitter, jitter)))
        local_y = max(0, min(window.height - 1, y + random.randint(-jitter, jitter)))
        target_x, target_y = window.x + local_x, window.y + local_y
        time.sleep(random.uniform(low, high) / 1000)
        check()
        if self.options.get("smooth_movement", True):
            try:
                fields = dict(line.split("=", 1) for line in run_xdotool("getmouselocation", "--shell").splitlines())
                start_x, start_y = int(fields["X"]), int(fields["Y"])
            except (KeyError, ValueError):
                raise DesktopError("Cannot read mouse position; no click.") from None
            duration = self.options.get("movement_seconds", 0.3)
            if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 <= duration <= 2:
                raise DesktopError("clicker.movement_seconds must be between 0 and 2.")
            for step in range(1, 13):
                if stop_event is not None and stop_event.is_set():
                    raise DesktopError("Cycle cancelled; no click.")
                t = step / 12
                eased = t * t * (3 - 2 * t)
                run_xdotool("mousemove", round(start_x + (target_x - start_x) * eased),
                            round(start_y + (target_y - start_y) * eased))
                time.sleep(duration / 12)
        check()
        # A single checked process performs the final move and the only click.
        run_xdotool("mousemove", target_x, target_y, "click", "1")
        return {"click_x": local_x, "click_y": local_y, "screen_x": target_x, "screen_y": target_y}
