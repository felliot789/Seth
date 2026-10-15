import os
import unittest
from unittest.mock import Mock, patch

try:
    from PyQt6.QtCore import Qt
except ImportError:
    Qt = None

from overlay import SethOverlay, check_display
from trigger import TriggerError


class DisplayReadinessTests(unittest.TestCase):
    def test_missing_display_fails_cleanly_before_qt_startup(self):
        with patch.dict(os.environ, {"DISPLAY": ""}):
            with self.assertRaisesRegex(TriggerError, "not ready"):
                check_display("xcb")
            check_display("offscreen")

    def test_unreachable_display_fails_cleanly_and_ready_display_is_closed(self):
        x11 = Mock()
        with patch.dict(os.environ, DISPLAY=":1"), patch("overlay.ctypes.util.find_library", return_value="libX11.so.6"), \
                patch("overlay.ctypes.CDLL", return_value=x11):
            x11.XOpenDisplay.return_value = None
            with self.assertRaisesRegex(TriggerError, "not accessible"):
                check_display("xcb")
            x11.XCloseDisplay.assert_not_called()
            x11.XOpenDisplay.return_value = 123
            check_display("xcb")
            x11.XCloseDisplay.assert_called_once_with(123)


@unittest.skipIf(Qt is None, "Optional PyQt6 indicator dependency is not installed")
class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, QT_QPA_PLATFORM="offscreen"):
            cls.overlay = SethOverlay()

    @classmethod
    def tearDownClass(cls):
        cls.overlay.stop()

    def test_visibility_toggle_preserves_latest_progress_without_showing(self):
        overlay = self.overlay
        self.assertTrue(overlay.window.isVisible())
        overlay.toggle()
        overlay.poll(("thinking", "Thinking"))
        self.assertFalse(overlay.window.isVisible())
        self.assertEqual(overlay.label.text(), "Seth · Waiting 0:00")
        overlay.toggle()
        overlay.poll(("done", "Done"))
        self.assertTrue(overlay.window.isVisible())
        self.assertEqual(overlay.label.text(), "Seth · Done")

    def test_wait_timer_advances_and_resets_for_next_request(self):
        overlay = self.overlay
        overlay.poll(("idle", "Idle"))
        with patch("overlay.time.monotonic", return_value=100):
            overlay.poll(("thinking", "Waiting"))
        with patch("overlay.time.monotonic", return_value=175):
            overlay.poll(("thinking", "Waiting"))
            self.assertEqual(overlay.label.text(), "Seth · Waiting 1:15")
            overlay.poll(("error", "API timeout"))
            self.assertEqual(overlay.label.text(), "Seth · API timeout")
            overlay.poll(("thinking", "Waiting"))
            self.assertEqual(overlay.label.text(), "Seth · Waiting 0:00")
        overlay.poll(("idle", "Idle"))

    def test_indicator_is_noninteractive_and_does_not_request_activation(self):
        window = self.overlay.window
        for flag in (Qt.WindowType.WindowTransparentForInput, Qt.WindowType.WindowDoesNotAcceptFocus,
                     Qt.WindowType.WindowStaysOnTopHint):
            self.assertTrue(window.windowFlags() & flag)
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(window.focusPolicy(), Qt.FocusPolicy.NoFocus)


if __name__ == "__main__":
    unittest.main()
