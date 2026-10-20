"""Conservative local grounding of approximate vision-model answer points.

Only small, clearly identified radio/checkbox outlines are supported. This does not infer
which answer is correct: an ambiguous or absent nearby control rejects the plan.
"""

from copy import deepcopy
from contextlib import contextmanager
import math
from pathlib import Path
import tempfile

from PIL import Image, ImageDraw, ImageFont

from inference import AIError
from scroll_limits import MAX_SCROLL_VIEWS, MAX_SHEET_HEIGHT, MAX_SHEET_PIXELS


SEARCH_RADIUS = 24
MAX_DIAMETER = 34
CONTRAST = 18
# Native browser focus decoration can extend six pixels beyond a control.
# Keep the surrounding context outside that bounded halo checked as well.
CONTROL_HALO = 6
CONTROL_CONTEXT = CONTROL_HALO + 4


def _background(image, *, exclude=None):
    histogram = image.histogram()
    if exclude is not None:
        histogram = [count - inner for count, inner in zip(histogram, image.crop(exclude).histogram())]
    halfway = sum(histogram) / 2
    total = 0
    for value, count in enumerate(histogram):
        total += count
        if total >= halfway:
            return value


def _components(mask):
    """Eight-connected foreground components in a small local crop."""
    remaining = set(mask)
    while remaining:
        point = remaining.pop()
        component, stack = [point], [point]
        while stack:
            x, y = stack.pop()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    neighbor = (x + dx, y + dy)
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        component.append(neighbor)
                        stack.append(neighbor)
        yield component


def _interior(box, kind):
    left, top, right, bottom = box
    cx, cy = (left + right - 1) / 2, (top + bottom - 1) / 2
    rx, ry = (right - left - 1) / 2, (bottom - top - 1) / 2
    for y in range(top, bottom):
        for x in range(left, right):
            nx, ny = (x - cx) / rx, (y - cy) / ry
            if (nx * nx + ny * ny <= .25 if kind == "radio" else max(abs(nx), abs(ny)) <= .55):
                yield x, y


def _outline(component, box, kind):
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    minimum = 16 if kind == "radio" else 12
    if not (minimum <= min(width, height) <= max(width, height) <= MAX_DIAMETER
            and abs(width - height) <= (1 if kind == "radio" else 2)):
        return False
    cx, cy = (left + right - 1) / 2, (top + bottom - 1) / 2
    rx, ry = (width - 1) / 2, (height - 1) / 2
    normalized = [((x - cx) / rx, (y - cy) / ry) for x, y in component]
    if kind == "radio":
        # Require a circular perimeter, not an oval letter or square. A filled
        # circle is retained so an already-selected control blocks grounding.
        ring = [(x, y) for x, y in normalized if .74 <= math.hypot(x, y) <= 1.12]
        sectors = {int((math.atan2(y, x) + math.pi) * 16 / (2 * math.pi)) % 16 for x, y in ring}
        outside = sum(math.hypot(x, y) > 1.12 for x, y in normalized)
        return outside <= .03 * len(component) and len(sectors) == 16
    # Square outlines must reach all four corners. A radio ring cannot satisfy
    # these corner regions. Filled squares are retained as already-selected
    # blockers rather than allowing a snap to a different nearby checkbox.
    corners = {(x > 0, y > 0) for x, y in normalized if abs(x) >= .8 and abs(y) >= .8}
    edges = []
    for axis, sign in ((0, -1), (0, 1), (1, -1), (1, 1)):
        edge = {round(point[1 - axis] * (ry if axis == 0 else rx) + (cy if axis == 0 else cx))
                for point in normalized if sign * point[axis] >= .8}
        edges.append(len(edge) >= .65 * (height if axis == 0 else width))
    return len(corners) == 4 and all(edges)


