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
            for first, second in ((33, 32), (32, 33)):
                events = [(ctrl, 1), (first, 1), (first, 2), (second, 1),
                          (second, 2), (first, 2), (first, 0), (second, 0), (ctrl, 0)]
                output, fired = self.replay(events)
                self.assertEqual(output, [(ctrl, 1), (ctrl, 0)])
                self.assertEqual(fired, [8])

    def test_control_released_first_still_waits_for_letters(self):
        output, fired = self.replay([(29, 1), (32, 1), (33, 1), (29, 0), (32, 0), (33, 0)])
        self.assertEqual(output, [(29, 1), (29, 0)])
        self.assertEqual(fired, [5])

    def test_normal_typing_incomplete_chords_and_extra_modifiers_pass_through(self):
        for events in (
            [(32, 1), (32, 2), (32, 0)],
            [(29, 1), (32, 1), (32, 0), (29, 0)],
            [(29, 1), (33, 1), (29, 0), (33, 0)],
            [(29, 1), (32, 1), (30, 1), (33, 1), (32, 0), (33, 0), (30, 0), (29, 0)],
            [(42, 1), (29, 1), (32, 1), (33, 1), (33, 0), (32, 0), (29, 0), (42, 0)],
            # D was already forwarded before Ctrl: don't consume its release.
            [(32, 1), (29, 1), (33, 1), (33, 0), (32, 0), (29, 0)],
        ):
            with self.subTest(events=events):
                self.assertEqual(self.replay(events), (events, []))

    def test_release_rearms_for_next_question(self):
        chord = [(29, 1), (32, 1), (33, 1), (33, 0), (32, 0), (29, 0)]
        output, fired = self.replay(chord * 2)
        self.assertEqual(fired, [5, 11])
        self.assertEqual(output, [(29, 1), (29, 0)] * 2)

    def test_former_chords_pass_through_immediately_without_triggering(self):
        for enable_toggle in (False, True):
            for ctrl in (29, 97):
                for shift in (None, 42, 54):
                    for first, second in ((24, 25), (25, 24)):
                        with self.subTest(enable_toggle=enable_toggle, ctrl=ctrl,
                                          shift=shift, first=first):
                            chord = ChordFilter(enable_toggle=enable_toggle)
                            modifiers = [ctrl] + ([shift] if shift is not None else [])
                            events = ([(key, 1) for key in modifiers]
                                      + [(first, 1), (first, 2), (second, 1),
                                         (second, 0), (first, 0)]
                                      + [(key, 0) for key in reversed(modifiers)])
                            for event in events:
                                self.assertEqual(chord.feed(*event), ([event], False))

    def test_shift_chord_only_toggles_even_if_shift_releases_first(self):
        for ctrl in (29, 97):
            for shift in (42, 54):
                for first, second in ((33, 32), (32, 33)):
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
                   [(29, 1), (32, 1), (33, 1), (42, 1), (42, 0), (33, 0), (32, 0), (29, 0)]]
        self.assertEqual([action for action in actions if action], ["toggle"])


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        self.device = Mock(name="keyboard")
        self.device.name = "Test keyboard"
        self.device.capabilities.return_value = {1: [33, 32, 29, 97]}
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
        events = [(29, 1), (32, 1), (33, 1), (33, 2), (33, 0), (32, 0), (29, 0)]
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
        events = [(29, 1), (42, 1), (32, 1), (33, 1), (33, 0), (32, 0), (42, 0), (29, 0),
                  (29, 1), (32, 1), (33, 1), (33, 0), (32, 0), (29, 0)]
        self.device.read.return_value = [SimpleNamespace(type=1, code=c, value=v) for c, v in events]
        try:
            with patch("hotkey.select.select", return_value=([self.device], [], [])):
                hotkey.poll()
            toggle.assert_called_once_with()
            callback.assert_called_once_with()
        finally:
            hotkey.stop()

    def test_former_shortcuts_do_not_invoke_callbacks(self):
        callback, toggle = Mock(return_value=True), Mock()
        hotkey = SethHotkey(callback, toggle_callback=toggle)
        hotkey.start()
        events = [(29, 1), (25, 1), (24, 1), (24, 0), (25, 0), (29, 0),
                  (29, 1), (42, 1), (25, 1), (24, 1), (24, 0), (25, 0), (42, 0), (29, 0)]
        self.device.read.return_value = [SimpleNamespace(type=1, code=c, value=v) for c, v in events]
        try:
            with patch("hotkey.select.select", return_value=([self.device], [], [])):
                hotkey.poll()
            callback.assert_not_called()
            toggle.assert_not_called()
            self.assertEqual([call.args for call in self.virtual.write.call_args_list],
                             [(1, code, value) for code, value in events])
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

    def test_all_six_actions_dispatch_only_to_their_own_callback(self):
        self.device.capabilities.return_value = {1: [16, 17, 18, 19, 32, 33, 34, 29, 42]}
        run, indicator, provider, capture, text_color, interrupt = (Mock(return_value=True) for _ in range(6))
        hotkey = SethHotkey(run, toggle_callback=indicator, provider_callback=provider, capture_callback=capture,
                           text_color_callback=text_color, interrupt_callback=interrupt)
        hotkey.start()
        events = []
        for letters, shifted in (((32, 33), False), ((32, 33), True), ((18, 19), False), ((18, 19), True), ((16, 17), True), ((34,), True)):
            modifiers = [29] + ([42] if shifted else [])
            events += [(key, 1) for key in modifiers + list(letters)]
            events += [(key, 0) for key in reversed(modifiers + list(letters))]
        self.device.read.return_value = [SimpleNamespace(type=1, code=c, value=v) for c, v in events]
        try:
            with patch('hotkey.select.select', return_value=([self.device], [], [])):
                hotkey.poll()
            for callback in (run, indicator, provider, capture, text_color, interrupt):
                callback.assert_called_once_with()
            self.assertTrue(all(call.args[1] in (29, 42) for call in self.virtual.write.call_args_list))
        finally:
            hotkey.stop()


