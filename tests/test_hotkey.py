import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hotkey import ChordFilter, SethHotkey
from trigger import TriggerError


class ChordTests(unittest.TestCase):
    def replay(self, events):
        chord = ChordFilter()
        output, fired = [], []
        for index, event in enumerate(events):
            keys, trigger = chord.feed(*event)
            output.extend(keys)
            if trigger:
                fired.append(index)
        return output, fired

    def test_both_orders_and_both_controls_trigger_once_on_release(self):
        for ctrl in (29, 97):
            for first, second in ((24, 25), (25, 24)):
                events = [(ctrl, 1), (first, 1), (first, 2), (second, 1),
                          (second, 2), (first, 2), (first, 0), (second, 0), (ctrl, 0)]
                output, fired = self.replay(events)
                self.assertEqual(output, [(ctrl, 1), (ctrl, 0)])
                self.assertEqual(fired, [8])

    def test_control_released_first_still_waits_for_letters(self):
        output, fired = self.replay([(29, 1), (25, 1), (24, 1), (29, 0), (25, 0), (24, 0)])
        self.assertEqual(output, [(29, 1), (29, 0)])
        self.assertEqual(fired, [5])

    def test_normal_typing_incomplete_chords_and_extra_modifiers_pass_through(self):
        for events in (
            [(25, 1), (25, 2), (25, 0)],
            [(29, 1), (25, 1), (25, 0), (29, 0)],
            [(29, 1), (24, 1), (29, 0), (24, 0)],
            [(29, 1), (25, 1), (30, 1), (24, 1), (25, 0), (24, 0), (30, 0), (29, 0)],
            [(42, 1), (29, 1), (25, 1), (24, 1), (24, 0), (25, 0), (29, 0), (42, 0)],
            # P was already forwarded before Ctrl: don't consume its release.
            [(25, 1), (29, 1), (24, 1), (24, 0), (25, 0), (29, 0)],
        ):
            with self.subTest(events=events):
                self.assertEqual(self.replay(events), (events, []))

    def test_release_rearms_for_next_question(self):
        chord = [(29, 1), (25, 1), (24, 1), (24, 0), (25, 0), (29, 0)]
        output, fired = self.replay(chord * 2)
        self.assertEqual(fired, [5, 11])
        self.assertEqual(output, [(29, 1), (29, 0)] * 2)

    def test_shift_chord_only_toggles_even_if_shift_releases_first(self):
        for ctrl in (29, 97):
            for shift in (42, 54):
                for first, second in ((24, 25), (25, 24)):
                    chord = ChordFilter(enable_toggle=True)
                    output, actions = [], []
                    events = [(ctrl, 1), (shift, 1), (first, 1), (second, 1), (second, 2),
                              (shift, 0), (first, 0), (second, 0), (ctrl, 0)]
                    for event in events:
                        keys, action = chord.feed(*event)
                        output.extend(keys)
                        if action:
                            actions.append(action)
                    self.assertEqual(actions, ["toggle"])
                    self.assertEqual(output, [(ctrl, 1), (shift, 1), (shift, 0), (ctrl, 0)])

    def test_shift_added_after_letters_upgrades_to_toggle(self):
        chord = ChordFilter(enable_toggle=True)
        actions = [chord.feed(*event)[1] for event in
                   [(29, 1), (25, 1), (24, 1), (42, 1), (42, 0), (24, 0), (25, 0), (29, 0)]]
        self.assertEqual([action for action in actions if action], ["toggle"])


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        self.device = Mock(name="keyboard")
        self.device.name = "Test keyboard"
        self.device.capabilities.return_value = {1: [24, 25, 29, 97]}
        self.device.active_keys.return_value = []
        self.virtual = Mock()
        self.api = SimpleNamespace(
            InputDevice=Mock(return_value=self.device), list_devices=lambda: ["/dev/input/event0"],
            UInput=SimpleNamespace(from_device=Mock(return_value=self.virtual)),
            ecodes=SimpleNamespace(EV_KEY=1, EV_REL=2, EV_SYN=0, SYN_DROPPED=3),
        )
        self.patch = patch.dict(sys.modules, evdev=self.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_events_are_consumed_and_busy_requests_are_not_retried(self):
        callback = Mock(return_value=False)
        hotkey = SethHotkey(callback)
        hotkey.start()
        events = [(29, 1), (25, 1), (24, 1), (24, 2), (24, 0), (25, 0), (29, 0)]
        self.device.read.return_value = [SimpleNamespace(type=1, code=c, value=v) for c, v in events]
        try:
            with patch("hotkey.select.select", return_value=([self.device], [], [])):
                hotkey.poll()
            callback.assert_called_once_with()
            self.assertEqual([call.args for call in self.virtual.write.call_args_list], [(1, 29, 1), (1, 29, 0)])
        finally:
            hotkey.stop()
        self.device.close.assert_called_once()
        self.virtual.close.assert_called_once()

    def test_startup_failure_releases_devices(self):
        self.device.grab.side_effect = PermissionError(13, "denied")
        with self.assertRaisesRegex(TriggerError, "permissions"):
            SethHotkey(Mock()).start()
        self.device.close.assert_called_once()
        self.virtual.close.assert_called_once()

    def test_toggle_and_cycle_dispatch_separately(self):
        callback, toggle = Mock(return_value=True), Mock()
        hotkey = SethHotkey(callback, toggle_callback=toggle)
        hotkey.start()
        events = [(29, 1), (42, 1), (25, 1), (24, 1), (24, 0), (25, 0), (42, 0), (29, 0),
                  (29, 1), (25, 1), (24, 1), (24, 0), (25, 0), (29, 0)]
        self.device.read.return_value = [SimpleNamespace(type=1, code=c, value=v) for c, v in events]
        try:
            with patch("hotkey.select.select", return_value=([self.device], [], [])):
                hotkey.poll()
            toggle.assert_called_once_with()
            callback.assert_called_once_with()
        finally:
            hotkey.stop()

    def test_held_keys_and_missing_keyboards_are_rejected(self):
        self.device.active_keys.return_value = [29]
        with self.assertRaisesRegex(TriggerError, "Release all"):
            SethHotkey(Mock()).start()
        self.device.grab.assert_not_called()
        self.api.list_devices = lambda: []
        with self.assertRaisesRegex(TriggerError, "No accessible keyboard"):
            SethHotkey(Mock()).start()

    def test_dropped_input_stops_instead_of_guessing_key_state(self):
        hotkey = SethHotkey(Mock())
        hotkey.start()
        self.device.read.return_value = [SimpleNamespace(type=0, code=3)]
        try:
            with patch("hotkey.select.select", return_value=([self.device], [], [])):
                with self.assertRaisesRegex(TriggerError, "events were lost"):
                    hotkey.poll()
            hotkey.callback.assert_not_called()
        finally:
            hotkey.stop()


if __name__ == "__main__":
    unittest.main()
