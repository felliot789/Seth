"""Screenshot-verified whole-page wheel capture without scrollbar geometry."""

from dataclasses import dataclass

from PIL import Image

from scroll_capture import ScrolledQuestion, ScrollLayoutError, ScrollLimitError, ScrollView, masked
from scroll_navigation import local_ignores, scroll_offset
from scroll_limits import MAX_SCROLL_VIEWS


@dataclass(frozen=True)
class WheelViewport:
    panel: tuple
    content: tuple


class WheelScroll:
    def __init__(self, collector, layout):
        self.collector, self.layout = collector, layout
        self.area = layout.areas[0]
        if len(layout.areas) != 1 or self.area.role != 'both' or not self.area.wheel:
            raise ScrollLayoutError('Wheel capture requires one whole-page question and answer viewport.')
        self.reference = layout.frame
        self.age = collector.config.get('capture', {}).get('max_age_seconds', 90)
        self.limit = collector.options.get('max_views', MAX_SCROLL_VIEWS)
        if type(self.limit) is not int or not 2 <= self.limit <= MAX_SCROLL_VIEWS:
            raise ScrollLayoutError(f'Scroll capture max_views must be between 2 and {MAX_SCROLL_VIEWS}.')

    def _masks(self, image):
        return (self.layout.overlay_tracker.boxes(image, alignment=True) if self.layout.overlay_tracker
                else self.layout.ignore_boxes + self.layout.occlusion_boxes)

    def _same(self, before, after):
        with Image.open(before.path) as a, Image.open(after.path) as b:
            return (masked(a, self.layout.ignore_boxes, self.layout.overlay_tracker).crop(self.area.box).tobytes()
                    == masked(b, self.layout.ignore_boxes, self.layout.overlay_tracker).crop(self.area.box).tobytes())

    def _distance(self, before, after, *, upward=False):
        if upward:
            before, after = after, before
        with Image.open(before.path) as a, Image.open(after.path) as b:
            masks_a, masks_b = self._masks(a), self._masks(b)
            distance = scroll_offset(masked(a, masks_a).crop(self.area.box), masked(b, masks_b).crop(self.area.box),
                                     ignored=local_ignores(self.area.box, masks_a),
                                     after_ignored=local_ignores(self.area.box, masks_b))
        if distance > (self.area.box[3] - self.area.box[1]) * .65:
            raise ScrollLayoutError('Wheel movement left insufficient overlapping question content; no answer input.')
        return distance

    def move(self, frame, ticks):
        self.collector.check(self.reference, frame, ())
        left, top, right, bottom = self.area.box
        positions = [(round(left + (right - left) * fx), round(top + (bottom - top) * fy))
                     for fx, fy in ((.5, .5), (.75, .75), (.25, .75), (.75, .25))]
        with Image.open(frame.path) as image:
            covered = self._masks(image)
        position = next(((x, y) for x, y in positions
                         if not any(a <= x < c and b <= y < d for a, b, c, d in covered)), None)
        if position is None:
            raise ScrollLayoutError('No unobstructed point is available for question wheel scrolling.')
        self.collector.capture.backend.wheel(frame, *position, ticks, self.age, self.collector.stop_event)
        self.collector.capture.backend.park_pointer(frame, self.age, self.collector.stop_event)
        fresh = self.collector.settled(self.reference, self.area.box, ())
        self.layout.verify(self.collector, fresh, moving=self.area)
        return fresh

    def top(self, fresh):
        """Two stable upward probes establish the endpoint; input is bounded."""
        unchanged = 0
        # A captured interval uses at most six ticks. Allow enough unit probes
        # to return across the entire supported document plus endpoint checks.
        for _ in range(self.limit * 6 + 2):
            before = fresh
            fresh = self.move(before, -1)
            if self._same(before, fresh):
                unchanged += 1
                if unchanged == 2:
                    return fresh
            else:
                self._distance(before, fresh, upward=True)
                unchanged = 0
        raise ScrollLayoutError('Could not establish the top of the question within the wheel limit; no answer input.')

    def restore(self, view, fresh, *, previous=None):
        # Forward traversal from an exactly verified recorded position can
        # reuse its tick count. Never subtract clamped bottom ticks upward.
        if previous is not None and previous.wheel_step < view.wheel_step:
            remaining = view.wheel_step - previous.wheel_step
        else:
            fresh = self.top(fresh)
            remaining = view.wheel_step
        while remaining:
            ticks = min(6, remaining)
            fresh = self.move(fresh, ticks)
            remaining -= ticks
        return fresh

    def collect(self):
        self.collector.identity_boxes, self.collector.ignore_boxes = self.layout.identity_boxes, self.layout.ignore_boxes
        self.collector.overlay_tracker = self.layout.overlay_tracker
        self.collector.report('Finding page top with wheel scrolling')
        current = self.collector.capture.capture_frame()
        self.layout.verify(self.collector, current)
        current = self.top(current)
        window = WheelViewport(self.area.box, self.area.box)
        views, offset, ticks, burst, unchanged = [], 0, 0, 1, 0

        def remember():
            index = len(views)
            views.append(ScrollView(f'1:{index}', 1, current, self.area.box, window, 0,
                                    f'Whole page, section {index + 1}', wheel_step=ticks, offset=offset))

        remember()
        while True:
            self.collector.report(f'Reading page section {len(views)} with wheel scrolling')
            before = current
            current = self.move(before, burst)
            if self._same(before, current):
                unchanged += 1
                if unchanged == 2:
                    if len(views) == 1:
                        raise ScrollLayoutError('The declared long page did not move under wheel input; no answer inference.')
                    break
                continue
            unchanged = 0
            distance = self._distance(before, current)
            offset += distance
            ticks += burst
            if len(views) >= self.limit:
                raise ScrollLimitError(len(views) + 1, self.limit)
            remember()
            if len(views) == 2:
                burst = max(1, min(6, int((self.area.box[3] - self.area.box[1]) * .55 / distance)))
        self.collector.check(self.reference, current, ())
        contexts = [{'path': view.frame.path, 'box': self.area.box, 'label': view.label} for view in views[:-1]]
        return ScrolledQuestion(current, contexts, [self.area.box], (), views=views,
                                identity_boxes=self.layout.identity_boxes, ignore_boxes=self.layout.ignore_boxes,
                                layout=self.layout.summary, occlusion_boxes=self.layout.occlusion_boxes,
                                overlay_tracker=self.layout.overlay_tracker, wheel_navigation=self)
