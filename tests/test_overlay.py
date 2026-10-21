import os
import unittest
from unittest.mock import Mock, patch

try:
    from PyQt6.QtCore import Qt
except ImportError:
    Qt = None

from overlay import COLORS, SethOverlay, check_display
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

    def setUp(self):
        self.overlay.display_mode = 'full'
        if self.overlay.text_color != 'white':
            self.overlay.toggle_text_color()
        self.overlay._apply_mode()
        self.overlay.poll(('idle', 'Idle'))

    def test_full_hidden_dot_full_cycle_keeps_the_dot_at_the_same_corner(self):
        overlay = self.overlay
        full_center = overlay.dot.mapToGlobal(overlay.dot.rect().center())
        self.assertEqual(overlay.toggle(), 'hidden')
        self.assertFalse(overlay.window.isVisible())
        self.assertEqual(overlay.toggle(), 'dot')
        self.assertTrue(overlay.window.isVisible())
        self.assertTrue(overlay.dot.isVisible())
        self.assertFalse(overlay.panel.isVisible())
        self.assertFalse(overlay.label.isVisible())
        self.assertEqual(overlay.dot.mapToGlobal(overlay.dot.rect().center()), full_center)
        self.assertEqual(overlay.toggle(), 'full')
        self.assertTrue(overlay.label.isVisible())

    def test_dot_stays_text_free_and_updates_colours_without_expanding(self):
        overlay = self.overlay
        overlay.toggle()
        overlay.toggle()
        for state in ('capturing', 'thinking', 'done', 'uncertain', 'no_action', 'error'):
            with self.subTest(state=state):
                overlay.poll((state, state), 'Gemini · Table')
                self.assertEqual(overlay.display_mode, 'dot')
                self.assertFalse(overlay.label.isVisible())
                self.assertFalse(overlay.panel.isVisible())
                self.assertEqual(overlay.window.width(), 32)
                self.assertIn(COLORS[state], overlay.dot.styleSheet())
        image = overlay.window.grab().toImage()
        self.assertEqual((overlay.dot.width(), overlay.dot.height()), (3, 3))
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(image.pixelColor(15, 18).name(), COLORS['error'])
        self.assertLess(image.pixelColor(14, 17).alpha(), 255)
        self.assertEqual(image.pixelColor(13, 18).alpha(), 0)
        self.assertEqual(image.pixelColor(17, 18).alpha(), 0)
        self.assertNotEqual(COLORS['thinking'], COLORS['uncertain'])

    def test_full_text_has_no_background_or_border_in_either_color(self):
        overlay = self.overlay
        for color in ('white', 'black'):
            if overlay.text_color != color:
                overlay.toggle_text_color()
            overlay.poll(('done', 'Done'), 'Gemini · Table')
            image = overlay.window.grab().toImage()
            for point in ((0, 0), (0, 19), (100, 3), (100, 34), (312, 19), (335, 37)):
                self.assertEqual(image.pixelColor(*point).alpha(), 0, (color, point))
            ink = [image.pixelColor(x, y) for y in range(image.height()) for x in range(13, 307)
                   if image.pixelColor(x, y).alpha() > 200]
            self.assertTrue(ink)
            self.assertTrue(all(pixel.name() == ('#ffffff' if color == 'white' else '#000000') for pixel in ink))

    def test_text_color_toggle_preserves_display_mode_status_and_dot(self):
        overlay = self.overlay
        overlay.poll(('thinking', 'Waiting'), 'Qwen · Table')
        progress, dot = overlay.progress, overlay.dot.styleSheet()
        for mode in ('full', 'hidden', 'dot'):
            overlay.display_mode = mode
            overlay._apply_mode()
            visible = overlay.window.isVisible()
            for expected in ('black', 'white'):
                self.assertEqual(overlay.toggle_text_color(), expected)
                self.assertEqual(overlay.display_mode, mode)
                self.assertEqual(overlay.window.isVisible(), visible)
                self.assertEqual(overlay.progress, progress)
                self.assertEqual(overlay.dot.styleSheet(), dot)
                self.assertEqual(overlay.label.isVisible(), mode == 'full')

    def test_full_shows_both_selections_and_preserves_result_colour_on_a_switch(self):
        overlay = self.overlay
        overlay.poll(('done', 'Done'), 'API · MC')
        colour = overlay.dot.styleSheet()
        overlay.poll(('done', 'Done'), 'Qwen · Table')
        self.assertEqual(overlay.label.text(), 'Done')
        self.assertEqual(overlay.dot.styleSheet(), colour)

    def test_answer_keys_are_plain_and_complete_without_prefix_or_clipping(self):
        overlay = self.overlay
        for keys in ("2", "1, 3, 5", "L, R, L, R, R", ", ".join(str(i) for i in range(1, 65))):
            overlay.poll(("done", keys), "API · MCMA")
            self.assertEqual(overlay.label.text(), keys)
            self.assertEqual(overlay.label.textFormat(), Qt.TextFormat.PlainText)
            self.assertGreaterEqual(overlay.label.height(), overlay.label.heightForWidth(overlay.label.width()))
            self.assertLessEqual(overlay.window.width(), overlay.app.primaryScreen().availableGeometry().width() - 32)

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
        self.assertEqual(overlay.label.text(), "Done")

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
                     Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.NoDropShadowWindowHint):
            self.assertTrue(window.windowFlags() & flag)
        self.assertTrue(window.property("_KDE_NET_WM_SKIP_SHADOW"))
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(window.focusPolicy(), Qt.FocusPolicy.NoFocus)


if __name__ == "__main__":
    unittest.main()
