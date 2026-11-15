import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from diagnostic_logs import DiagnosticLogHandler
from wemos_diagnostics import DevicePoll, InvalidDiagnostics, WemosDiagnostics, validated_batch

BOOT = '0123456789abcdef'
DEVICE = {'id': 'test', 'name': 'Wemos', 'ip': '192.0.2.1'}


def batch(after=0, items=None):
    if items is None:
        items = [{'seq': 1, 'uptime_ms': 50, 'code': 1, 'arg': 4}]
    return {
        'schema': 1, 'boot_id': BOOT, 'uptime_ms': 1050,
        'oldest_seq': 1, 'latest_seq': 1, 'next_seq': 1, 'lost': 0,
        'reset_reason': 4, 'events': items if after == 0 else [],
    }


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'build' / 'tests'
        root.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(directory.cleanup)
        self.handler = DiagnosticLogHandler(directory.name)
        self.addCleanup(self.handler.close)
        self.logger = logging.Logger('diagnostic-test')
        self.logger.addHandler(self.handler)
        self.collector = WemosDiagnostics(self.logger, [], lambda: [DEVICE])

    def test_disk_failure_keeps_one_visible_event_and_retries_cursor(self):
        state = DevicePoll()
        with patch('wemos_diagnostics.read_json', side_effect=[
            {'diagnostics_version': 1}, batch(), batch(), batch(), batch(1),
        ]):
            with patch.object(self.handler, '_persist', side_effect=OSError):
                self.collector._poll(DEVICE, state)
                self.collector._poll(DEVICE, state)
                self.assertEqual(state.seq, 0)
                self.assertFalse(self.handler.snapshot()['persistent'])
                self.assertEqual(len(self.handler.snapshot()['logs']), 1)
            self.collector._poll(DEVICE, state)
            self.collector._poll(DEVICE, state)
        self.assertEqual(state.seq, 1)
        history = self.handler.snapshot()
        self.assertTrue(history['persistent'])
        self.assertEqual(len(history['logs']), 1)
        row = history['logs'][0]
        self.assertEqual(row['source'], 'wemos')
        self.assertEqual(row['details']['boot_id'], BOOT)
        restarted = DiagnosticLogHandler(self.handler.path.parent)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.snapshot()['logs'], [row])
        self.assertEqual(restarted.snapshot(after=row['id'])['logs'], [])

    def test_malformed_batch_cannot_advance_cursor(self):
        with self.assertRaises(InvalidDiagnostics):
            validated_batch(batch(items=[{'seq': 2, 'uptime_ms': 0, 'code': 1, 'arg': 4}]), '', 0)

    def test_older_firmware_is_reported_once(self):
        state = DevicePoll()
        with patch('wemos_diagnostics.read_json', return_value={'ip': DEVICE['ip']}) as get:
            self.collector._poll(DEVICE, state)
            self.collector._poll(DEVICE, state)
        self.assertEqual([call.args[1] for call in get.call_args_list], ['/status', '/status'])
        self.assertEqual([row['code'] for row in self.handler.snapshot()['logs']], ['diagnostics_unsupported'])

    def test_new_boot_with_overwritten_ring_saves_boot_gap_and_retained_event(self):
        state = DevicePoll(boot='fedcba9876543210', seq=18, supported=True)
        payload = batch(items=[{'seq': 8, 'uptime_ms': 50, 'code': 6, 'arg': 7777}])
        payload.update(oldest_seq=8, latest_seq=8, next_seq=8, lost=7)
        with patch('wemos_diagnostics.read_json', return_value=payload):
            self.collector._poll(DEVICE, state)
        rows = self.handler.snapshot()['logs']
        self.assertEqual((state.boot, state.seq), (BOOT, 8))
        self.assertEqual([row['code'] for row in rows], ['boot', 'history_gap', 'udp_bound'])
        self.assertEqual(rows[1]['count'], 6)  # Opening metadata reconstructed sequence 1.

    def test_wifi_recovery_includes_outage_and_estimated_timestamp(self):
        state = DevicePoll(boot=BOOT, seq=1, supported=True)
        payload = batch(items=[
            {'seq': 2, 'uptime_ms': 50, 'code': 2, 'arg': 201},
            {'seq': 3, 'uptime_ms': 550, 'code': 3, 'arg': 500},
        ])
        payload.update(latest_seq=3, next_seq=3)
        with patch('wemos_diagnostics.read_json', return_value=payload):
            self.collector._poll(DEVICE, state)
        outage, recovery = self.handler.snapshot()['logs']
        self.assertIn('taramada', outage['possible_causes'][0])
        details = recovery['details']
        self.assertEqual(details['outage_ms'], 500)
        self.assertEqual(
            datetime.fromisoformat(details['received_at']) -
            datetime.fromisoformat(details['event_timestamp_estimated']),
            timedelta(milliseconds=500),
        )

    def test_rotation_keeps_ids_in_order_and_bounded_backups(self):
        with patch('diagnostic_logs.FILE_LIMIT', 600), patch('diagnostic_logs.BACKUPS', 2):
            for number in range(5):
                self.logger.info('event %s', number, extra={'source': 'wemos'})
            restarted = DiagnosticLogHandler(self.handler.path.parent)
            self.addCleanup(restarted.close)
        self.assertEqual(len(list(self.handler.path.parent.iterdir())), 3)
        rows = restarted.snapshot()['logs']
        self.assertEqual(rows[-1]['msg'], 'event 4')
        self.assertEqual([row['id'] for row in rows], sorted({row['id'] for row in rows}))