def _control_shapes(gray, component, background):
    xs, ys = zip(*component)
    box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
    width, height = box[2] - box[0], box[3] - box[1]
    if (box[0] == 0 or box[1] == 0 or box[2] == gray.width or box[3] == gray.height
            or not 12 <= min(width, height) <= max(width, height) <= MAX_DIAMETER
            or abs(width - height) > 2):
        return
    # Alternating table rows can differ subtly from the frame's dominant
    # background. Refine each candidate against its own local background,
    # keeping the same outline and unchecked-interior requirements.
    context = (max(0, box[0] - 3), max(0, box[1] - 3), min(gray.width, box[2] + 3), min(gray.height, box[3] + 3))
    # Use only surrounding pixels: a large filled control must not become its
    # own "background" and be mistaken for an unchecked outline.
    local_background = _background(gray.crop(context), exclude=(box[0] - context[0], box[1] - context[1],
                                                               box[2] - context[0], box[3] - context[1]))
    candidates = [component] if local_background == background else _components({
        (x, y) for y in range(box[1], box[3]) for x in range(box[0], box[2])
        if abs(gray.getpixel((x, y)) - local_background) >= CONTRAST})
    for candidate in candidates:
        xs, ys = zip(*candidate)
        yield candidate, (min(xs), min(ys), max(xs) + 1, max(ys) + 1), local_background


def _separated_outline(gray, box, background):
    """Unchecked controls need a clear gap, unlike round letters inside words."""
    left, top, right, bottom = box
    nearby = [abs(gray.getpixel((x, y)) - background)
              for y in range(max(0, top - 2), min(gray.height, bottom + 2))
              for x in range(max(0, left - 2), min(gray.width, right + 2))
              if not (left <= x < right and top <= y < bottom)
              and abs(gray.getpixel((x, y)) - background) >= CONTRAST]
    # Native antialiasing can leave a couple of faint disconnected pixels.
    return len(nearby) <= 2 and all(value < CONTRAST * 2 for value in nearby)


def locate_control(image, x, y, kind):
    """Find exactly one nearby, unchecked supported control or fail closed."""
    margin = SEARCH_RADIUS + MAX_DIAMETER // 2 + 3
    left, top = max(0, x - margin), max(0, y - margin)
    crop = image.crop((left, top, min(image.width, x + margin + 1), min(image.height, y + margin + 1))).convert("L")
    background = _background(crop)
    mask = {(px, py) for py in range(crop.height) for px in range(crop.width)
            if abs(crop.getpixel((px, py)) - background) >= CONTRAST}
    candidates = []
    for component in _components(mask):
        for candidate, box, local_background in _control_shapes(crop, component, background):
            cx, cy = (box[0] + box[2] - 1) / 2 + left, (box[1] + box[3] - 1) / 2 + top
            if math.hypot(cx - x, cy - y) > SEARCH_RADIUS or not _outline(candidate, box, kind):
                continue
            interior = list(_interior(box, kind))
            filled = sum(abs(crop.getpixel(point) - local_background) >= CONTRAST for point in interior) / len(interior)
            if filled <= .03 and not _separated_outline(crop, box, local_background):
                continue
            candidates.append((box, cx, cy, filled))
    containing = []
    for candidate in candidates:
        box, cx, cy, _ = candidate
        nx, ny = (x - cx) / ((box[2] - box[0] - 1) / 2), (y - cy) / ((box[3] - box[1] - 1) / 2)
        if (nx * nx + ny * ny <= 1 if kind == "radio" else max(abs(nx), abs(ny)) <= 1):
            containing.append(candidate)
    # A point already inside one control remains clear even in a dense table.
    # Outside controls, never guess between two neighboring candidates.
    candidates = containing or candidates
    if len(candidates) != 1:
        raise AIError("Could not identify one unambiguous nearby answer control; no input sent.", "browser_grounding")
    box, cx, cy, filled = candidates[0]
    if filled > .03:
        raise AIError("The nearby answer control is already selected or has an unsupported interior; no input sent.", "browser_grounding")
    return {"kind": kind, "box": [box[0] + left, box[1] + top, box[2] + left, box[3] + top]}, round(cx), round(cy)


