"""Per-cycle cancellation that also observes listener shutdown."""

import threading
import time


class CancellationEvent(threading.Event):
    """Event with synchronous subscribers, including race-safe registration.

    Unregister waits for an in-flight callback, so its resources can immediately
    be removed. Callbacks must be short and must not acquire another event lock.
    """

    def __init__(self):
        super().__init__()
        self._callbacks_lock = threading.RLock()
        self._callbacks = set()

    def set(self):
        with self._callbacks_lock:
            super().set()
            failure = None
            for callback in tuple(self._callbacks):
                try:
                    callback()
                except Exception as error:
                    # One failed marker must not prevent the other active
                    # helpers from observing the same cancellation.
                    if failure is None:
                        failure = error
            if failure is not None:
                raise failure

    def clear(self):
        with self._callbacks_lock:
            super().clear()

    def register(self, callback):
        with self._callbacks_lock:
            self._callbacks.add(callback)
            try:
                if self.is_set():
                    callback()
            except BaseException:
                self._callbacks.discard(callback)
                raise

        def unregister():
            with self._callbacks_lock:
                self._callbacks.discard(callback)
        return unregister


class CycleCancellation:
    def __init__(self, shutdown):
        self.shutdown = shutdown
        self.interrupted = CancellationEvent()

    def is_set(self):
        return self.shutdown.is_set() or self.interrupted.is_set()

    def set(self):
        self.interrupted.set()

    def clear(self):
        self.interrupted.clear()

    @property
    def requires_cancel_relay(self):
        # A stock Event cannot publish shutdown synchronously. Interrupts still
        # use our subscription; helper callers additionally relay that shutdown.
        return not callable(getattr(self.shutdown, "register", None))

    def register(self, callback):
        remove_interrupt = self.interrupted.register(callback)
        try:
            register_shutdown = getattr(self.shutdown, "register", None)
            remove_shutdown = register_shutdown(callback) if register_shutdown else lambda: None
            if not register_shutdown and self.shutdown.is_set():
                callback()
        except BaseException:
            remove_interrupt()
            raise

        def unregister():
            remove_interrupt()
            remove_shutdown()
        return unregister

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            self.interrupted.wait(.05 if remaining is None else min(.05, remaining))
        return self.is_set()
