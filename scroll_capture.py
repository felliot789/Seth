"""Ordered original screenshots of one whole-page scroll area; no stitching."""

from collections import Counter
import time

from PIL import Image, ImageChops

from inference import AIError
from vbox_backend import VMError


def body(path):
    with Image.open(path) as image:
        w, h = image.size
        # Ignore browser chrome, the outer scrollbar, and the guest taskbar.
        return image.convert("L").crop((int(w * .05), int(h * .15), int(w * .90), int(h * .90)))


def unchanged(before, after):
    if before.size != after.size:
        return False
    diff = ImageChops.difference(before, after).point(lambda v: 255 if v > 12 else 0)
    tiles = diff.resize((24, 24), Image.Resampling.BOX)
    # A small clock/caret change must not keep a stationary page moving forever.
    return sum(v > 4 for v in tiles.tobytes()) <= 2


def displacement(before, after, direction):
    """Match distinctive eight-row strips; reject ambiguous or missing overlap."""
    def strips(image):
        image = image.resize((96, image.height), Image.Resampling.BOX).point(lambda v: v // 16)
        result = {}
        # Separate bands let moving text match beside a fixed navigation panel.
        for band in range(3):
            data = image.crop((band * 32, 0, (band + 1) * 32, image.height)).tobytes()
            for y in range(image.height - 8):
                strip = data[y * 32:(y + 8) * 32]
                if max(strip) - min(strip) < 3:
                    continue
                result.setdefault((band, strip), []).append(y)
        return result
    old, new = strips(before), strips(after)
    votes = Counter()
    for strip, positions in old.items():
        found = new.get(strip, [])
        if len(positions) == len(found) == 1:
            delta = (positions[0] - found[0]) * direction
            if 0 < delta <= before.height * .65:
                votes[delta] += 1
    best = votes.most_common(2)
    if not best or best[0][1] < 3 or len(best) > 1 and best[1][1] >= best[0][1] * .7:
        raise VMError("Cannot confirm scroll overlap; no question sent. Use Single mode or reduce guest wheel speed.")
    return best[0][0]


class ScrollCapture:
    def __init__(self, capture, config, stop_event, progress=lambda _: None):
        self.capture, self.stop, self.progress = capture, stop_event, progress
        options = config.get("capture", {})
        self.max_frames = options.get("max_frames", 8)
        if type(self.max_frames) is not int or not 2 <= self.max_frames <= 16:
            raise ValueError("capture.max_frames must be an integer from 2 to 16.")
        self.deadline = time.monotonic() + 45
        self.probes, self.frames = [], []
        self.display = None

    def check(self):
        if self.stop.is_set():
            raise AIError("Scroll capture cancelled.", "cancelled")
        if time.monotonic() >= self.deadline:
            raise VMError("Scroll capture exceeded 45 seconds; no question sent.")

    def sample(self):
        self.check()
        frame = self.capture.capture_frame()
        self.probes.append(frame)
        if self.display is not None and frame.display != self.display:
            raise VMError("VM or display changed during scroll capture; no question sent.")
        self.display = frame.display
        self.check()
        return frame, body(frame.path)

    def settled(self):
        self.check()
        self.stop.wait(.12)
        frame, image = self.sample()
        until = time.monotonic() + 3
        while True:
            self.check()
            self.stop.wait(.12)
            newer, current = self.sample()
            if unchanged(image, current):
                return newer, current
            frame, image = newer, current
            if time.monotonic() >= until:
                raise VMError("Page did not settle after scrolling; no question sent.")

    def step(self, frame, ticks):
        self.check()
        self.capture.backend.scroll(frame, ticks, self.stop)
        self.check()
        return self.settled()

    def run(self):
        def retain(frame):
            if len(self.frames) >= self.max_frames:
                raise VMError("Scroll capture reached its image limit before the bottom; no question sent.")
            self.frames.append(frame)
            self.progress(f"Capturing section {len(self.frames)}")

        try:
            self.progress("Finding page top")
            frame, image = self.settled()
            stationary, ticks = 0, 1
            while stationary < 2:
                next_frame, next_image = self.step(frame, -ticks)
                stationary = stationary + 1 if unchanged(image, next_image) else 0
                if not stationary:
                    delta = displacement(image, next_image, -1)
                    ticks = max(1, min(4, int(image.height * .4 * ticks / delta)))
                frame, image = next_frame, next_image
            retain(frame)
            stationary, distance, ticks = 0, 0, 1
            while stationary < 2:
                next_frame, next_image = self.step(frame, ticks)
                if unchanged(image, next_image):
                    stationary += 1
                else:
                    stationary = 0
                    delta = displacement(image, next_image, 1)
                    ticks = max(1, min(4, int(image.height * .4 * ticks / delta)))
                    if distance and distance + delta > image.height * .6:
                        retain(frame)
                        distance = 0
                    distance += delta
                frame, image = next_frame, next_image
                if distance >= image.height * .4 or stationary and distance:
                    retain(frame)
                    distance = 0
            self.check()
            return tuple(self.frames)
        finally:
            retained = {frame.path for frame in self.frames}
            for frame in self.probes:
                if frame.path not in retained:
                    frame.path.unlink(missing_ok=True)
