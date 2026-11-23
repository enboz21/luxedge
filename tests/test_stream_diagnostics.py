import io
import logging
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from mss.exception import ScreenShotError

from backend_fixture import TEST_ROOT, backend
from connectivity import Connectivity
from diagnostic_logs import DiagnosticLogHandler, STREAM_LOG_QUEUE_LIMIT, STREAM_LOG_WARNING_RESERVE, STREAM_THREAD
from monitor_catalog import MonitorCatalog
from stream_diagnostics import StreamDiagnostics
from test_frame_processing import SyntheticCapture


class StreamDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        profile = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(profile.cleanup)
        self.handler = DiagnosticLogHandler(profile.name)
        self.addCleanup(self.handler.close)
        self.log = logging.Logger('stream-test')
        self.log.addHandler(self.handler)

    def test_worker_reports_distinct_failed_stages_and_udp_recovery(self):
        cfg = {'wemos_ip': '192.0.2.7', 'wemos_port': 7779, 'fps': 60, 'top_leds': 2,
               'bottom_leds': 0, 'left_leds': 1, 'right_leds': 1, 'edge_width': 2, 'led_monitor_index': 1}
        capture = SyntheticCapture()
        grab = capture.grab
        rgb = backend.edge_rgb
        captures = calculations = sends = iterations = 0

        def fake_grab(monitor):
            nonlocal captures
            captures += 1
            if captures == 1:
                failure = ScreenShotError('BitBlt')
                failure.details = {'func': 'BitBlt', 'error_code': 0}
                raise failure
            return grab(monitor)

        def fake_edge_rgb(*args):
            nonlocal calculations
            calculations += 1
            if calculations == 1:
                raise ValueError('RGB calculation failed')
            return rgb(*args)

        def fake_send(payload, address):
            nonlocal sends
            sends += 1
            if sends <= 2:
                raise OSError(10055, 'Buffers are full', None, 10055)
            self.assertEqual(address, ('192.0.2.7', 7779))
            return len(payload)

        def sleep(delay):
            nonlocal iterations
            iterations += 1
            if iterations >= 7:
                backend.running = False

        capture.grab = fake_grab
        udp = SimpleNamespace(sendto=fake_send, getsockname=lambda: ('0.0.0.0', 53011), close=lambda: None)
        fake_time = SimpleNamespace(sleep=sleep, perf_counter=backend.time.perf_counter, time=backend.time.time)
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(io.StringIO()))
            for name, value in {
                'running': True, 'log': self.log, 'connectivity': Connectivity(), 'monitor_catalog': MonitorCatalog(),
                'load_config': lambda: cfg, 'get_local_ip': lambda: None, 'mss': lambda: capture,
                'get_led_monitor_index': lambda sct: 1, 'time': fake_time,
                'socket': SimpleNamespace(socket=lambda *args: udp, AF_INET=2, SOCK_DGRAM=2),
                'wemos_connectivity_checker': lambda *args: None, 'edge_rgb': fake_edge_rgb,
            }.items():
                stack.enter_context(mock.patch.object(backend, name, value))
            stack.enter_context(mock.patch.object(backend.ctypes.windll.winmm, 'timeBeginPeriod', return_value=1))
            self.handler.start_stream_writer()
            stack.callback(self.handler.stop_stream_writer)
            worker = threading.Thread(target=backend.ambilight_worker, args=(cfg,), name=STREAM_THREAD)
            worker.start()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())

        rows = self.handler.snapshot()['logs']
        self.assertEqual((captures, calculations, sends), (7, 6, 5))
        failures = {row['details']['stage']: row for row in rows
                    if row['code'] == 'frame_failed' and row['level'] == 'ERROR'}
        self.assertEqual(set(failures), {'capture', 'color', 'udp_send'})
        self.assertEqual(failures['capture']['details']['error_type'], 'ScreenShotError')
        self.assertEqual(failures['capture']['details']['winerror'], 0)
        self.assertEqual(failures['capture']['details']['windows_function'], 'BitBlt')
        self.assertEqual(failures['capture']['details']['monitor']['width'], 97)
        self.assertEqual(failures['udp_send']['details']['winerror'], 10055)
        self.assertIn('192.0.2.7:7779', failures['udp_send']['msg'])
        repeated = next(row for row in rows if row['code'] == 'frame_failed' and row['level'] == 'WARNING')
        self.assertEqual(repeated['count'], 1)
        self.assertEqual(repeated['details']['winerror'], 10055)
        for row in failures.values():
            details = row['details']
            self.assertGreaterEqual(details['stage_ms'], 0)
            self.assertEqual(set(details['timings_ms']), {'capture', 'color', 'udp_send'})
            self.assertRegex(details['error_timestamp'], r'\.\d{3}\+00:00$')
            self.assertRegex(row['time'], r'\.\d{3}$')

        onset, recovery = [row for row in rows if row['code'] in {'udp_send_recovered', 'udp_send_error_started'}]
        self.assertEqual(onset['details']['consecutive_errors'], 1)
        self.assertEqual(recovery['details']['consecutive_errors'], 2)
        self.assertLessEqual(recovery['details']['first_error_at'], recovery['details']['last_error_at'])
        report = next(row['details'] for row in rows if row['code'] == 'udp_send_sample')
        self.assertEqual((report['attempted_packets'], report['accepted_packets'], report['accepted_bytes'],
                          report['send_errors']), (5, 3, 36, 2))
        self.assertIsNotNone(datetime.fromisoformat(report['last_successful_send_at']))
        timing_summary = next(row for row in rows if row['code'] == 'frame_timings')
        self.assertIn('LED rengi hesaplama', timing_summary['msg'])
        self.assertEqual(set(timing_summary['details']['average_ms']), {'capture', 'color', 'udp_send'})

    def test_one_second_windows_keep_targets_and_idle_gaps_separate(self):
        targets = [('a', '192.0.2.1', 7777), ('b', '192.0.2.2', 7778)]
        clock = [10.0]
        sampler = StreamDiagnostics(self.log)
        fake_clock = SimpleNamespace(perf_counter=lambda: clock[0], time_ns=lambda: int(clock[0] * 1_000_000_000))
        with mock.patch('stream_diagnostics.time', fake_clock), redirect_stdout(io.StringIO()):
            sampler.configure(targets, 40)
            for now in (10.25, 10.5, 10.75):
                clock[0] = now
                sampler.attempt(targets[0])
                sampler.accepted(targets[0], 222)
            clock[0] = 11
            sampler.report()
            clock[0] = 12
            sampler.report()
        one, idle, two, still_idle = [row['details'] for row in self.handler.snapshot()['logs']]
        self.assertEqual((one['target_fps'], one['measured_fps'], one['accepted_bytes'], one['longest_gap_ms']),
                         (40, 3, 666, 250))
        self.assertEqual((idle['port'], idle['measured_fps'], idle['longest_gap_ms']), (7778, 0, 1000))
        self.assertEqual((two['measured_fps'], two['longest_gap_ms']), (0, 1250))
        self.assertEqual((still_idle['longest_gap_ms'], still_idle['last_successful_send_at']), (2000, None))

    def test_blocked_disk_does_not_block_producer_and_overflow_is_reported(self):
        entered = threading.Event()
        release = threading.Event()
        real_persist = self.handler._persist

        def delayed_persist(row):
            entered.set()
            if not release.wait(timeout=5):
                raise OSError('fake disk timeout')
            real_persist(row)

        capacity = STREAM_LOG_QUEUE_LIMIT - STREAM_LOG_WARNING_RESERVE
        with redirect_stdout(io.StringIO()), mock.patch.object(self.handler, '_persist', side_effect=delayed_persist):
            self.handler.start_stream_writer()
            try:
                seed = threading.Thread(target=self.log.info, args=('seed',), name=STREAM_THREAD)
                seed.start()
                seed.join(timeout=2)
                self.assertTrue(entered.wait(timeout=2))

                def burst():
                    for index in range(capacity + 7):
                        self.log.info('sample %s', index)
                    self.log.error('kept warning')

                producer = threading.Thread(target=burst, name=STREAM_THREAD)
                producer.start()
                producer.join(timeout=2)
                self.assertFalse(producer.is_alive())
            finally:
                release.set()
                self.handler.stop_stream_writer()

        rows = self.handler.snapshot()['logs']
        self.assertEqual(len(rows), capacity + 3)
        loss = next(row for row in rows if row['code'] == 'stream_log_queue_dropped')
        self.assertEqual(loss['details']['dropped_records'], 7)
        self.assertIn('kept warning', [row['msg'] for row in rows])
