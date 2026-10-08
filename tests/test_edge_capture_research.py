"""Feasibility of ROI colors only. No desktop capture or performance claim."""
from types import SimpleNamespace
import unittest

import numpy as np

from frame_processing import edge_rgb, pack_rgb


def image(pixels):
    return SimpleNamespace(raw=pixels.tobytes(), height=pixels.shape[0], width=pixels.shape[1])


def synthetic_roi_packet(pixels, counts, width, offset):
    h, w = pixels.shape[:2]
    top, bottom, left, right = counts
    packets = []
    for count, region, edges in (
        (right, pixels[:, w-width-offset:w-offset], (0, 0, 0, right)),
        (top, pixels[offset:offset+width], (top, 0, 0, 0)),
        (left, pixels[:, offset:offset+width], (0, 0, left, 0)),
        (bottom, pixels[h-width-offset:h-offset], (0, bottom, 0, 0)),
    ):
        if count:
            packets.append(pack_rgb(edge_rgb(image(region), *edges, width, 0)))
    return b''.join(packets)


class RoiResearchTests(unittest.TestCase):
    def test_separate_edge_regions_match_full_frame_colors(self):
        # Source array remains unchanged between the simulated region reads.
        for w, h in ((192, 108), (257, 145)):
            pixels = np.random.default_rng(73).integers(0, 256, (h, w, 4), dtype=np.uint8)
            for counts in ((34, 0, 20, 20), (34, 34, 20, 20), (0, 0, 0, 0)):
                for width, offset in ((1, 0), (10, 5), (20, 10)):
                    with self.subTest(size=(w, h), counts=counts, width=width, offset=offset):
                        expected = pack_rgb(edge_rgb(image(pixels), *counts, width, offset))
                        self.assertEqual(synthetic_roi_packet(pixels, counts, width, offset), expected)
