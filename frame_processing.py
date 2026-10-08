"""Pure frame processing helpers; no desktop, network or config access."""
from functools import lru_cache

import numpy as np


class FrameCapture:
    """Reuse a monitor capture within one frame, never across frames."""
    def __init__(self, capture):
        self.capture = capture
        self.monitors = capture.monitors
        self.frames = {}

    def begin_frame(self):
        self.frames.clear()

    def grab(self, monitor):
        key = tuple(monitor.get(k) for k in ('left', 'top', 'width', 'height'))
        if key not in self.frames:
            self.frames[key] = self.capture.grab(monitor)
        return self.frames[key]


@lru_cache(maxsize=64)
def edge_plan(h, w, top, bottom, left, right, width, offset):
    plans = []
    # Preserve RIGHT bottom-to-top, TOP right-to-left, LEFT, BOTTOM order.
    for count, axis, start, stop, reverse in (
        (right, 1, max(0, w-width-offset), max(0, w-offset) or w, True),
        (top, 0, offset, width+offset, True),
        (left, 1, offset, width+offset, False),
        (bottom, 0, max(0, h-width-offset), max(0, h-offset) or h, False),
    ):
        if count <= 0:
            continue
        length = h if axis == 1 else w
        boundaries = np.arange(count + 1, dtype=np.int64) * length // count
        plans.append((axis, start, stop, boundaries, reverse))
    return plans


def edge_rgb(screenshot, top, bottom, left, right, width, offset):
    h, w = screenshot.height, screenshot.width
    rgb = np.frombuffer(screenshot.raw, dtype=np.uint8).reshape(h, w, 4)[:, :, 2::-1]
    result = []
    for axis, start, stop, bounds, reverse in edge_plan(h, w, top, bottom, left, right, width, offset):
        strip = rgb[:, start:stop] if axis == 1 else rgb[start:stop, :]
        sums = strip.sum(axis=axis, dtype=np.uint64)
        prefix = np.concatenate((np.zeros((1, 3), dtype=np.uint64), sums.cumsum(axis=0)))
        totals = prefix[bounds[1:]] - prefix[bounds[:-1]]
        sizes = (np.diff(bounds) * strip.shape[axis]).astype(np.uint64)
        colors = np.zeros((len(sizes), 3), dtype=np.uint8)
        valid = sizes > 0
        colors[valid] = totals[valid] // sizes[valid, None]
        if reverse:
            colors = colors[::-1]
        result.append(colors)
    return np.concatenate(result) if result else np.empty((0, 3), dtype=np.uint8)


def edge_colors(screenshot, top, bottom, left, right, width, offset):
    """Compatibility view for callers that require RGB tuples."""
    return list(map(tuple, edge_rgb(screenshot, top, bottom, left, right, width, offset).tolist()))


def pack_rgb(colors, brightness=255):
    """Preserve legacy int(channel * (brightness / 255.0)) truncation."""
    if brightness < 255:
        colors = (colors * (brightness / 255.0)).astype(np.uint8)
    return colors.tobytes(order='C')


@lru_cache(maxsize=64)
def solid_frame(r, g, b, count):
    return bytes((r, g, b)) * count


def frame_delay(start, now, interval):
    """Do not accumulate debt or spin when a frame overruns its budget."""
    return max(0.001, interval - (now - start))
