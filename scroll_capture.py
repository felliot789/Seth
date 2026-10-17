"""Bounded screenshot-only context capture for native Chromium quiz scrollbars."""

from dataclasses import dataclass, field
import math
import time

from PIL import Image

from control_targets import _components
from vbox_backend import VMError


class ScrollLimitError(VMError):
    def __init__(self, required_views, max_views):
        super().__init__(f'Question requires {required_views} scroll views, exceeding the configured limit of '
                         f'{max_views}; no scrolling or inference attempted.')
        self.required_views, self.max_views = required_views, max_views


class ScrollNavigationError(VMError):
    pass


@dataclass(frozen=True)
class Scrollbar:
    thumb: tuple
    top: int
    bottom: int
    panel: tuple

    @property
    def height(self):
        return self.thumb[3] - self.thumb[1]

    def destination(self, fraction):
        return (round((self.thumb[0] + self.thumb[2] - 1) / 2),
                round(self.top + self.height / 2 + fraction * (self.bottom - self.top - self.height)))


def pixel_box(normalized, size):
    if (not isinstance(normalized, (list, tuple)) or len(normalized) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in normalized)
            or not 0 <= normalized[0] < normalized[2] <= 1
            or not 0 <= normalized[1] < normalized[3] <= 1):
        raise VMError('Scroll capture regions must be normalized left/top/right/bottom rectangles.')
    return tuple(round(v * size[i % 2]) for i, v in enumerate(normalized))


def find_scrollbar(image, region):
    """Require one solid gray thumb and matching native up/down arrow buttons."""
    left, top, right, bottom = region
    # Scrollbars must be at the right edge of their configured question column.
    left = max(left, right - round((right - left) * .22))
    crop = image.crop((left, top, right, bottom)).convert('RGB')
    gray = crop.convert('L')
    mask = {(x, y) for y in range(gray.height) for x in range(gray.width)
            if 70 <= gray.getpixel((x, y)) <= 170}
    shapes = []
    for component in _components(mask):
        xs, ys = zip(*component)
        box = min(xs), min(ys), max(xs) + 1, max(ys) + 1
        w, h = box[2] - box[0], box[3] - box[1]
        if not 8 <= w <= 22 or h < 5:
            continue
        if sum(max(crop.getpixel(p)) - min(crop.getpixel(p)) <= 3 for p in component) < .95 * len(component):
            continue
        shapes.append((box, component, len(component) / (w * h)))
    found = []
    for thumb, _, density in shapes:
        w, h = thumb[2] - thumb[0], thumb[3] - thumb[1]
        if h < 40 or h < 3 * w or density < .8:
            continue
        center = (thumb[0] + thumb[2]) / 2
        arrows = []
        for box, component, fill in shapes:
            ah = box[3] - box[1]
            if not 5 <= ah <= 16 or not .3 <= fill <= .85 or abs((box[0] + box[2]) / 2 - center) > 2:
                continue
            upper = sum(y < box[1] + ah / 3 for _, y in component)
            lower = sum(y >= box[3] - ah / 3 for _, y in component)
            if box[3] < thumb[1] and upper < lower:
                arrows.append(('up', box))
            elif box[1] > thumb[3] and upper > lower:
                arrows.append(('down', box))
        ups = [box for kind, box in arrows if kind == 'up']
        downs = [box for kind, box in arrows if kind == 'down']
        if len(ups) != 1 or len(downs) != 1:
            continue
        track_top, track_bottom = ups[0][3] + top, downs[0][1] + top
        if track_bottom - track_top < 100 or h >= track_bottom - track_top:
            continue
        actual_thumb = (thumb[0] + left, thumb[1] + top, thumb[2] + left, thumb[3] + top)
        panel = (region[0], max(region[1], ups[0][1] + top - 8),
                 min(region[2], actual_thumb[2] + 8), min(region[3], downs[0][3] + top + 8))
        found.append(Scrollbar(actual_thumb, track_top, track_bottom, panel))
    if len(found) > 1:
        raise VMError('More than one native scrollbar matched a question column; no scrolling attempted.')
    return found[0] if found else None


@dataclass
class ScrollView:
    key: str
    panel: int
    frame: object
    region: tuple
    bar: Scrollbar
    fraction: float
    label: str


@dataclass
class ScrolledQuestion:
    frame: object
    contexts: list
    regions: list
    identity_box: tuple
    selected_controls: list = field(default_factory=list)
    views: list = field(default_factory=list)

    def navigate(self, config, capture, stop_event=None, report=lambda message: None):
        from scroll_navigation import ScrollNavigator
        return ScrollNavigator(self, config, capture, stop_event, report)

    def verify_identity(self, capture, stop_event=None):
        if stop_event is not None and stop_event.is_set():
            raise VMError('Cycle cancelled; no further input.')
        fresh = capture.capture_frame()
        if fresh.display != self.frame.display:
            raise VMError('VM display changed after context capture; no input.')
        with Image.open(self.frame.path) as expected, Image.open(fresh.path) as actual:
            if expected.crop(self.identity_box).tobytes() != actual.crop(self.identity_box).tobytes():
                raise VMError('Question header changed after context capture; start a fresh cycle.')
            expected, actual = expected.convert('RGB'), actual.convert('RGB')
            # Prior verified selections may legitimately change color/focus.
            # Everything else in the captured question columns must still match.
            for control in self.selected_controls:
                left, top, right, bottom = control['target_control']['box']
                box = (max(0, left - 24), max(0, top - 24),
                       min(expected.width, right + 24), min(expected.height, bottom + 24))
                actual.paste(expected.crop(box), box)
            for left, top, right, bottom in self.regions:
                box = (left, top, max(left + 1, right - 24), bottom)
                if expected.crop(box).tobytes() != actual.crop(box).tobytes():
                    raise VMError('Question or answer view changed after inference; start a fresh cycle.')