class TextColorChordTests(unittest.TestCase):
    def replay(self, events, enabled=True):
        chord = ChordFilter(enable_text_color=enabled)
        output, actions = [], []
        for index, event in enumerate(events):
            forwarded, action = chord.feed(*event)
            output.extend(forwarded)
            if action:
                actions.append((index, action))
        return output, actions

    def test_modifier_orders_letter_orders_and_repeats_trigger_once_on_release(self):
        for ctrl in (29, 97):
            for shift in (42, 54):
                for modifiers in ((ctrl, shift), (shift, ctrl)):
                    for first, second in ((16, 17), (17, 16)):
                        events = ([(key, 1) for key in modifiers] + [(first, 1), (first, 2), (second, 1), (second, 2),
                                  (ctrl, 0), (first, 0), (second, 0), (shift, 0)])
                        with self.subTest(events=events):
                            output, actions = self.replay(events)
                            self.assertEqual(output, [event for event in events if event[0] in modifiers])
                            self.assertEqual(actions, [(len(events) - 1, 'text_color')])

    def test_shift_can_arrive_between_or_after_both_letters(self):
        for middle in ([(16, 1), (54, 1), (17, 1)], [(16, 1), (17, 1), (54, 1)]):
            events = [(29, 1)] + middle + [(54, 0), (17, 0), (16, 0), (29, 0)]
            self.assertEqual(self.replay(events), ([(29, 1), (54, 1), (54, 0), (29, 0)], [(7, 'text_color')]))

    def test_unshifted_partial_mixed_and_disabled_chords_preserve_original_event_order(self):
        cases = [
            [(29, 1), (16, 1), (17, 1), (17, 0), (16, 0), (29, 0)],
            [(29, 1), (42, 1), (16, 1), (16, 0), (42, 0), (29, 0)],
            [(29, 1), (16, 1), (54, 1), (54, 0), (16, 0), (29, 0)],
            [(29, 1), (16, 1), (54, 1), (54, 0), (17, 1), (17, 0), (16, 0), (29, 0)],
            [(29, 1), (16, 1), (54, 1), (18, 1), (18, 0), (16, 0), (54, 0), (29, 0)],
            [(56, 1), (29, 1), (42, 1), (16, 1), (17, 1), (17, 0), (16, 0), (42, 0), (29, 0), (56, 0)],
        ]
        for events in cases:
            with self.subTest(events=events):
                self.assertEqual(self.replay(events), (events, []))
        events = [(29, 1), (42, 1), (16, 1), (17, 1), (17, 0), (16, 0), (42, 0), (29, 0)]
        self.assertEqual(self.replay(events, enabled=False), (events, []))

    def test_full_release_rearms_and_an_extra_key_cancels_a_completed_chord(self):
        events = [(29, 1), (42, 1), (16, 1), (17, 1), (17, 0), (16, 0), (42, 0), (29, 0)]
        self.assertEqual(self.replay(events * 2)[1], [(7, 'text_color'), (15, 'text_color')])
        cancelled = events[:4] + [(30, 1), (30, 0)] + events[4:]
        self.assertEqual(self.replay(cancelled)[1], [])


