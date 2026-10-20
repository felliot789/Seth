"""A neutral Single capture and whole-question checks between selections."""

from dataclasses import replace
import time

from PIL import Image, ImageDraw

from control_targets import CONTROL_HALO, selection_visible
from scroll_capture import find_scrollbars
from vbox_backend import VMError, validate_age


class SingleQuestion:
    def __init__(self, config, capture, frame, stop_event=None):
        self.capture, self.frame, self.stop_event = capture, frame, stop_event
        self.max_age = config.get('capture', {}).get('max_age_seconds', 150)
        self.selected = []
        self.regions = [(0, 0, frame.display.width, frame.display.height)]
        self.identity = []
        with Image.open(frame.path) as image:
            bars = find_scrollbars(image)
        # A locally recognized native outer viewport excludes the desktop clock.
        # Keep browser chrome as identity, rather than trusting the target alone.
        outer = [bar for bar in bars if bar.viewport
                 and bar.thumb[0] >= frame.display.width * .95
                 and 32 <= bar.viewport[0] <= frame.display.height * .25
                 and bar.viewport[1] >= frame.display.height * .75]
        if len(outer) == 1:
            top, bottom = outer[0].viewport
            self.regions = [(0, top, outer[0].thumb[0], bottom)]
            self.identity = [(0, 0, frame.display.width, top),
                             (outer[0].thumb[0], top, frame.display.width, bottom)]

    def _check(self, fresh):
        if self.stop_event is not None and self.stop_event.is_set():
            raise VMError('Cycle cancelled; no further selections.')
        validate_age(self.frame.captured_at, self.max_age)
        if fresh.display != self.frame.display:
            raise VMError('VM display changed after capture; no further input.')

    def _content(self, frame):
        with Image.open(frame.path) as source:
            image = source.convert('RGB')
        return b''.join(image.crop(box).tobytes() for box in self.identity + self.regions)

    def verify(self):
        self._check(self.frame)
        self.capture.backend.park_pointer(self.frame, self.max_age, self.stop_event)
        self._check(self.frame)
        fresh = self.capture.capture_frame()
        self._check(fresh)
        with Image.open(self.frame.path) as source, Image.open(fresh.path) as current:
            before, after = source.convert('RGB'), current.convert('RGB')
        # Only already confirmed control interiors/focus may change. Preserve
        # every other question and option pixel, including text near the target.
        for point in self.selected:
            control = point['target_control']
            if not selection_visible(before, after, control):
                raise VMError('A previously confirmed answer changed; no further input.')
        for image in (before, after):
            draw = ImageDraw.Draw(image)
            for point in self.selected:
                left, top, right, bottom = point['target_control']['box']
                draw.rectangle((left - CONTROL_HALO, top - CONTROL_HALO,
                                right + CONTROL_HALO - 1, bottom + CONTROL_HALO - 1), fill='white')
        for box in self.identity + self.regions:
            if before.crop(box).tobytes() != after.crop(box).tobytes():
                raise VMError('Question or answer content changed after inference; start a fresh cycle.')
        # Keep the inference deadline even though execution uses fresh pixels.
        return replace(fresh, captured_at=self.frame.captured_at)

    def confirm(self, point):
        left, top, right, bottom = point['target_control']['box']
        if not any(a <= left < right <= c and b <= top < bottom <= d
                   for a, b, c, d in self.regions):
            raise VMError('Answer control lies outside the verified question area.')
        self.selected.append(point)
        self.verify()


def prepare_single(config, capture, initial, stop_event=None):
    """Remove initial hover before inference and require a settled baseline."""
    age = config.get('capture', {}).get('max_age_seconds', 150)
    if stop_event is not None and stop_event.is_set():
        raise VMError('Cycle cancelled; no further input.')
    capture.backend.park_pointer(initial, age, stop_event)
    if stop_event is not None and stop_event.is_set():
        raise VMError('Cycle cancelled; no further input.')
    neutral = capture.capture_frame()
    if neutral.display != initial.display:
        raise VMError('VM display changed while clearing hover; no further input.')
    question = SingleQuestion(config, capture, neutral, stop_event)
    previous, deadline = question._content(neutral), time.monotonic() + 3
    while True:
        if stop_event is not None:
            stop_event.wait(.1)
        else:
            time.sleep(.1)
        question._check(question.frame)
        fresh = capture.capture_frame()
        question._check(fresh)
        content = question._content(fresh)
        if content == previous:
            settled = SingleQuestion(config, capture, fresh, stop_event)
            if (settled.regions, settled.identity) == (question.regions, question.identity):
                return settled
            # A resize/reflow can reveal content outside the earlier viewport.
            # Derive scopes from the final pixels and settle under those scopes.
            question = settled
            content = question._content(fresh)
        if time.monotonic() >= deadline:
            raise VMError('The question did not settle after clearing hover; no inference sent.')
        previous = content
