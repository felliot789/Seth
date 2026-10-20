"""Bounded screenshot-only context capture for native Chromium quiz scrollbars."""

from dataclasses import dataclass, field, replace
import math
import time

from PIL import Image, ImageDraw

from control_targets import _components
from scroll_limits import MAX_SCROLL_VIEWS
from vbox_backend import VMError, validate_age


class ScrollLimitError(VMError):
    def __init__(self, required_views, max_views):
        super().__init__(f'Question requires {required_views} scroll views, exceeding the configured limit of '
                         f'{max_views}; no answer inference or selections attempted.')
        self.required_views, self.max_views = required_views, max_views


class ScrollNavigationError(VMError):
    pass


class ScrollLayoutError(VMError):
    pass


def masked(image, boxes=(), tracker=None, *, alignment=False):
    if tracker is not None:
        boxes = tracker.boxes(image, alignment=alignment)
    image = image.convert('RGB')
    draw = ImageDraw.Draw(image)
    for left, top, right, bottom in boxes:
        draw.rectangle((left, top, right - 1, bottom - 1), fill='white')
    return image


@dataclass(frozen=True)
class Scrollbar:
    thumb: tuple
    top: int
    bottom: int
    panel: tuple
    content: tuple = ()
    viewport: tuple = ()

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


def find_scrollbars(image, region=None, *, right_edge=False):
    """Require one solid gray thumb and matching native up/down arrow buttons."""
    region = region or (0, 0, image.width, image.height)
    left, top, right, bottom = region
    if right_edge:
        left = max(left, right - round((right - left) * .22))
    crop = image.crop((left, top, right, bottom)).convert('RGB')
    gray = crop.convert('L')
    mask = {(x, y) for y in range(gray.height) for x in range(gray.width)
            if 70 <= gray.getpixel((x, y)) <= 215}
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
        # A contrasting native rail gives an exact viewport edge, preventing
        # a rounded model box from including browser chrome or the taskbar.
        column = min(crop.width - 1, thumb[2] + 3)
        color = crop.getpixel((column, track_top - top + 2))
        def rail(y):
            return max(abs(a - b) for a, b in zip(crop.getpixel((column, y)), color)) <= 3
        rail_top, rail_bottom = track_top - top, track_bottom - top
        while rail_top > 0 and rail(rail_top - 1):
            rail_top -= 1
        while rail_bottom < crop.height and rail(rail_bottom):
            rail_bottom += 1
        viewport = ()
        if abs(rail_top - ups[0][1]) <= 20 and abs(rail_bottom - downs[0][3]) <= 20:
            viewport = rail_top + top, rail_bottom + top
        found.append(Scrollbar(actual_thumb, track_top, track_bottom, panel, viewport=viewport))
    return sorted(found, key=lambda bar: (bar.thumb[0], bar.top))


def find_scrollbar(image, region):
    found = find_scrollbars(image, region, right_edge=True)
    if len(found) > 1:
        raise VMError('More than one native scrollbar matched a question column; no scrolling attempted.')
    return found[0] if found else None


@dataclass
class ScrollView:
    key: str
    panel: int
    frame: object
    region: tuple
    bar: object
    fraction: float
    label: str
    wheel_step: int | None = None
    offset: int | None = None