class InterruptChordTests(unittest.TestCase):
    def replay(self, events, enabled=True):
        chord = ChordFilter(enable_interrupt=enabled)
        output, actions = [], []
        for index, event in enumerate(events):
            forwarded, action = chord.feed(*event)
            output.extend(forwarded)
            if action:
                actions.append((index, action))
        return output, actions

    def test_both_modifiers_orders_and_repeat_fire_once_after_release(self):
        for ctrl in (29, 97):
            for shift in (42, 54):
                for presses in ((ctrl, shift, 34), (shift, ctrl, 34), (ctrl, 34, shift)):
                    events = [(key, 1) for key in presses] + [(34, 2), (ctrl, 0), (34, 0), (shift, 0)]
                    output, actions = self.replay(events)
                    self.assertEqual(output, [event for event in events if event[0] != 34])
                    self.assertEqual(actions, [(6, 'interrupt')])

    def test_plain_g_control_g_shift_g_and_disabled_binding_pass_through(self):
        for keys in ((34,), (29, 34), (42, 34), (56, 29, 42, 34), (34, 29, 42)):
            events = [(key, 1) for key in keys] + [(key, 0) for key in reversed(keys)]
            self.assertEqual(self.replay(events), (events, []))
        events = [(key, 1) for key in (29, 42, 34)] + [(key, 0) for key in (34, 42, 29)]
        self.assertEqual(self.replay(events, enabled=False), (events, []))
        self.assertEqual(self.replay(events * 2)[1], [(5, 'interrupt'), (11, 'interrupt')])


class SelectionChordTests(unittest.TestCase):
    def replay(self, events, **options):
        chord = ChordFilter(enable_toggle=True, enable_provider=True, enable_capture=True, **options)
        output, actions = [], []
        for index, event in enumerate(events):
            forwarded, action = chord.feed(*event)
            output.extend(forwarded)
            if action:
                actions.append((index, action))
        return output, actions

    def test_both_letter_orders_modifiers_and_autorepeat_fire_once_after_release(self):
        for ctrl in (29, 97):
            for shift in (None, 42, 54):
                for letters in ((18, 19), (19, 18)):
                    with self.subTest(ctrl=ctrl, shift=shift, letters=letters):
                        modifiers = [ctrl] + ([shift] if shift else [])
                        events = ([(key, 1) for key in modifiers] + [(letters[0], 1), (letters[0], 2),
                                  (letters[1], 1), (letters[1], 2), (ctrl, 0), (letters[0], 0), (letters[1], 0)]
                                  + ([(shift, 0)] if shift else []))
                        output, actions = self.replay(events)
                        self.assertEqual(output, [(key, value) for key, value in events if key in modifiers])
                        self.assertEqual(actions, [(len(events) - 1, 'capture' if shift else 'provider')])

    def test_shift_added_late_selects_capture_instead_of_provider(self):
        events = [(29, 1), (18, 1), (19, 1), (54, 1), (54, 0), (18, 0), (19, 0), (29, 0)]
        self.assertEqual(self.replay(events)[1], [(7, 'capture')])

    def test_partial_mixed_or_unsupported_chords_pass_through_without_an_action(self):
        for events in (
            [(29, 1), (18, 1), (18, 0), (29, 0)],
            [(29, 1), (19, 1), (29, 0), (19, 0)],
            [(29, 1), (18, 1), (33, 1), (33, 0), (18, 0), (29, 0)],
            [(56, 1), (29, 1), (18, 1), (19, 1), (19, 0), (18, 0), (29, 0), (56, 0)],
            [(18, 1), (29, 1), (19, 1), (19, 0), (29, 0), (18, 0)],
        ):
            with self.subTest(events=events):
                self.assertEqual(self.replay(events), (events, []))

    def test_unrelated_key_after_a_completed_chord_cancels_the_action(self):
        for extra in (32, 56, 30):
            events = [(29, 1), (18, 1), (19, 1), (extra, 1), (extra, 0), (18, 0), (19, 0), (29, 0)]
            self.assertEqual(self.replay(events)[1], [])

    def test_disabled_selection_shortcuts_pass_through_and_full_release_rearms(self):
        events = [(29, 1), (18, 1), (19, 1), (19, 0), (18, 0), (29, 0)]
        chord = ChordFilter(enable_toggle=True)
        for event in events:
            self.assertEqual(chord.feed(*event), ([event], False))
        output, actions = self.replay(events * 2)
        self.assertEqual(actions, [(5, 'provider'), (11, 'provider')])


if __name__ == "__main__":
    unittest.main()