class ScrollCollector:
    def __init__(self, config, capture, stop_event=None, report=lambda message: None):
        self.config, self.capture = config, capture
        self.options = config.get('capture', {}).get('scroll_context', {})
        self.stop_event, self.report = stop_event, report

    def check(self, reference, fresh, identity_box):
        if self.stop_event is not None and self.stop_event.is_set():
            raise VMError('Scroll capture cancelled; no further input.')
        if fresh.display != reference.display:
            raise VMError('VM display changed during scroll capture; no further input.')
        with Image.open(reference.path) as before, Image.open(fresh.path) as after:
            if before.crop(identity_box).tobytes() != after.crop(identity_box).tobytes():
                raise VMError('Question header changed during scroll capture; no further input.')

    def settled(self, reference, region, identity_box):
        deadline, previous = time.monotonic() + 3, None
        while True:
            fresh = self.capture.capture_frame()
            self.check(reference, fresh, identity_box)
            with Image.open(fresh.path) as image:
                # Ignore the scrollbar itself, whose hover state may change.
                content = image.crop((region[0], region[1], region[2] - 24, region[3])).tobytes()
            if content == previous:
                return fresh
            if time.monotonic() >= deadline:
                raise VMError('The scrolled question did not settle; no answer input.')
            previous = content
            if self.stop_event is not None:
                self.stop_event.wait(.15)
            else:
                time.sleep(.15)

    def collect(self, initial):
        if not self.options.get('enabled', False):
            return ScrolledQuestion(initial, [], [], ())
        size = initial.display.width, initial.display.height
        regions = [pixel_box(box, size) for box in self.options.get('regions', [])]
        if not 1 <= len(regions) <= 4:
            raise VMError('Scroll capture needs one to four configured question columns.')
        identity = pixel_box(self.options.get('identity_box', [.05, .15, .35, .28]), size)
        limit = self.options.get('max_views', 12)
        if type(limit) is not int or not 2 <= limit <= 12:
            raise VMError('Scroll capture max_views must be between 2 and 12.')
        with Image.open(initial.path) as image:
            bars = [(region, find_scrollbar(image, region)) for region in regions]
        plans = []
        for region, bar in bars:
            if bar is None:
                continue
            # At least 35% overlap, even with native thumb/arrow padding.
            intervals = max(1, math.ceil((bar.bottom - bar.top - bar.height) / (.65 * bar.height)))
            plans.append((region, bar, intervals))
        required_views = sum(intervals + 1 for _, _, intervals in plans)
        if required_views > limit:
            raise ScrollLimitError(required_views, limit)
        if not plans:
            return ScrolledQuestion(initial, [], regions, identity)
        current, contexts, views = initial, [], []
        for index, (region, original, intervals) in enumerate(plans, 1):
            for step in range(intervals + 1):
                label = f'Reading {step + 1}/{intervals + 1}' if len(plans) == 1 else f'Reading {index}:{step + 1}/{intervals + 1}'
                self.report(label)
                fresh = self.capture.capture_frame()
                self.check(initial, fresh, identity)
                with Image.open(fresh.path) as image:
                    bar = find_scrollbar(image, region)
                if (bar is None or abs(bar.top - original.top) > 2 or abs(bar.bottom - original.bottom) > 2
                        or abs(bar.thumb[0] - original.thumb[0]) > 2 or abs(bar.height - original.height) > 2):
                    raise VMError('Question scrollbar changed before capture; no further input.')
                fraction = step / intervals
                x, y = bar.destination(fraction)
                start_x = round((bar.thumb[0] + bar.thumb[2] - 1) / 2)
                start_y = round((bar.thumb[1] + bar.thumb[3] - 1) / 2)
                self.capture.backend.drag(fresh, start_x, start_y, x, y,
                                          self.config.get('capture', {}).get('max_age_seconds', 90), self.stop_event)
                current = self.settled(initial, original.panel, identity)
                with Image.open(current.path) as image:
                    moved = find_scrollbar(image, region)
                if moved is None or (step == 0 and moved.thumb[1] > moved.top + 20) or (
                        step == intervals and moved.thumb[3] < moved.bottom - 20):
                    raise VMError('Could not confirm the scrollbar reached the required endpoint; no answer input.')
                views.append(ScrollView(f'{index}:{step}', index, current, region, moved, fraction,
                                        f'Panel {index}, section {step + 1}/{intervals + 1}'))
                if step < intervals:
                    contexts.append({'path': current.path, 'box': original.panel,
                                     'label': f'Context panel {index}, section {step + 1}/{intervals + 1}'})
        # Use the same neutral pointer position as post-click confirmation, so
        # row hover shading and scrollbar hover do not alter the baseline.
        self.capture.backend.park_pointer(current, self.config.get('capture', {}).get('max_age_seconds', 90), self.stop_event)
        final = self.capture.capture_frame()
        self.check(initial, final, identity)
        content_top = min(bar.panel[1] for _, bar, _ in plans)
        content_bottom = max(bar.panel[3] for _, bar, _ in plans)
        active_panels = {region: bar.panel for region, bar, _ in plans}
        answer_regions = [active_panels.get(box, (box[0], max(box[1], content_top), box[2], min(box[3], content_bottom)))
                          for box in regions]
        return ScrolledQuestion(final, contexts, answer_regions, identity, views=views)
