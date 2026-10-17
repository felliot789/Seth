"""Host-only status indicator. All Qt operations run on the listener thread."""

import os
import time
import ctypes
import ctypes.util

from trigger import TriggerError

COLORS = {
    "idle": "#94a3b8", "capturing": "#60a5fa", "thinking": "#fbbf24",
    "selecting": "#c084fc", "done": "#4ade80", "dry_run": "#60a5fa",
    "no_action": "#fb923c", "low_confidence": "#fb923c",
    "error": "#f87171", "cancelled": "#94a3b8",
}
TEXT_COLORS = {"white": "#ffffff", "black": "#000000"}


def check_display(platform):
    """Fail normally while XWayland starts, instead of letting Qt abort."""
    if platform.split(":", 1)[0] != "xcb":
        return
    if not os.environ.get("DISPLAY"):
        raise TriggerError("Host display is not ready yet; retry after the desktop session starts.")
    library = ctypes.util.find_library("X11")
    if not library:
        raise TriggerError("Host indicator requires the X11 client library for XWayland.")
    x11 = ctypes.CDLL(library)
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x11.XOpenDisplay(None)
    if not display:
        raise TriggerError("Host display is not accessible yet; retry after the desktop session starts.")
    x11.XCloseDisplay(display)


class SethOverlay:
    def __init__(self):
        try:
            from PyQt6.QtCore import Qt
            from PyQt6.QtWidgets import QApplication, QLabel, QWidget
        except ImportError:
            raise TriggerError("The indicator needs PyQt6. Install requirements-overlay.txt.") from None
        # XWayland permits corner placement; native Wayland's xdg-shell does not.
        platform = os.environ.get("QT_QPA_PLATFORM", "xcb")
        check_display(platform)
        self.app = QApplication.instance() or QApplication(["seth-indicator", "-platform", platform])
        self.app.setQuitOnLastWindowClosed(False)
        flags = (Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.window = QWidget(None, flags)
        self.window.setWindowTitle("Seth status")
        self.window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.window.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.window.setFixedSize(336, 38)
        self.panel = panel = QWidget(self.window)
        panel.setGeometry(0, 0, 336, 38)
        panel.setObjectName("statusPanel")
        panel.setStyleSheet("#statusPanel { background: transparent; border: none; }")
        self.dot = QLabel(self.window)
        self.dot.setFixedSize(5, 5)
        self.label = QLabel(panel)
        self.label.setGeometry(13, 0, 294, 38)
        self.label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.text_color = "white"
        self._apply_text_color()
        self.progress = None
        self.display_mode = "full"
        self.update(("idle", "Idle"))
        self._apply_mode()
        self.app.processEvents()

    def place(self):
        screen = self.app.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.window.move(area.right() - self.window.width() - 15, area.top() + 16)

    def update(self, progress, selection=None):
        if progress != self.progress:
            self.progress = progress
            self.progress_started = time.monotonic()
            state, label = progress
            self.dot.setStyleSheet("border-radius: 2px; background: " + COLORS.get(state, COLORS["idle"]) + ";")
        state, label = progress
        if state == "thinking":
            elapsed = max(0, int(time.monotonic() - self.progress_started))
            label = f"Waiting {elapsed // 60}:{elapsed % 60:02d}"
        text = " · ".join(["Seth"] + ([selection] if selection else []) + [label])
        if self.label.text() != text:
            self.label.setText(text)

    def poll(self, progress, selection=None):
        self.update(progress, selection)
        self.app.processEvents()

    @property
    def visible(self):
        return self.display_mode != "hidden"

    def _apply_mode(self):
        full = self.display_mode == "full"
        self.window.setFixedSize(336 if full else 32, 38)
        self.panel.setVisible(full)
        # The dot's screen position is identical in Full and Dot modes.
        self.dot.move(self.window.width() - 19, 16)
        if self.visible:
            self.place()
            self.window.show()
        else:
            self.window.hide()

    def toggle(self):
        self.display_mode = {"full": "hidden", "hidden": "dot", "dot": "full"}[self.display_mode]
        self._apply_mode()
        self.app.processEvents()
        return self.display_mode

    def _apply_text_color(self):
        self.label.setStyleSheet("color: " + TEXT_COLORS[self.text_color]
                                + "; font-size: 12px; font-weight: 500; background: transparent; border: none;")

    def toggle_text_color(self):
        self.text_color = "black" if self.text_color == "white" else "white"
        self._apply_text_color()
        self.app.processEvents()
        return self.text_color

    def stop(self):
        self.window.close()
        self.app.processEvents()
