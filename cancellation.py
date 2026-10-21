"""Per-cycle cancellation that also observes listener shutdown."""

import threading
import time


class CycleCancellation:
    def __init__(self, shutdown):
        self.shutdown = shutdown
        self.interrupted = threading.Event()

    def is_set(self):
        return self.shutdown.is_set() or self.interrupted.is_set()

    def set(self):
        self.interrupted.set()

    def clear(self):
        self.interrupted.clear()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            self.interrupted.wait(.05 if remaining is None else min(.05, remaining))
        return self.is_set()
