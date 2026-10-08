import types
import unittest
from unittest import mock

import numpy as np

from backend_fixture import backend
from frame_processing import FrameCapture, edge_colors, edge_rgb, pack_rgb, edge_plan, solid_frame, frame_delay
from reference_edges import grab_edge_colors as original_colors
from monitor_catalog import MonitorCatalog


class SyntheticCapture:
    def __init__(self, w=97, h=61):
        self.monitors = [{}, {'left': 0, 'top': 0, 'width': w, 'height': h},
                         {'left': w, 'top': 0, 'width': w, 'height': h}]
        self.raw = np.random.default_rng(42).integers(0, 256, (h, w, 4), dtype=np.uint8)
        self.image = types.SimpleNamespace(raw=self.raw.tobytes(), width=w, height=h)
        self.calls = []

    def grab(self, monitor):
        self.calls.append(monitor)
        return self.image

    def close(self):
        pass


class FrameTests(unittest.TestCase):
    def test_vector_packet_matches_all_legacy_brightness_values(self):
        values = np.arange(256, dtype=np.uint8)
        colors = np.column_stack((values, values[::-1], np.roll(values, 7)))
        for brightness in range(256):
            expected = bytes(int(channel * (brightness / 255.0)) for rgb in colors for channel in rgb)
            self.assertEqual(pack_rgb(colors, brightness), expected)
        self.assertEqual(pack_rgb(np.empty((0, 3), dtype=np.uint8)), b'')

    def test_direct_packet_matches_legacy_edges_without_tuple_conversion(self):
        capture = SyntheticCapture()
        for counts in ((34, 0, 20, 20), (12, 11, 7, 8), (0, 0, 0, 0)):
            colors = original_colors(*counts, 10, 5, capture, monitor_index=1)
            for brightness in (0, 1, 127, 254, 255):
                expected = bytes(int(c * (brightness / 255.0)) for rgb in colors for c in rgb)
                self.assertEqual(backend.grab_edge_frame(*counts, 10, 5, capture, 1, brightness), expected)

    def test_exact_legacy_color_and_order_for_synthetic_frames(self):
        for w, h in ((97, 61), (192, 108), (5, 3)):
            capture = SyntheticCapture(w, h)
            for counts in ((34, 0, 20, 20), (12, 11, 7, 8), (0, 0, 0, 0), (1, 2, 3, 4)):
                for width, offset in ((1, 0), (2, 1)):
                    with self.subTest(size=(w, h), counts=counts, width=width, offset=offset):
                        expected = original_colors(*counts, width, offset, capture, monitor_index=1)
                        actual = edge_colors(capture.image, *counts, width, offset)
                        self.assertEqual(actual, expected)
                        self.assertEqual(len(actual), sum(counts))

    def test_capture_is_shared_only_within_frame(self):
        capture = SyntheticCapture()
        shared = FrameCapture(capture)
        shared.begin_frame()
        for monitor in (1, 1, 2, 2):
            shared.grab(shared.monitors[monitor])
        self.assertEqual(len(capture.calls), 2)
        shared.begin_frame()
        shared.grab(shared.monitors[1])
        self.assertEqual(len(capture.calls), 3)

    def test_regions_and_solid_packets_are_cached(self):
        args = (61, 97, 34, 0, 20, 20, 10, 5)
        self.assertIs(edge_plan(*args), edge_plan(*args))
        self.assertIsNot(edge_plan(*args), edge_plan(*args[:-1], 6))
        self.assertIs(solid_frame(12, 34, 56, 74), solid_frame(12, 34, 56, 74))
        self.assertEqual(solid_frame(12, 34, 56, 2), bytes((12, 34, 56)) * 2)
        self.assertNotEqual(solid_frame(12, 34, 56, 2), solid_frame(12, 34, 57, 2))

    def test_explicit_monitor_does_not_read_configuration(self):
        with mock.patch.object(backend, 'load_config', side_effect=AssertionError('Frame disk access')):
            colors = backend.grab_edge_colors(34, 0, 20, 20, 10, 5, SyntheticCapture(), monitor_index=1)
        self.assertEqual(len(colors), 74)

    def test_frame_deadline_never_catches_up_in_a_busy_loop(self):
        self.assertAlmostEqual(frame_delay(1, 1.006, 1/60), 1/60 - .006)
        self.assertEqual(frame_delay(1, 2, 1/60), .001)

    def run_worker(self, *, multi=False, idle=False, frames=7, refresh=None, timer_failure=False, manual_refresh=False, refresh_fails=False):
        from contextlib import ExitStack
        devices = [dict(id=str(i), ip='192.0.2.' + str(i+1), port=7777, enabled=True,
                        monitor_index=1, leds={'top': 2, 'bottom': 0, 'left': 1, 'right': 1}) for i in range(2)]
        config = dict(wemos_ip='192.0.2.1', wemos_port=7777, fps=60, top_leds=2, bottom_leds=0,
                      left_leds=1, right_leds=1, edge_width=2, edge_offset=0,
                      led_monitor_index=1, idle_mode=idle, idle_color='#ff0000', idle_brightness=50,
                      multi_device_enabled=multi, wemos_devices=devices)
        capture = SyntheticCapture()
        udp = mock.Mock()
        clock = [100.0]
        sleeps = []
        packets = []

        def send(data, address):
            packets.append((bytes(data), address))

        def sleep(delay):
            sleeps.append(delay)
            clock[0] += 1
            if manual_refresh and len(sleeps) == 2:
                backend.monitor_catalog.refresh(lambda: [{'index': 1}])
            if len(sleeps) >= frames:
                backend.running = False

        udp.sendto.side_effect = send
        with ExitStack() as stack:
            def patch(name, **kwargs):
                return stack.enter_context(mock.patch.object(backend, name, **kwargs))
            patch('running', new=True)
            patch('get_local_ip', return_value=None)
            patch('is_fullscreen', return_value=False)
            factory = patch('mss', return_value=capture)
            if refresh_fails:
                factory.side_effect = [capture, OSError('simulated display error')]
            patch('monitor_catalog', new=MonitorCatalog())
            patch('log', new=mock.Mock())
            patch('get_led_monitor_index', return_value=1)
            load = patch('load_config', side_effect=lambda: dict(refresh or config))
            patch('get_device_validation', return_value={'compatible': True})
            stack.enter_context(mock.patch.object(backend.socket, 'socket', return_value=udp))
            stack.enter_context(mock.patch.object(backend.urllib.request, 'urlopen', side_effect=AssertionError('Network forbidden')))
            stack.enter_context(mock.patch.object(backend.subprocess, 'run', side_effect=AssertionError('Process forbidden')))
            stack.enter_context(mock.patch.object(backend.threading, 'Thread'))
            stack.enter_context(mock.patch.object(backend.time, 'perf_counter', side_effect=lambda: clock[0]))
            stack.enter_context(mock.patch.object(backend.time, 'sleep', side_effect=sleep))
            begin = stack.enter_context(mock.patch.object(backend.ctypes.windll.winmm, 'timeBeginPeriod', return_value=1 if timer_failure else 0))
            end = stack.enter_context(mock.patch.object(backend.ctypes.windll.winmm, 'timeEndPeriod'))
            backend.ambilight_worker(config)
            self.assertEqual(end.call_count, 0 if timer_failure else 1)
            self.assertEqual(begin.call_count, 1)
            self.assertEqual(factory.call_count, 2 if manual_refresh else 1)
        return capture, packets, load.call_count, sleeps

    def test_single_worker_has_no_shadow_captures_or_per_frame_reads(self):
        capture, packets, reads, sleeps = self.run_worker()
        self.assertEqual(len(capture.calls), 7)
        self.assertEqual(reads, 2)  # only periodic 3-second refresh
        self.assertEqual(len(packets), 7)
        self.assertEqual({address for _, address in packets}, {('192.0.2.1', 7777)})
        self.assertTrue(all(len(data) == 12 for data, _ in packets))
        self.assertTrue(all(abs(delay-1/60) < .0001 for delay in sleeps))

    def test_multi_worker_shares_capture_and_omits_unused_single_frame(self):
        capture, packets, _, _ = self.run_worker(multi=True)
        self.assertEqual(len(capture.calls), 7)
        # Existing mode activation timing (first 3-second refresh) is unchanged.
        self.assertEqual(len(packets), 3 + 4*2)

    def test_idle_worker_captures_nothing_and_keeps_udp_cadence(self):
        capture, packets, _, _ = self.run_worker(idle=True)
        self.assertEqual(capture.calls, [])
        self.assertEqual(len(packets), 7)
        self.assertTrue(all(data == bytes((127, 0, 0))*4 for data, _ in packets))

    def test_external_config_refresh_changes_color_and_fps(self):
        refresh = dict(wemos_ip='192.0.2.1', fps=30, top_leds=2, bottom_leds=0,
                       left_leds=1, right_leds=1, edge_width=2, edge_offset=0,
                       led_monitor_index=1, idle_mode=True, idle_color='#00ff00', idle_brightness=50)
        _, packets, _, sleeps = self.run_worker(idle=True, refresh=refresh)
        self.assertEqual(packets[0][0], bytes((127, 0, 0))*4)
        self.assertEqual(packets[-1][0], bytes((0, 127, 0))*4)
        self.assertAlmostEqual(sleeps[-1], 1/30)

    def test_failed_timer_acquisition_does_not_release_unowned_timer(self):
        self.run_worker(frames=1, timer_failure=True)

    def test_manual_monitor_refresh_recreates_capture_without_changing_packets(self):
        _, packets, _, _ = self.run_worker(manual_refresh=True)
        self.assertEqual(len(packets), 7)
        self.assertTrue(all(packet == packets[0] for packet in packets))

    def test_failed_capture_refresh_keeps_worker_running_without_retry_loop(self):
        _, packets, _, _ = self.run_worker(manual_refresh=True, refresh_fails=True)
        self.assertEqual(len(packets), 7)
