"""Local control IDs and verified return trips for captured question panels."""

from collections import defaultdict

from PIL import Image, ImageChops

from control_targets import catalog_controls, locate_control, selection_visible
from scroll_capture import ScrollCollector, ScrollNavigationError, find_scrollbar


def content_box(view):
    left, top, right, bottom = view.bar.panel
    return left + 4, top + 12, right - 28, bottom - 12


def ink_rows(image):
    """Ignore light stationary watermarks while retaining text and graph lines."""
    red, green, blue = image.convert('RGB').split()
    darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
    binary = darkest.point([255 if value < 190 else 0 for value in range(256)])
    binary = binary.convert('1', dither=Image.Dither.NONE)
    data, stride = binary.tobytes(), (binary.width + 7) // 8
    return [int.from_bytes(data[y * stride:(y + 1) * stride], 'big') for y in range(binary.height)]


def agreement(before, after):
    intersection = sum((a & b).bit_count() for a, b in zip(before, after))
    union = sum((a | b).bit_count() for a, b in zip(before, after))
    return intersection / union if union >= 80 else 0


def scroll_offset(before, after, expected=None):
    """Require a unique foreground alignment; repeated/blank rows are ambiguous."""
    a, b = ink_rows(before), ink_rows(after)
    lower, upper = 1, max(2, round(len(a) * .85))
    if expected is not None:
        lower, upper = max(lower, round(expected * .55) - 4), min(upper, round(expected * 1.6) + 5)
    scores = [(agreement(a[delta:], b[:-delta]), delta) for delta in range(lower, upper)]
    if not scores:
        raise ScrollNavigationError('Overlapping answer views have inconsistent scroll positions; no answer input.')
    best, delta = max(scores)
    competing = max((score for score, other in scores if abs(other - delta) > 1), default=0)
    if best < .97 or competing > best - .025:
        raise ScrollNavigationError('Cannot uniquely align overlapping answer views; no answer input.')
    return delta


def same_bar(before, after, *, position=False):
    return (after is not None and abs(before.top - after.top) <= 2
            and abs(before.bottom - after.bottom) <= 2 and abs(before.height - after.height) <= 2
            and abs(before.thumb[0] - after.thumb[0]) <= 2
            and (not position or abs(before.thumb[1] - after.thumb[1]) <= 1))