@dataclass
class ScrolledQuestion:
    frame: object
    contexts: list
    regions: list
    identity_box: tuple
    selected_controls: list = field(default_factory=list)
    views: list = field(default_factory=list)
    identity_boxes: list = field(default_factory=list)
    ignore_boxes: list = field(default_factory=list)
    layout: list = field(default_factory=list)
    occlusion_boxes: list = field(default_factory=list)
    overlay_tracker: object = None
    wheel_navigation: object = None

    @property
    def inference_captured_at(self):
        """The oldest required inference section owns the execution deadline."""
        return min(frame.captured_at for frame in [self.frame] + [view.frame for view in self.views])

    def validate_age(self, config):
        maximum = config.get('capture', {}).get('max_age_seconds', 90)
        for frame in [self.frame] + [view.frame for view in self.views]:
            validate_age(frame.captured_at, maximum)

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
            expected, actual = masked(expected, self.ignore_boxes, self.overlay_tracker), masked(actual, self.ignore_boxes, self.overlay_tracker)
            for box in self.identity_boxes or ([self.identity_box] if self.identity_box else []):
                if expected.crop(box).tobytes() != actual.crop(box).tobytes():
                    raise VMError('Question header changed after context capture; start a fresh cycle.')
            # Prior verified selections may legitimately change color/focus.
            # Everything else in the captured question columns must still match.
            for control in self.selected_controls:
                left, top, right, bottom = control['target_control']['box']
                box = (max(0, left - 24), max(0, top - 24),
                       min(expected.width, right + 24), min(expected.height, bottom + 24))
                actual.paste(expected.crop(box), box)
            for left, top, right, bottom in self.regions:
                box = (left, top, max(left + 1, right - (0 if self.layout else 24)), bottom)
                if expected.crop(box).tobytes() != actual.crop(box).tobytes():
                    raise VMError('Question or answer view changed after inference; start a fresh cycle.')