def ground_decision(image_path, decision, *, regions=None):
    """Resolve every target before returning any part of a Qwen click plan."""
    result = deepcopy(decision)
    points = result["selections"] if result["action"] == "select" else [result]
    kind = "checkbox" if result.get("selection_type") == "multiple_answer" else "radio"
    controls = set()
    with Image.open(image_path) as image:
        for point in points:
            x, y = point["click_x"], point["click_y"]
            control, cx, cy = locate_control(image, x, y, kind)
            if regions is not None and not any(
                    left <= control['box'][0] < control['box'][2] <= right
                    and top <= control['box'][1] < control['box'][3] <= bottom
                    for left, top, right, bottom in regions):
                raise AIError('Answer control lies outside the verified question area; no input sent.', 'browser_grounding')
            identity = tuple(control["box"])
            if identity in controls:
                raise AIError("Multiple answer points resolve to the same control; no input sent.", "browser_grounding")
            controls.add(identity)
            point.setdefault("model_point", {"space": "pixels", "x": x, "y": y})
            point.update(click_x=cx, click_y=cy, target_control=control)
    return result


def _small_components(image, background):
    """Scan a whole frame with a compact mutable mask, discarding large blobs."""
    width, height = image.size
    mask = bytearray(image.point([int(abs(value - background) >= CONTRAST) for value in range(256)]).tobytes())
    for origin, active in enumerate(mask):
        if not active:
            continue
        mask[origin] = 0
        stack, component, area = [origin], [], 0
        while stack:
            index = stack.pop()
            area += 1
            if area <= MAX_DIAMETER ** 2:
                component.append(index)
            x, y = index % width, index // width
            for ny in range(max(0, y - 1), min(height, y + 2)):
                row = ny * width
                for nx in range(max(0, x - 1), min(width, x + 2)):
                    neighbor = row + nx
                    if mask[neighbor]:
                        mask[neighbor] = 0
                        stack.append(neighbor)
        if area <= MAX_DIAMETER ** 2:
            yield [(index % width, index // width) for index in component]


def catalog_controls(image):
    """Assign local IDs to supported unchecked controls, in visual reading order."""
    gray = image.convert("L")
    background = _background(gray)
    controls = []
    for component in _small_components(gray, background):
        for candidate, box, local_background in _control_shapes(gray, component, background):
            for kind in ("radio", "checkbox"):
                if not _outline(candidate, box, kind):
                    continue
                interior = list(_interior(box, kind))
                if sum(abs(gray.getpixel(p) - local_background) >= CONTRAST for p in interior) > .03 * len(interior):
                    continue
                if not _separated_outline(gray, box, local_background):
                    continue
                controls.append({"kind": kind, "box": list(box)})
                break
    if len(controls) > 64:
        raise AIError("Too many candidate controls to label unambiguously; no input sent.", "browser_grounding")
    controls.sort(key=lambda c: (c["box"][1] + c["box"][3], c["box"][0] + c["box"][2]))
    return {f"{index:02d}": control for index, control in enumerate(controls, 1)}


def _badge_controls(image, controls):
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype('DejaVuSansMono-Bold.ttf', 18)
    except OSError:
        font = ImageFont.load_default()
    for identifier, control in controls.items():
        left, top, right, bottom = control['box']
        x, y = (left + right - 1) / 2, (top + bottom - 1) / 2
        bx, by, br, bb = draw.textbbox((0, 0), identifier, font=font)
        width, height = br - bx, bb - by
        draw.rectangle((x - width / 2 - 2, y - height / 2 - 2,
                        x + width / 2 + 2, y + height / 2 + 2), fill='#fff200', outline='#7400c9')
        draw.text((x - width / 2 - bx, y - height / 2 - by), identifier, font=font, fill='#111111')


def _context_sheet(answer, contexts):
    if not isinstance(contexts, list) or not 1 <= len(contexts) <= MAX_SCROLL_VIEWS:
        raise AIError(f"Scroll context must contain one to {MAX_SCROLL_VIEWS} reference sections.", "browser_grounding")
    tiles = []
    for context in contexts:
        box = context.get("box")
        if (not isinstance(box, (list, tuple)) or len(box) != 4 or any(type(v) is not int for v in box)
                or not 0 <= box[0] < box[2] <= answer.width or not 0 <= box[1] < box[3] <= answer.height):
            raise AIError("Scroll context has an invalid crop rectangle.", "browser_grounding")
        with Image.open(context["path"]) as source:
            if source.format not in ("PNG", "JPEG") or source.size != answer.size:
                raise AIError("Scroll context must be PNG or JPEG and match the live answer frame dimensions.", "browser_grounding")
            source = source.convert('RGB')
            if 'controls' in context:
                _badge_controls(source, context['controls'])
            suffix = ' (answer IDs)' if 'controls' in context else ' (reference only)'
            tiles.append((source.crop(box), str(context.get("label", "Question context"))[:100] + suffix))
    positions, x, y, row_height = [], 0, 0, 0
    for tile, label in tiles:
        if x and x + tile.width > answer.width:
            x, y, row_height = 0, y + row_height + 12, 0
        positions.append((x, y, tile, label))
        x += tile.width + 12
        row_height = max(row_height, tile.height + 36)
    answer_y = y + row_height + 48
    height = answer_y + answer.height
    if height > MAX_SHEET_HEIGHT or height * answer.width > MAX_SHEET_PIXELS:
        raise AIError("Scroll context image exceeds its bounded size; no inference sent.", "browser_grounding")
    result = Image.new("RGB", (answer.width, height), "#e2e8f0")
    draw = ImageDraw.Draw(result)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    for x, y, tile, label in positions:
        draw.text((x + 6, y + 5), label, font=font, fill="#0f172a")
        result.paste(tile, (x, y + 32))
    heading = ('FINAL VIEW - yellow IDs in all sections are selectable' if any('controls' in c for c in contexts)
               else 'LIVE ANSWER VIEW - choose only yellow badge IDs here')
    draw.text((8, answer_y - 32), heading, font=font, fill="#0f172a")
    result.paste(answer, (0, answer_y))
    return result


def _document_sheet(answer, document):
    """Remove verified overlap without scaling or cutting diagrams into tiles."""
    sections = document['sections']
    if not isinstance(sections, list) or not 2 <= len(sections) <= MAX_SCROLL_VIEWS:
        raise AIError('Invalid complete-question section count.', 'browser_grounding')
    box = sections[0]['box']
    if (not isinstance(box, (tuple, list)) or len(box) != 4 or any(type(v) is not int for v in box)
            or not 0 <= box[0] < box[2] <= answer.width or not 0 <= box[1] < box[3] <= answer.height):
        raise AIError('Invalid complete-question crop.', 'browser_grounding')
    width, viewport_height = box[2] - box[0], box[3] - box[1]
    previous = None
    for section in sections:
        offset = section['offset']
        if (tuple(section['box']) != tuple(box) or type(offset) is not int
                or (previous is None and offset != 0)
                or (previous is not None and not 0 < offset - previous <= viewport_height * .65)):
            raise AIError('Complete-question sections lack verified continuous overlap.', 'browser_grounding')
        previous = offset
    height = previous + viewport_height
    if height + 36 > MAX_SHEET_HEIGHT or (height + 36) * width > MAX_SHEET_PIXELS:
        raise AIError('Complete question image exceeds its bounded size; no inference sent.', 'browser_grounding')
    result = Image.new('RGB', (width, height + 36), '#e2e8f0')
    covered = 0
    for section in sections:
        with Image.open(section['path']) as source:
            if source.format not in ('PNG', 'JPEG') or source.size != answer.size:
                raise AIError('Complete-question frames must match the VM dimensions.', 'browser_grounding')
            # Only append new rows. The complete diagram remains contiguous,
            # including any radio originally clipped at a viewport boundary.
            start = covered - section['offset']
            strip = source.convert('RGB').crop((box[0], box[1] + start, box[2], box[3]))
            result.paste(strip, (0, covered + 36))
            covered = section['offset'] + viewport_height
    controls = {}
    for identifier, control in document['controls'].items():
        left, top, right, bottom = control['box']
        if not 0 <= left < right <= width or not 0 <= top < bottom <= height:
            raise AIError('Answer badge lies outside the complete question.', 'browser_grounding')
        controls[identifier] = control | {'box': [left, top + 36, right, bottom + 36]}
    _badge_controls(result, controls)
    ImageDraw.Draw(result).text((8, 8), 'COMPLETE QUESTION - read top to bottom; choose yellow badge IDs', fill='#0f172a')
    return result


@contextmanager
def annotated_controls(image_path, *, context_images=None, regions=None, scroll_targets=None):
    """Upload-only annotated copy; the original VM frame stays unchanged."""
    with Image.open(image_path) as original:
        controls = catalog_controls(original) if scroll_targets is None else scroll_targets['controls']
        image = original.convert("RGB")
    if regions is not None and scroll_targets is None:
        controls = {f"{index:02d}": control for index, control in enumerate(
            (control for control in controls.values() if any(
                region[0] <= control["box"][0] < control["box"][2] <= region[2]
                and region[1] <= control["box"][1] < control["box"][3] <= region[3]
                for region in regions)), 1)}
    _badge_controls(image, controls if scroll_targets is None else scroll_targets['live_controls'])
    if scroll_targets is not None and scroll_targets.get('document'):
        image = _document_sheet(image, scroll_targets['document'])
    elif context_images:
        image = _context_sheet(image, context_images)
    with tempfile.TemporaryDirectory(prefix="seth-controls-") as directory:
        path = Path(directory) / (Path(image_path).stem + "-controls.png")
        image.save(path)
        path.chmod(0o600)
        yield path, controls


def _surroundings_unchanged(before, after, box):
    def matches(inner, outer):
        points = [(x, y) for y in range(max(0, box[1] - outer), min(before.height, box[3] + outer))
                  for x in range(max(0, box[0] - outer), min(before.width, box[2] + outer))
                  if not (box[0] - inner <= x < box[2] + inner and box[1] - inner <= y < box[3] + inner)]
        return bool(points) and sum(abs(after.getpixel(p) - before.getpixel(p)) >= CONTRAST
                                    for p in points) <= .5 * len(points)

    # Preserve the close-background check for controls without focus decoration.
    # A persistent focus rectangle is allowed only with unchanged context beyond
    # its bounded halo; broad hover shading or page changes still fail.
    return matches(0, 4) or matches(CONTROL_HALO, CONTROL_CONTEXT)


def selection_visible(before, after, control):
    """Require a newly filled interior, not merely hover/focus/border changes."""
    box, kind = control["box"], control["kind"]
    original, fresh = before.convert("L"), after.convert("L")
    background = _background(original.crop(tuple(box)))
    interior = list(_interior(box, kind))
    filled = sum(abs(fresh.getpixel(point) - background) >= CONTRAST for point in interior)
    changed = sum(abs(fresh.getpixel(point) - original.getpixel(point)) >= CONTRAST for point in interior)
    if filled < .2 * len(interior) or changed < .2 * len(interior):
        return False
    if not _surroundings_unchanged(original, fresh, box):
        return False
    # A modal, navigation, or large filled patch is not evidence of selection.
    crop = fresh.crop(tuple(box))
    mask = {(x, y) for y in range(crop.height) for x in range(crop.width)
            if abs(crop.getpixel((x, y)) - background) >= CONTRAST}
    bounds = (0, 0, crop.width, crop.height)
    for component in _components(mask):
        xs, ys = zip(*component)
        if (min(xs), min(ys), max(xs) + 1, max(ys) + 1) == bounds and _outline(component, bounds, kind):
            return True
    return False