class ScrollNavigator:
    def __init__(self, question, config, capture, stop_event=None, report=lambda message: None):
        self.question, self.config, self.capture = question, config, capture
        self.stop_event, self.report = stop_event, report
        self.collector = ScrollCollector(config, capture, stop_event, report)
        self.views = {view.key: view for view in question.views}
        self.active = {view.panel: view.key for view in question.views}
        self.shifts = {panel: 0 for panel in self.active}
        self.current = question.frame
        self.selected = set()
        self.returned_views = []
        self.records, self.offsets, self.segments = {}, {}, {}
        self.view_controls = defaultdict(dict)
        self._index()

    def _controls(self, frame, box):
        with Image.open(frame.path) as image:
            controls = catalog_controls(image.crop(box))
        left, top, right, bottom = box
        result = []
        for control in controls.values():
            x1, y1, x2, y2 = control['box']
            actual = [x1 + left, y1 + top, x2 + left, y2 + top]
            if left + 2 <= actual[0] and top + 2 <= actual[1] and actual[2] <= right - 2 and actual[3] <= bottom - 2:
                result.append({'kind': control['kind'], 'box': actual})
        return result

    def _index(self):
        nodes, previous, segment = [], {}, defaultdict(int)
        for view in self.views.values():
            if self.stop_event is not None and self.stop_event.is_set():
                raise ScrollNavigationError('Scroll indexing cancelled; no answer input.')
            controls = self._controls(view.frame, content_box(view))
            prior = previous.get(view.panel)
            offset = 0
            if prior is not None and view.bar.thumb[1] <= prior[0].bar.thumb[1]:
                raise ScrollNavigationError('The scrollbar did not advance between captures; no answer input.')
            if controls and prior is not None and prior[1]:
                earlier = prior[0]
                expected = ((view.bar.thumb[1] - earlier.bar.thumb[1])
                            * (content_box(view)[3] - content_box(view)[1]) / view.bar.height)
                with Image.open(earlier.frame.path) as before, Image.open(view.frame.path) as after:
                    offset = self.offsets[earlier.key] + scroll_offset(
                        before.crop(content_box(earlier)), after.crop(content_box(view)), expected)
            elif controls:
                # An empty intervening viewport separates distinct control groups.
                segment[view.panel] += 1
            self.offsets[view.key], self.segments[view.key] = offset, segment[view.panel]
            for control in controls:
                left, top, right, bottom = control['box']
                global_box = [left, top + offset, right, bottom + offset]
                matching = [node for node in nodes if node['panel'] == view.panel
                            and node['segment'] == segment[view.panel]
                            and max(abs(a - b) for a, b in zip(node['global_box'], global_box)) <= 1]
                if len(matching) > 1 or (matching and matching[0]['kind'] != control['kind']):
                    raise ScrollNavigationError('Answer controls overlap ambiguously; no answer input.')
                if matching:
                    node = matching[0]
                else:
                    node = {'panel': view.panel, 'segment': segment[view.panel], 'global_box': global_box,
                            'kind': control['kind'], 'occurrences': []}
                    nodes.append(node)
                node['occurrences'].append((view.key, control['box']))
            previous[view.panel] = view, controls
        active_boxes = {view.bar.panel for view in self.views.values()}
        for box in self.question.regions:
            if box in active_boxes:
                continue
            for control in self._controls(self.question.frame, box):
                nodes.append({'panel': 0, 'segment': 0, 'global_box': control['box'], 'kind': control['kind'],
                              'occurrences': [('live', control['box'])]})
        if len(nodes) > 64:
            raise ScrollNavigationError('Too many distinct answer controls across scroll views; no answer input.')
        nodes.sort(key=lambda node: (node['panel'], node['segment'], node['global_box'][1], node['global_box'][0]))
        self.controls = {}
        for index, node in enumerate(nodes, 1):
            identifier = f'{index:02d}'
            def clearance(occurrence):
                key, box = occurrence
                if key == 'live':
                    return 10000
                region = content_box(self.views[key])
                return min(box[1] - region[1], region[3] - box[3])
            key, box = max(node['occurrences'], key=clearance)
            self.records[identifier] = node
            self.controls[identifier] = {'kind': node['kind'], 'box': list(box), 'target_view': key}
            for occurrence, local_box in node['occurrences']:
                self.view_controls[occurrence][identifier] = {'kind': node['kind'], 'box': list(local_box)}
        self.live_controls = dict(self.view_controls['live'])
        for key in self.active.values():
            self.live_controls.update(self.view_controls[key])
        self.contexts = [{'path': view.frame.path, 'box': view.bar.panel, 'label': view.label,
                          'controls': self.view_controls[view.key]}
                         for view in self.views.values() if view.key != self.active[view.panel]]
        self.payload = {'controls': self.controls, 'live_controls': self.live_controls}

    def plan(self, decision):
        points = decision['selections'] if decision['action'] == 'select' else [decision]
        seen, rows = set(), []
        for point in points:
            identifier = point.get('target_id')
            control = self.controls.get(identifier)
            if (control is None or identifier in seen or identifier in self.selected
                    or point.get('target_view') != control['target_view']
                    or point.get('target_control') != {'kind': control['kind'], 'box': control['box']}
                    or (point.get('click_x'), point.get('click_y')) != (
                        round((control['box'][0] + control['box'][2] - 1) / 2),
                        round((control['box'][1] + control['box'][3] - 1) / 2))):
                raise ScrollNavigationError('Answer plan does not match the captured control IDs; no answer input.')
            seen.add(identifier)
            node = self.records[identifier]
            if decision.get('selection_type') == 'classification':
                row = (node['panel'], node['segment'], (node['global_box'][1] + node['global_box'][3]) / 2)
                if any(row[:2] == other[:2] and abs(row[2] - other[2]) <= 3 for other in rows):
                    raise ScrollNavigationError('The plan selects two categories in the same visible table row; no answer input.')
                rows.append(row)
        order = {key: index for index, key in enumerate(self.views)} | {'live': -1}
        return sorted(points, key=lambda point: (order[point['target_view']], point['click_y'], point['click_x']))

    def _box_in_view(self, identifier, key, shift=0):
        node = self.records[identifier]
        if key == 'live':
            return list(node['global_box']) if node['panel'] == 0 else None
        view = self.views[key]
        if node['panel'] != view.panel or node['segment'] != self.segments[key]:
            return None
        occurrence = next((box for candidate, box in node['occurrences'] if candidate == key), None)
        if occurrence is not None:
            left, top, right, bottom = occurrence
            return [left, top + shift, right, bottom + shift]
        left, top, right, bottom = node['global_box']
        delta = shift - self.offsets[key]
        return [left, top + delta, right, bottom + delta]

    def _selected_boxes(self, shifts=None, identifiers=None):
        result = {}
        shifts = self.shifts if shifts is None else shifts
        for identifier in self.selected if identifiers is None else identifiers:
            panel = self.records[identifier]['panel']
            key = self.active.get(panel, 'live')
            box = self._box_in_view(identifier, key, shifts.get(panel, 0))
            if box is not None:
                region = content_box(self.views[key]) if key != 'live' else (0, 0, self.current.display.width, self.current.display.height)
                if box[3] > region[1] and box[1] < region[3]:
                    result[identifier] = box
        return result

    @staticmethod
    def _erase(image, boxes):
        from PIL import ImageDraw
        draw = ImageDraw.Draw(image)
        for left, top, right, bottom in boxes:
            draw.rectangle((left - 4, top - 4, right + 3, bottom + 3), fill='white')

    def _alignment(self, before, after, area, ignored, tolerance, minimum=.998):
        """Match the complete foreground with only known control changes masked."""
        left, top, right, bottom = area
        area = left, top + tolerance, right, bottom - tolerance
        original = before.convert('RGB')
        self._erase(original, ignored)
        a = ink_rows(original.crop(area))
        scores = []
        for shift in range(-tolerance, tolerance + 1):
            candidate = after.convert('RGB')
            self._erase(candidate, [[x1, y1 + shift, x2, y2 + shift] for x1, y1, x2, y2 in ignored])
            b = ink_rows(candidate.crop((area[0], area[1] + shift, area[2], area[3] + shift)))
            scores.append((agreement(a, b), shift))
        best, shift = max(scores)
        competing = max((score for score, other in scores if abs(other - shift) > 1), default=0)
        if best < minimum or competing > best - .025:
            raise ScrollNavigationError('Question content does not align uniquely; no further input.')
        return shift

    def _verify_unchanged(self, fresh, exclude=None, pending=()):
        self.collector.check(self.question.frame, fresh, self.question.identity_box)
        selected = self._selected_boxes(identifiers=self.selected | set(pending))
        proposed = self.shifts.copy()
        panels = {self.views[key].bar.panel: panel for panel, key in self.active.items()}
        with Image.open(self.current.path) as before, Image.open(fresh.path) as after:
            old, new = before.convert('RGB'), after.convert('RGB')
            self._erase(old, selected.values())
            self._erase(new, selected.values())
            for region in self.question.regions:
                if region == exclude:
                    continue
                panel = panels.get(region)
                if panel is not None:
                    view = self.views[self.active[panel]]
                    original = find_scrollbar(before, view.region)
                    actual = find_scrollbar(after, view.region)
                    if original is None or not same_bar(original, actual, position=True):
                        raise ScrollNavigationError('The question scrollbar moved or changed; no further input.')
                left, top, right, bottom = region
                box = left, top, right - 24, bottom
                if old.crop(box).tobytes() == new.crop(box).tobytes():
                    continue
                if panel is None:
                    raise ScrollNavigationError('Question or answer view changed; start a fresh cycle.')
                # Native panels can nudge by a pixel when a radio/checkbox gains
                # focus. Track each panel independently, without accepting a
                # larger scroll or changed words/graphs as harmless movement.
                shift = self._alignment(before, after, content_box(view), list(selected.values()), 3, minimum=1.0)
                if shift == 0:
                    raise ScrollNavigationError('Question or answer appearance changed; start a fresh cycle.')
                proposed[panel] += shift
        return proposed

    def _verify_selected(self, fresh, shifts=None):
        with Image.open(fresh.path) as actual:
            for identifier, box in self._selected_boxes(shifts).items():
                node = self.records[identifier]
                panel = node['panel']
                region = content_box(self.views[self.active[panel]]) if panel else (0, 0, actual.width, actual.height)
                if not region[1] + 6 <= box[1] < box[3] <= region[3] - 6:
                    continue
                key = self.active.get(panel, 'live')
                source_key, source_box = next((pair for pair in node['occurrences'] if pair[0] == key), node['occurrences'][0])
                frame = self.views[source_key].frame if source_key != 'live' else self.question.frame
                with Image.open(frame.path) as original:
                    before = original.crop((source_box[0] - 6, source_box[1] - 6, source_box[2] + 6, source_box[3] + 6))
                after = actual.crop((box[0] - 6, box[1] - 6, box[2] + 6, box[3] + 6))
                control = {'kind': node['kind'], 'box': [6, 6, before.width - 6, before.height - 6]}
                if not selection_visible(before, after, control):
                    raise ScrollNavigationError('A previously confirmed answer changed; no further input.')

    def verify_current(self):
        fresh = self.capture.capture_frame()
        proposed = self._verify_unchanged(fresh)
        self._verify_selected(fresh, proposed)
        self.shifts, self.current = proposed, fresh
        return fresh

    def _return_shift(self, view, fresh):
        left, top, right, bottom = content_box(view)
        tolerance = min(32, (bottom - top) // 8)
        selected = [box for identifier in self.selected
                    if (box := self._box_in_view(identifier, view.key)) is not None]
        with Image.open(view.frame.path) as original, Image.open(fresh.path) as actual:
            try:
                return self._alignment(original, actual, (left, top, right, bottom), selected, tolerance)
            except ScrollNavigationError:
                raise ScrollNavigationError('Returned answer view does not match its capture; no answer input.') from None

    def prepare(self, point):
        identifier = point['target_id']
        control = self.controls[identifier]
        key = control['target_view']
        fresh = self.verify_current()
        shift = 0
        if key != 'live':
            view = self.views[key]
            if self.active[view.panel] != key:
                self.report(f'Returning to section {key}')
                with Image.open(fresh.path) as image:
                    bar = find_scrollbar(image, view.region)
                if not same_bar(view.bar, bar):
                    raise ScrollNavigationError('Question scrollbar geometry changed; no further input.')
                x, y = bar.destination(view.fraction)
                self.capture.backend.drag(fresh, round((bar.thumb[0] + bar.thumb[2] - 1) / 2),
                                          round((bar.thumb[1] + bar.thumb[3] - 1) / 2), x, y,
                                          self.config.get('capture', {}).get('max_age_seconds', 90), self.stop_event)
                fresh = self.collector.settled(self.question.frame, view.bar.panel, self.question.identity_box)
                self.capture.backend.park_pointer(fresh, self.config.get('capture', {}).get('max_age_seconds', 90), self.stop_event)
                fresh = self.capture.capture_frame()
                proposed = self._verify_unchanged(fresh, exclude=view.bar.panel)
                with Image.open(fresh.path) as image:
                    restored = find_scrollbar(image, view.region)
                if not same_bar(view.bar, restored) or abs(view.bar.thumb[1] - restored.thumb[1]) > 3:
                    raise ScrollNavigationError('Scrollbar did not return to the captured position; no answer input.')
                shift = self._return_shift(view, fresh)
                self.active[view.panel], proposed[view.panel] = key, shift
                self.shifts = proposed
                self.current = fresh
                self._verify_selected(fresh)
                self.returned_views.append(key)
            shift = self.shifts[view.panel]
        left, top, right, bottom = control['box']
        expected = [left, top + shift, right, bottom + shift]
        with Image.open(fresh.path) as image:
            actual, x, y = locate_control(image, round((left + right - 1) / 2),
                                          round((top + bottom - 1) / 2) + shift, control['kind'])
        if actual['box'] != expected:
            raise ScrollNavigationError('Returned control does not match its captured location; no answer input.')
        self.current = fresh
        return fresh, point | {'click_x': x, 'click_y': y, 'target_control': actual}

    def verify_selection(self, frame, point):
        """Confirm a click after measuring tiny independent panel movements."""
        identifier = point['target_id']
        panel = self.records[identifier]['panel']
        original_box = point['target_control']['box']

        def verifier(fresh):
            proposed = self._verify_unchanged(fresh, pending=(identifier,))
            self._verify_selected(fresh, proposed)
            delta = proposed.get(panel, 0) - self.shifts.get(panel, 0)
            left, top, right, bottom = original_box
            with Image.open(frame.path) as before, Image.open(fresh.path) as after:
                before = before.crop((left - 6, top - 6, right + 6, bottom + 6))
                after = after.crop((left - 6, top - 6 + delta, right + 6, bottom + 6 + delta))
            control = {'kind': point['target_control']['kind'], 'box': [6, 6, before.width - 6, before.height - 6]}
            if not selection_visible(before, after, control):
                return False
            self.shifts, self.current = proposed, fresh
            return True

        self.capture.verify_selection(frame, point, self.stop_event, verifier=verifier)

    def confirm(self, point):
        identifier = point['target_id']
        if identifier in self.selected:
            raise ScrollNavigationError('Answer control was already selected in this cycle; no further input.')
        self.selected.add(identifier)