class ScrollCollector:
    def __init__(self, config, capture, stop_event=None, report=lambda message: None, *, identity_boxes=None, ignore_boxes=(), overlay_tracker=None):
        self.config, self.capture = config, capture
        self.options = config.get('capture', {}).get('scroll_context', {})
        self.stop_event, self.report = stop_event, report
        self.identity_boxes, self.ignore_boxes = identity_boxes, ignore_boxes
        self.overlay_tracker = overlay_tracker
        self.inference_question = None

    def check(self, reference, fresh, identity_box):
        if self.stop_event is not None and self.stop_event.is_set():
            raise VMError('Scroll capture cancelled; no further input.')
        if self.inference_question is not None:
            self.inference_question.validate_age(self.config)
        if fresh.display != reference.display:
            raise VMError('VM display changed during scroll capture; no further input.')
        with Image.open(reference.path) as before, Image.open(fresh.path) as after:
            before, after = masked(before, self.ignore_boxes, self.overlay_tracker), masked(after, self.ignore_boxes, self.overlay_tracker)
            for box in self.identity_boxes if self.identity_boxes is not None else ([identity_box] if identity_box else []):
                if before.crop(box).tobytes() != after.crop(box).tobytes():
                    raise VMError('Question header changed during scroll capture; no further input.')

    def settled(self, reference, region, identity_box):
        deadline, previous = time.monotonic() + 3, None
        while True:
            fresh = self.capture.capture_frame()
            self.check(reference, fresh, identity_box)
            with Image.open(fresh.path) as image:
                # Ignore the scrollbar itself, whose hover state may change.
                content = masked(image, self.ignore_boxes, self.overlay_tracker).crop(region).tobytes()
            if content == previous:
                return fresh
            if time.monotonic() >= deadline:
                raise VMError('The scrolled question did not settle; no answer input.')
            previous = content
            if self.stop_event is not None:
                self.stop_event.wait(.15)
            else:
                time.sleep(.15)

    def collect(self, initial, *, layout_client=None):
        if not self.options.get('enabled', False):
            return ScrolledQuestion(initial, [], [], ())
        mode = self.options.get('layout', 'manual' if 'regions' in self.options else 'auto')
        if mode == 'auto':
            from scroll_layout import discover_layout
            return self.collect_layout(discover_layout(self, initial, layout_client))
        if mode != 'manual':
            raise ScrollLayoutError('Scroll layout must be auto or manual.')
        size = initial.display.width, initial.display.height
        regions = [pixel_box(box, size) for box in self.options.get('regions', [])]
        if not 1 <= len(regions) <= 4:
            raise VMError('Scroll capture needs one to four configured question columns.')
        identity = pixel_box(self.options.get('identity_box', [.05, .15, .35, .28]), size)
        limit = self.options.get('max_views', MAX_SCROLL_VIEWS)
        if type(limit) is not int or not 2 <= limit <= MAX_SCROLL_VIEWS:
            raise VMError(f'Scroll capture max_views must be between 2 and {MAX_SCROLL_VIEWS}.')
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
                current = self.settled(initial, (original.panel[0], original.panel[1], original.panel[2] - 24, original.panel[3]), identity)
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

    def collect_layout(self, layout):
        """Capture discovered areas with the same bounded, verified return path."""
        if any(area.wheel for area in layout.areas):
            from wheel_scroll import WheelScroll
            return WheelScroll(self, layout).collect()
        initial = layout.frame
        self.identity_boxes, self.ignore_boxes = layout.identity_boxes, layout.ignore_boxes
        self.overlay_tracker = layout.overlay_tracker
        plans = [(area, max(1, math.ceil((area.bar.bottom - area.bar.top - area.bar.height)
                                       / (.65 * area.bar.height))))
                 for area in layout.areas if area.bar is not None]
        limit = self.options.get('max_views', MAX_SCROLL_VIEWS)
        if type(limit) is not int or not 2 <= limit <= MAX_SCROLL_VIEWS:
            raise ScrollLayoutError(f'Scroll capture max_views must be between 2 and {MAX_SCROLL_VIEWS}.')
        required = sum(intervals + 1 for _, intervals in plans)
        if required > limit:
            raise ScrollLimitError(required, limit)
        # Verify ownership and restore the original position before collecting.
        for area, _ in plans:
            layout.probe(self, area)
        views, contexts = [], []
        current = self.capture.capture_frame()
        layout.verify(self, current)
        for index, (area, intervals) in enumerate(plans, 1):
            for step in range(intervals + 1):
                self.report(f'Reading {index}:{step + 1}/{intervals + 1}')
                fresh = self.capture.capture_frame()
                self.check(initial, fresh, ())
                with Image.open(fresh.path) as image:
                    bar = find_scrollbar(image, area.search)
                if not layout.same_bar(area.bar, bar):
                    raise ScrollLayoutError('Discovered scrollbar changed before capture; start a fresh cycle.')
                fraction = step / intervals
                self.capture.backend.drag(fresh, round((bar.thumb[0] + bar.thumb[2] - 1) / 2),
                                          round((bar.thumb[1] + bar.thumb[3] - 1) / 2),
                                          *bar.destination(fraction),
                                          self.config.get('capture', {}).get('max_age_seconds', 90), self.stop_event)
                current = self.settled(initial, area.box, ())
                with Image.open(current.path) as image:
                    moved = find_scrollbar(image, area.search)
                if (not layout.same_bar(area.bar, moved)
                        or (step == 0 and moved.thumb[1] > moved.top + 20)
                        or (step == intervals and moved.thumb[3] < moved.bottom - 20)):
                    raise ScrollLayoutError('Could not confirm the discovered scrollbar endpoint; no answer input.')
                moved = replace(moved, panel=area.box, content=area.box)
                views.append(ScrollView(f'{index}:{step}', index, current, area.search, moved, fraction,
                                        f'{area.role.title()} panel {index}, section {step + 1}/{intervals + 1}'))
        if plans:
            self.capture.backend.park_pointer(current, self.config.get('capture', {}).get('max_age_seconds', 90), self.stop_event)
        final = self.capture.capture_frame()
        self.check(initial, final, ())
        # Also retain fixed question/answer regions alongside the scrolling ones.
        for area in layout.areas:
            if area.bar is None:
                with Image.open(initial.path) as before, Image.open(final.path) as after:
                    if masked(before, self.ignore_boxes, self.overlay_tracker).crop(area.box).tobytes() != masked(after, self.ignore_boxes, self.overlay_tracker).crop(area.box).tobytes():
                        raise ScrollLayoutError('Fixed question or answer area changed during capture; no answer input.')
        active = {view.panel: view.key for view in views}
        contexts = [{'path': view.frame.path, 'box': view.bar.panel, 'label': view.label}
                    for view in views if view.key != active[view.panel]]
        return ScrolledQuestion(final, contexts, [area.box for area in layout.areas], (), views=views,
                                identity_boxes=layout.identity_boxes, ignore_boxes=layout.ignore_boxes,
                                layout=layout.summary, occlusion_boxes=layout.occlusion_boxes, overlay_tracker=layout.overlay_tracker)
