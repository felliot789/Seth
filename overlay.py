"""Host-only status indicator. All Qt operations run on the listener thread."""

import os
import time
import ctypes
import ctypes.util

from trigger import TriggerError

COLORS = {
    "idle": "#94a3b8", "capturing": "#60a5fa", "thinking": "#fbbf24",
    "selecting": "#c084fc", "done": "#4ade80", "dry_run": "#60a5fa",
    "no_action": "#94a3b8", "low_confidence": "#fbbf24",
    "error": "#fb7185", "cancelled": "#94a3b8",
}


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
            from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget
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
        self.window.setFixedSize(188, 38)
        panel = QWidget(self.window)
        panel.setGeometry(0, 0, 188, 38)
        panel.setObjectName("statusPanel")
        panel.setStyleSheet("#statusPanel { background: rgba(20, 25, 36, 235); "
                            "border: 1px solid #374151; border-radius: 12px; }")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(13, 0, 12, 0)
        layout.setSpacing(9)
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        self.label = QLabel()
        self.label.setStyleSheet("color: #e5e7eb; font-size: 12px; font-weight: 500; background: transparent;")
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        layout.addStretch()
        self.progress = None
        self.visible = True
        self.update(("idle", "Idle"))
        self.place()
        self.window.show()
        self.app.processEvents()

    def place(self):
        screen = self.app.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.window.move(area.right() - self.window.width() - 15, area.top() + 16)

    def update(self, progress):
        if progress != self.progress:
            self.progress = progress
            self.progress_started = time.monotonic()
            state, label = progress
            self.dot.setStyleSheet("border-radius: 4px; background: " + COLORS.get(state, COLORS["idle"]) + ";")
        state, label = progress
        if state == "thinking":
            elapsed = max(0, int(time.monotonic() - self.progress_started))
            label = f"Waiting {elapsed // 60}:{elapsed % 60:02d}"
        text = "Seth · " + label
        if self.label.text() != text:
            self.label.setText(text)

    def poll(self, progress):
        self.update(progress)
        self.app.processEvents()

    def toggle(self):
        self.visible = not self.visible
        if self.visible:
            self.place()
            self.window.show()
        else:
            self.window.hide()

    def stop(self):
        self.window.close()
        self.app.processEvents()
