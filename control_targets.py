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


SEARCH_RADIUS = 24
MAX_DIAMETER = 34
CONTRAST = 18


def _background(image):
    histogram = image.histogram()
    halfway = image.width * image.height / 2
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
        xs, ys = zip(*component)
        box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
        if box[0] == 0 or box[1] == 0 or box[2] == crop.width or box[3] == crop.height:
            continue
        cx, cy = (box[0] + box[2] - 1) / 2 + left, (box[1] + box[3] - 1) / 2 + top
        if math.hypot(cx - x, cy - y) > SEARCH_RADIUS or not _outline(component, box, kind):
            continue
        interior = list(_interior(box, kind))
        filled = sum(point in mask for point in interior) / len(interior)
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


def ground_decision(image_path, decision):
    """Resolve every target before returning any part of a Qwen click plan."""
    result = deepcopy(decision)
    points = result["selections"] if result["action"] == "select" else [result]
    kind = "checkbox" if result.get("selection_type") == "multiple_answer" else "radio"
    controls = set()
    with Image.open(image_path) as image:
        for point in points:
            x, y = point["click_x"], point["click_y"]
            control, cx, cy = locate_control(image, x, y, kind)
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
        xs, ys = zip(*component)
        box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
        if (box[0] == 0 or box[1] == 0 or box[2] == image.width or box[3] == image.height
                or max(box[2] - box[0], box[3] - box[1]) > MAX_DIAMETER):
            continue
        for kind in ("radio", "checkbox"):
            if not _outline(component, box, kind):
                continue
            interior = list(_interior(box, kind))
            if sum(abs(gray.getpixel(p) - background) >= CONTRAST for p in interior) > .03 * len(interior):
                continue
            controls.append({"kind": kind, "box": list(box)})
            break
    if len(controls) > 64:
        raise AIError("Too many candidate controls to label unambiguously; no input sent.", "browser_grounding")
    controls.sort(key=lambda c: (c["box"][1] + c["box"][3], c["box"][0] + c["box"][2]))
    return {f"{index:02d}": control for index, control in enumerate(controls, 1)}


def _context_sheet(answer, contexts):
    if not isinstance(contexts, list) or not 1 <= len(contexts) <= 12:
        raise AIError("Scroll context must contain one to twelve reference sections.", "browser_grounding")
    tiles = []
    for context in contexts:
        box = context.get("box")
        if (not isinstance(box, (list, tuple)) or len(box) != 4 or any(type(v) is not int for v in box)
                or not 0 <= box[0] < box[2] <= answer.width or not 0 <= box[1] < box[3] <= answer.height):
            raise AIError("Scroll context has an invalid crop rectangle.", "browser_grounding")
        with Image.open(context["path"]) as source:
            if source.format != "PNG" or source.size != answer.size:
                raise AIError("Scroll context must match the live answer frame dimensions.", "browser_grounding")
            tiles.append((source.crop(box).convert("RGB"), str(context.get("label", "Question context"))[:100]))
    positions, x, y, row_height = [], 0, 0, 0
    for tile, label in tiles:
        if x and x + tile.width > answer.width:
            x, y, row_height = 0, y + row_height + 12, 0
        positions.append((x, y, tile, label))
        x += tile.width + 12
        row_height = max(row_height, tile.height + 36)
    answer_y = y + row_height + 48
    height = answer_y + answer.height
    if height > 8192 or height * answer.width > 16_000_000:
        raise AIError("Scroll context image exceeds its bounded size; no inference sent.", "browser_grounding")
    result = Image.new("RGB", (answer.width, height), "#e2e8f0")
    draw = ImageDraw.Draw(result)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    for x, y, tile, label in positions:
        draw.text((x + 6, y + 5), label + " (reference only)", font=font, fill="#0f172a")
        result.paste(tile, (x, y + 32))
    draw.text((8, answer_y - 32), "LIVE ANSWER VIEW - choose only yellow badge IDs here", font=font, fill="#0f172a")
    result.paste(answer, (0, answer_y))
    return result


@contextmanager
def annotated_controls(image_path, *, context_images=None, regions=None):
    """Upload-only annotated copy; the original VM frame stays unchanged."""
    with Image.open(image_path) as original:
        controls = catalog_controls(original)
        image = original.convert("RGB")
    if regions is not None:
        controls = {f"{index:02d}": control for index, control in enumerate(
            (control for control in controls.values() if any(
                region[0] <= control["box"][0] < control["box"][2] <= region[2]
                and region[1] <= control["box"][1] < control["box"][3] <= region[3]
                for region in regions)), 1)}
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSansMono-Bold.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    for identifier, control in controls.items():
        left, top, right, bottom = control["box"]
        x, y = (left + right - 1) / 2, (top + bottom - 1) / 2
        bx, by, br, bb = draw.textbbox((0, 0), identifier, font=font)
        width, height = br - bx, bb - by
        draw.rectangle((x - width / 2 - 2, y - height / 2 - 2,
                        x + width / 2 + 2, y + height / 2 + 2), fill="#fff200", outline="#7400c9")
        draw.text((x - width / 2 - bx, y - height / 2 - by), identifier, font=font, fill="#111111")
    if context_images:
        image = _context_sheet(image, context_images)
    with tempfile.TemporaryDirectory(prefix="seth-controls-") as directory:
        path = Path(directory) / (Path(image_path).stem + "-controls.png")
        image.save(path)
        path.chmod(0o600)
        yield path, controls


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
    outside = [(x, y) for y in range(max(0, box[1] - 4), min(original.height, box[3] + 4))
               for x in range(max(0, box[0] - 4), min(original.width, box[2] + 4))
               if not (box[0] <= x < box[2] and box[1] <= y < box[3])]
    if not outside or sum(abs(fresh.getpixel(p) - original.getpixel(p)) >= CONTRAST for p in outside) > .5 * len(outside):
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
