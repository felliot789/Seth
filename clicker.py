"""One screenshot-relative click through VirtualBox's emulated pointing device."""

import math
import random
import time

from vbox_backend import VMError, VirtualBoxBackend, validate_age


class SethClicker:
    def __init__(self, config, backend=None):
        self.config = config
        self.backend = backend if backend is not None else VirtualBoxBackend(config)
        self.options = config.get("clicker", {})

    def click(self, frame, x, y, stop_event=None):
        def check():
            if stop_event is not None and stop_event.is_set():
                raise VMError("Cycle cancelled; no click.")
            validate_age(frame.captured_at, self.config.get("capture", {}).get("max_age_seconds", 90))

        display = frame.display
        if type(x) is not int or type(y) is not int or not (0 <= x < display.width and 0 <= y < display.height):
            raise VMError("Click lies outside captured guest display.")
        delay = self.options.get("delay_ms", [200, 800])
        if (not isinstance(delay, (list, tuple)) or len(delay) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in delay)
                or not 0 <= delay[0] <= delay[1] <= 10000):
            raise VMError("clicker.delay_ms must be [minimum, maximum] within 0..10000.")
        check()
        seconds = random.uniform(*delay) / 1000
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            time.sleep(seconds)
        check()
        return self.backend.click(frame, x, y, self.config.get("capture", {}).get("max_age_seconds", 90),
                                  stop_event=stop_event)
