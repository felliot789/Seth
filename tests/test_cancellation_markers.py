"""Cancellation publication uses only the fake SDK, with deterministic hooks."""

from pathlib import Path
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cancellation import CancellationEvent, CycleCancellation
from tests.fake_vbox import FakeManager, TEST_DISPLAY
from vbox_backend import GuestDisplay, VirtualBoxBackend, VMError, sdk_operation


class CancellationMarkerTests(unittest.TestCase):
    def backend(self, manager, paths):
        backend = VirtualBoxBackend({"vm": {"name": "fake-only"}})
        def request(operation, **data):
            paths.append(Path(data["cancel_path"]))
            return sdk_operation(dict(operation=operation, target="fake-only", screen=0, **data),
                                 manager_factory=lambda *_: manager)
        backend._request = request
        return backend

    def frame(self):
        return SimpleNamespace(display=GuestDisplay(**TEST_DISPLAY), captured_at=time.monotonic())

    def test_interrupt_and_shutdown_after_pointer_move_never_press(self):
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                stop = CancellationEvent()
                token = CycleCancellation(stop)
                manager, paths = FakeManager(), []
                manager.mouse.after_move = stop.set if shutdown else token.set
                backend = self.backend(manager, paths)
                with self.assertRaisesRegex(VMError, "cancelled"):
                    backend.click(self.frame(), 10, 10, 150, token)
                self.assertEqual([event[-1] for event in manager.mouse.events], [0])
                self.assertTrue(manager.unlocked)
                self.assertTrue(manager.closed)
                self.assertFalse(paths[0].parent.exists())
                self.assertFalse(token.interrupted._callbacks)
                self.assertFalse(stop._callbacks)
                # Cleanup must remove the old marker subscriber before rearming.
                token.clear()
                stop.clear()
                manager.mouse.after_move = None
                manager.mouse.events.clear()
                with patch("vbox_backend.time.sleep"):
                    backend.click(self.frame(), 10, 10, 150, token)
                self.assertEqual([event[-1] for event in manager.mouse.events], [0, 1, 0])
                token.set()
                stop.set()
                self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_registration_observes_cancellation_before_or_during_registration(self):
        for source in ("interrupt", "shutdown"):
            with self.subTest(source=source):
                shutdown = CancellationEvent()
                token = CycleCancellation(shutdown)
                event = token.interrupted if source == "interrupt" else shutdown
                event.set()
                called = []
                unregister = token.register(lambda: called.append(True))
                self.assertTrue(called)
                unregister()
                count = len(called)
                event.set()
                self.assertEqual(len(called), count)
        shutdown = CancellationEvent()
        token = CycleCancellation(shutdown)
        original = shutdown.register
        def register(callback):
            shutdown.set()
            return original(callback)
        with patch.object(shutdown, "register", side_effect=register):
            called = []
            unregister = token.register(lambda: called.append(True))
        self.assertEqual(called, [True])
        unregister()

    def test_cancellation_between_input_precheck_and_registration_prevents_movement(self):
        stop = CancellationEvent()
        token = CycleCancellation(stop)
        manager, paths = FakeManager(), []
        backend = self.backend(manager, paths)
        original = token.register
        def register(callback):
            stop.set()
            return original(callback)
        with patch.object(token, "register", side_effect=register):
            with self.assertRaisesRegex(VMError, "cancelled"):
                backend.click(self.frame(), 10, 10, 150, token)
        self.assertEqual(manager.mouse.events, [])
        self.assertTrue(manager.unlocked)
        self.assertTrue(manager.closed)
        self.assertFalse(stop._callbacks)
        self.assertFalse(token.interrupted._callbacks)
        self.assertFalse(paths[0].parent.exists())

    def test_stock_shutdown_event_retains_interrupt_subscription(self):
        shutdown = threading.Event()
        token = CycleCancellation(shutdown)
        self.assertTrue(token.requires_cancel_relay)
        published = []
        unregister = token.register(lambda: published.append(True))
        token.set()
        self.assertEqual(published, [True])
        unregister()
        self.assertFalse(token.interrupted._callbacks)
        shutdown.set()
        published.clear()
        unregister = token.register(lambda: published.append(True))
        self.assertTrue(published)
        unregister()

    def test_failed_subscriber_does_not_skip_other_active_helpers(self):
        event = CancellationEvent()
        published = []
        def fail():
            raise OSError("marker unavailable")
        remove_failure = event.register(fail)
        remove_success = event.register(lambda: published.append(True))
        with self.assertRaises(OSError):
            event.set()
        self.assertTrue(event.is_set())
        self.assertEqual(published, [True])
        remove_failure()
        remove_success()
        self.assertFalse(event._callbacks)

    def test_cancel_after_press_still_releases_and_failure_unregisters(self):
        stop = CancellationEvent()
        token = CycleCancellation(stop)
        manager, paths = FakeManager(), []
        original = manager.mouse.putMouseEventAbsolute
        def mouse(*args):
            original(*args)
            if args[-1] == 1:
                token.set()
        manager.mouse.putMouseEventAbsolute = mouse
        backend = self.backend(manager, paths)
        with patch("vbox_backend.time.sleep"):
            backend.click(self.frame(), 10, 10, 150, token)
        self.assertEqual([event[-1] for event in manager.mouse.events], [0, 1, 0])
        token.clear()
        manager.mouse.failure = "press"
        with self.assertRaises(VMError), patch("vbox_backend.time.sleep"):
            backend.click(self.frame(), 10, 10, 150, token)
        self.assertEqual(manager.mouse.events[-1][-1], 0)
        self.assertFalse(stop._callbacks)
        self.assertFalse(token.interrupted._callbacks)
        self.assertTrue(all(not path.parent.exists() for path in paths))
