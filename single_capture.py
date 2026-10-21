"""A neutral Single capture and whole-question checks between selections."""

from dataclasses import replace
from pathlib import Path
import time

from PIL import Image, ImageDraw, ImageChops

from control_targets import (CONTROL_HALO, CONTRAST, selection_visible, _background,
                             _small_components, _control_shapes, _outline, _interior,
                             _separated_outline)
from scroll_capture import find_scrollbars
from vbox_backend import VMError, validate_age


def _selected_controls(image, *, include_unchecked=False):
    """Find existing native radio/checkbox selections without trusting model output."""
    gray = image.convert('L')
    rgb = image.convert('RGB')
    background = _background(gray)
    controls, unchecked = [], []
    for component in _small_components(gray, background):
        for candidate, box, local_background in _control_shapes(gray, component, background):
            for kind in ('radio', 'checkbox'):
                if not _outline(candidate, box, kind):
                    continue
                interior = list(_interior(box, kind))
                filled = sum(abs(gray.getpixel(point) - local_background) >= CONTRAST
                             for point in interior)
                blue = sum(rgb.getpixel(point)[2] - rgb.getpixel(point)[0] >= 60
                           for point in interior)
                if filled <= .03 * len(interior) and _separated_outline(gray, box, local_background):
                    unchecked.append({'kind': kind, 'box': list(box)})
                if filled >= .2 * len(interior) and blue >= .2 * len(interior):
                    controls.append({'kind': kind, 'box': list(box)})
                break
    return (controls, unchecked + controls) if include_unchecked else controls


def _mask_existing_focus(image, controls):
    # Preserve the entire original control, including its selected interior.
    for control in controls:
        left, top, right, bottom = control['box']
        original = image.crop((left, top, right, bottom))
        ImageDraw.Draw(image).rectangle((left - CONTROL_HALO, top - CONTROL_HALO,
                                       right + CONTROL_HALO - 1, bottom + CONTROL_HALO - 1), fill='white')
        image.paste(original, (left, top))


def _clear_choice_expansion(before, after, region, controls):
    """Recognize the observed native footer transition without omitting content.

    The two small templates contain only public UI labels and panel borders.
    Native fractional layout rerasterizes these labels, so a translated-pixel
    comparison cannot safely recognize this transition. Other layouts/languages
    fail closed. Every changed content pixel must belong to this exact pair.
    """
    difference = ImageChops.difference(before.crop(region), after.crop(region)).getbbox()
    if not difference or not controls:
        return False
    left, top, right, bottom = difference
    box = (left + region[0], top + region[1], right + region[0], bottom + region[1])
    if max(control['box'][3] + CONTROL_HALO for control in controls) >= box[1]:
        return False
    directory = Path(__file__).parent / 'assets' / 'native_controls'
    for image, name in ((before, 'clear_choice_before.png'), (after, 'clear_choice_after.png')):
        with Image.open(directory / name) as source:
            template = source.convert('RGB')
        crop = image.crop(box)
        if crop.size != template.size or crop.tobytes() != template.tobytes():
            return False
    return True


def _matching_scrollbar_resize(before, after, old):
    candidates = [bar for bar in find_scrollbars(after)
                  if bar.viewport == old.viewport and bar.top == old.top
                  and bar.bottom == old.bottom and bar.panel == old.panel
                  and bar.thumb[0::2] == old.thumb[0::2]]
    if len(candidates) != 1:
        return False
    new = candidates[0]
    if not (new.thumb[1] <= old.thumb[1] and new.thumb[3] <= old.thumb[3]
            and new.thumb[3] - new.thumb[1] < old.thumb[3] - old.thumb[1]):
        return False
    box = (old.thumb[0], old.viewport[0], before.width, old.viewport[1])
    a, b = before.crop(box), after.crop(box)
    union = (0, min(old.thumb[1], new.thumb[1]) - box[1],
             old.thumb[2] - box[0], max(old.thumb[3], new.thumb[3]) - box[1])
    for image in (a, b):
        ImageDraw.Draw(image).rectangle((union[0], union[1], union[2] - 1, union[3] - 1), fill='white')
    return a.tobytes() == b.tobytes()


class SingleQuestion:
    def __init__(self, config, capture, frame, stop_event=None):
        self.capture, self.frame, self.stop_event = capture, frame, stop_event
        self.max_age = config.get('capture', {}).get('max_age_seconds', 150)
        self.selected = []
        self.regions = [(0, 0, frame.display.width, frame.display.height)]
        self.identity = []
        with Image.open(frame.path) as image:
            bars = find_scrollbars(image)
            self.existing, self.controls = _selected_controls(image, include_unchecked=True)
        # A locally recognized native outer viewport excludes the desktop clock.
        # Keep browser chrome as identity, rather than trusting the target alone.
        outer = [bar for bar in bars if bar.viewport
                 and bar.thumb[0] >= frame.display.width * .95
                 and 32 <= bar.viewport[0] <= frame.display.height * .25
                 and bar.viewport[1] >= frame.display.height * .75]
        self.outer = outer[0] if len(outer) == 1 else None
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
        _mask_existing_focus(image, self.existing)
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
            _mask_existing_focus(image, self.existing)
            draw = ImageDraw.Draw(image)
            for point in self.selected:
                left, top, right, bottom = point['target_control']['box']
                draw.rectangle((left - CONTROL_HALO, top - CONTROL_HALO,
                                right + CONTROL_HALO - 1, bottom + CONTROL_HALO - 1), fill='white')
        expanded = bool(self.selected and self.outer and
                        _clear_choice_expansion(before, after, self.regions[0], self.controls))
        if expanded and not _matching_scrollbar_resize(before, after, self.outer):
            expanded = False
        boxes = self.identity[:1] if expanded else self.identity + self.regions
        for box in boxes:
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
