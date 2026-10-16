"""Linux Ctrl+D+F trigger with chord suppression, including on Wayland."""

import logging
import select

from trigger import TriggerError

LOG = logging.getLogger("seth")
# Stable Linux input-event key codes. Import evdev only when the shortcut is used.
CTRL = {29, 97}
SHIFT = {42, 54}
LETTERS = {32, 33}  # D, F
CHORD = CTRL | SHIFT | LETTERS


class ChordFilter:
    """Delay Ctrl+D/F until release; consume both when the chord completes.

    Fire only after all chord keys are released so Ctrl cannot modify VM clicks.
    Holding keys, autorepeat, or an incomplete chord never queues extra cycles.
    """

    def __init__(self, enable_toggle=False):
        self.down = set()
        self.pending = []
        self.matched = False
        self.blocked = False
        self.enable_toggle = enable_toggle

    def feed(self, code, value):
        if value == 1:
            self.down.add(code)
        elif value == 0:
            self.down.discard(code)
        output = []
        fired = False
        if self.matched:
            if self.enable_toggle and code in SHIFT and value == 1:
                self.matched = "toggle"
            if code not in LETTERS:
                output.append((code, value))
            if not self.down & CHORD:
                fired = self.matched
                self.matched = False
            return output, fired

        if (not self.blocked and code in LETTERS and self.down & CTRL
                and self.down <= (CHORD if self.enable_toggle else CTRL | LETTERS)
                and value in (1, 2)):
            # Autorepeat isn't a second press; don't accumulate repeated events.
            if value == 1:
                self.pending.append((code, value))
            if LETTERS <= {key for key, _ in self.pending}:
                self.pending.clear()
                self.matched = "toggle" if self.down & SHIFT else "cycle"
            return output, False

        # An incomplete chord remains an ordinary Ctrl+D or Ctrl+F shortcut.
        # Flush before Ctrl-up or another key to preserve the original ordering.
        if self.pending:
            output.extend(self.pending)
            self.pending.clear()
            self.blocked = True
        output.append((code, value))
        if not self.down & CHORD:
            self.blocked = False
        return output, False


class SethHotkey:
    def __init__(self, callback, devices=None, toggle_callback=None):
        self.callback = callback
        self.toggle_callback = toggle_callback
        self.paths = devices
        self.devices = []

    def start(self):
        try:
            import evdev
        except ImportError:
            raise TriggerError("Ctrl+D+F needs evdev. Install requirements.txt in your venv.") from None
        self.evdev = evdev
        if self.paths is not None and (not isinstance(self.paths, list)
                                      or not all(isinstance(p, str) for p in self.paths)):
            raise TriggerError("hotkey.devices must be a list of keyboard event device paths.")
        try:
            for path in self.paths if self.paths is not None else evdev.list_devices():
                device = evdev.InputDevice(path)
                virtual = None
                try:
                    caps = device.capabilities()
                    keys = set(caps.get(evdev.ecodes.EV_KEY, []))
                    # Skip mice advertising keyboard keys and our own virtual devices.
                    if (device.name.startswith("Seth shortcut") or not LETTERS <= keys
                            or not keys & CTRL or (self.paths is None and evdev.ecodes.EV_REL in caps)):
                        continue
                    if device.active_keys():
                        raise TriggerError("Release all keyboard keys before starting the shortcut listener.")
                    virtual = evdev.UInput.from_device(device, name="Seth shortcut keyboard")
                    device.grab()
                    self.devices.append((device, virtual, ChordFilter(enable_toggle=self.toggle_callback is not None)))
                    LOG.info("Shortcut keyboard: %s (%s)", device.name, path)
                    device = virtual = None
                finally:
                    if virtual is not None:
                        virtual.close()
                    if device is not None:
                        device.close()
            if not self.devices:
                raise TriggerError("No accessible keyboard for Ctrl+D+F. Run the keyboard permission setup in README.md.")
        except OSError as error:
            self.stop()
            raise TriggerError("Cannot open/grab the keyboard or /dev/uinput for Ctrl+D+F. "
                               "Check keyboard permissions (README.md) and other shortcut listeners. "
                               f"System error: {error.strerror}") from None
        except Exception:
            self.stop()
            raise

    def poll(self):
        ready, _, _ = select.select([item[0] for item in self.devices], [], [], 0)
        for device, virtual, chord in self.devices:
            if device not in ready:
                continue
            try:
                events = list(device.read())
            except BlockingIOError:
                continue
            for event in events:
                if event.type == self.evdev.ecodes.EV_SYN and event.code == self.evdev.ecodes.SYN_DROPPED:
                    raise TriggerError("Keyboard events were lost; restart the shortcut listener.")
                if event.type == self.evdev.ecodes.EV_KEY:
                    forwarded, fired = chord.feed(event.code, event.value)
                    for code, value in forwarded:
                        virtual.write(self.evdev.ecodes.EV_KEY, code, value)
                    virtual.syn()
                    if fired == "toggle":
                        self.toggle_callback()
                        LOG.info("Ctrl+Shift+D+F: indicator visibility toggled")
                    elif fired:
                        accepted = self.callback()
                        LOG.info("Ctrl+D+F: %s", "cycle requested" if accepted else "busy; request discarded")
                else:
                    virtual.write_event(event)

    def stop(self):
        while self.devices:
            device, virtual, _ = self.devices.pop()
            try:
                device.close()  # Closing also releases the exclusive grab.
            finally:
                virtual.close()
