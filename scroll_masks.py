"""Locate bounded timer widgets that can move before becoming sticky."""

from PIL import Image


class OverlayTracker:
    def __init__(self, frame, ignored, overlays):
        from scroll_navigation import ink_rows, local_ignores
        self.ignored, self.overlays, self.templates, self.cache = ignored, overlays, [], {}
        with Image.open(frame.path) as source:
            for box in overlays:
                inset = box[0] + 4, box[1] + 4, box[2] - 4, box[3] - 4
                if inset[0] >= inset[2] or inset[1] >= inset[3]:
                    from scroll_capture import ScrollLayoutError
                    raise ScrollLayoutError('Fixed overlay is too small to track visually.')
                self.templates.append((inset, ink_rows(source.crop(inset)), local_ignores(inset, ignored)))

    def boxes(self, image, *, alignment=False):
        from scroll_capture import ScrollLayoutError
        from scroll_layout import overlaps
        from scroll_navigation import ink_rows, translation_score
        filename = getattr(image, 'filename', None)
        key = (filename, alignment)
        if filename and key in self.cache:
            return self.cache[key]
        result = [box for box in self.ignored if not any(overlaps(box, overlay) for overlay in self.overlays)]
        for box, (inset, template, ignored) in zip(self.overlays, self.templates):
            rows = ink_rows(image.crop((inset[0], 0, inset[2], image.height)))
            height, width = len(template), inset[2] - inset[0]
            scores = [(translation_score(template, rows[y:y + height], 0, width, ignored), y)
                      for y in range(image.height - height + 1)]
            best, y = max(scores)
            competing = max((score for score, other in scores if abs(other - y) > 2), default=0)
            # Sticky positioning can rerasterize a label at a fractional pixel.
            # This match locates only the bounded widget; answer foreground
            # verification retains its separate, stricter thresholds.
            if best < .75 or competing > best - .10:
                raise ScrollLayoutError('Could not uniquely locate the timer/overlay after scrolling; no answer input.')
            delta = y - inset[1]
            result.extend((x1, y1 + delta, x2, y2 + delta)
                          for x1, y1, x2, y2 in self.ignored if overlaps((x1, y1, x2, y2), box))
            if alignment:
                result.append((box[0], box[1] + delta, box[2], box[3] + delta))
        if filename:
            self.cache[key] = result
        return result
