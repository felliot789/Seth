"""Linux run, indicator, text-color, provider, and capture chords, including on Wayland."""

import logging
import select

from trigger import TriggerError

LOG = logging.getLogger("seth")
# Stable Linux input-event key codes. Import evdev only when the shortcut is used.
CTRL = {29, 97}
SHIFT = {42, 54}
LETTERS = {32, 33}  # D, F
SELECT_LETTERS = {18, 19}  # E, R
COLOR_LETTERS = {16, 17}  # Q, W


class ChordFilter:
    """Delay Ctrl+D/F until release; consume both when the chord completes.

    Fire only after all chord keys are released so Ctrl cannot modify VM clicks.
    Holding keys, autorepeat, or an incomplete chord never queues extra cycles.
    """

    def __init__(self, enable_toggle=False, enable_provider=False, enable_capture=False, enable_text_color=False):
        self.down = set()
        self.pending = []
        self.pending_group = None
        self.matched = False
        self.matched_group = set()
        self.cancelled = False
        self.blocked = False
        self.bindings = {frozenset(LETTERS): ("cycle", "toggle" if enable_toggle else None)}
        if enable_provider or enable_capture:
            self.bindings[frozenset(SELECT_LETTERS)] = (
                "provider" if enable_provider else None, "capture" if enable_capture else None)
        if enable_text_color:
            self.bindings[frozenset(COLOR_LETTERS)] = (None, "text_color")
        self.letters = set().union(*self.bindings)
        self.chord_keys = CTRL | SHIFT | self.letters

    def _match_pending(self, group, action):
        # Shift may arrive between the letters of a Shift-only binding. Keep
        # its original order if the chord is incomplete, or forward only the
        # modifiers once the completed letter chord is consumed.
        modifiers = [event for event in self.pending if event[0] in SHIFT]
        self.pending.clear()
        self.pending_group = None
        self.matched_group = set(group)
        self.matched = action
        return modifiers

    def feed(self, code, value):
        if value == 1:
            self.down.add(code)
        elif value == 0:
            self.down.discard(code)
        output = []
        fired = False
        if self.matched:
            if code in SHIFT and value == 1:
                shifted = self.bindings[frozenset(self.matched_group)][1]
                if shifted:
                    self.matched = shifted
                else:
                    self.cancelled = True
            elif value == 1 and code not in CTRL | SHIFT | self.matched_group:
                self.cancelled = True
            if code not in self.matched_group:
                output.append((code, value))
            if not self.down & self.chord_keys:
                fired = self.matched if not self.cancelled else False
                self.matched = False
                self.matched_group = set()
                self.cancelled = self.blocked = False
            return output, fired

        if (self.pending_group and code in SHIFT and value in (0, 1)
                and self.bindings[self.pending_group][0] is None
                and self.down & CTRL and self.down <= CTRL | SHIFT | self.pending_group):
            self.pending.append((code, value))
            if (self.down & SHIFT and self.pending_group <= self.down):
                group = self.pending_group
                output.extend(self._match_pending(group, self.bindings[group][1]))
            return output, False

        group = next((keys for keys in self.bindings if code in keys), None)
        shifted = bool(self.down & SHIFT)
        action = self.bindings[group][int(shifted)] if group else None
        can_delay = group and (action or self.bindings[group][0] is None and self.bindings[group][1])
        if (not self.blocked and can_delay and self.down & CTRL
                and self.down <= CTRL | SHIFT | group
                and self.pending_group in (None, group) and value in (1, 2)):
            # Autorepeat isn't a second press; don't accumulate repeated events.
            if value == 1:
                self.pending.append((code, value))
                self.pending_group = group
            if action and group <= {key for key, _ in self.pending}:
                output.extend(self._match_pending(group, action))
            return output, False

        # An incomplete chord remains an ordinary Ctrl+D or Ctrl+F shortcut.
        # Flush before Ctrl-up or another key to preserve the original ordering.
        if self.pending:
            output.extend(self.pending)
            self.pending.clear()
            self.pending_group = None
            self.blocked = True
        output.append((code, value))
        if not self.down & self.chord_keys:
            self.blocked = False
        return output, False


class SethHotkey:
    def __init__(self, callback, devices=None, toggle_callback=None, provider_callback=None, capture_callback=None,
                 text_color_callback=None):
        self.callback = callback
        self.toggle_callback = toggle_callback
        self.provider_callback = provider_callback
        self.capture_callback = capture_callback
        self.text_color_callback = text_color_callback
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
                    required = LETTERS | (SELECT_LETTERS if self.provider_callback or self.capture_callback else set())
                    if self.text_color_callback is not None:
                        required |= COLOR_LETTERS
                    if (device.name.startswith("Seth shortcut") or not required <= keys
                            or not keys & CTRL or (self.paths is None and evdev.ecodes.EV_REL in caps)):
                        continue
                    if device.active_keys():
                        raise TriggerError("Release all keyboard keys before starting the shortcut listener.")
                    virtual = evdev.UInput.from_device(device, name="Seth shortcut keyboard")
                    device.grab()
                    self.devices.append((device, virtual, ChordFilter(
                        enable_toggle=self.toggle_callback is not None,
                        enable_provider=self.provider_callback is not None,
                        enable_capture=self.capture_callback is not None,
                        enable_text_color=self.text_color_callback is not None)))
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
                        LOG.info("Ctrl+Shift+D+F: indicator display cycled")
                    elif fired == "text_color":
                        color = self.text_color_callback()
                        LOG.info("Ctrl+Shift+Q+W: indicator text is %s", color)
                    elif fired == "provider":
                        accepted = self.provider_callback()
                        LOG.info("Ctrl+E+R: %s", "provider changed" if accepted else "busy; switch discarded")
                    elif fired == "capture":
                        accepted = self.capture_callback()
                        LOG.info("Ctrl+Shift+E+R: %s", "capture mode changed" if accepted else "busy; switch discarded")
                    elif fired == "cycle":
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
