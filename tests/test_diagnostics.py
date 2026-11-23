import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

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
        self.assertFalse(state.probes['/diagnostics']['tested'])
        self.assertIsNone(state.probes['/diagnostics']['ok'])
        self.assertIn('firmware', state.probes['/diagnostics']['skip_reason'])

    def test_status_timeout_preserves_evidence_and_reports_recovery(self):
        state = DevicePoll()
        with patch('wemos_diagnostics.time.monotonic', return_value=100), \
             patch('wemos_diagnostics.read_json', side_effect=[
                 URLError(TimeoutError('timeout')), {'diagnostics_version': 1}, batch(),
             ]) as get:
            self.collector._poll(DEVICE, state)
            self.collector._log_probes(DEVICE, state)
            self.assertEqual(state.next_poll, 130)
            self.assertFalse(state.connected)
            self.collector._poll(DEVICE, state)
        unavailable, probe_log, recovered, boot = self.handler.snapshot()['logs']
        self.assertEqual([unavailable['code'], recovered['code'], boot['code']],
                         ['wemos_unreachable', 'wemos_reachable', 'boot'])
        failed, skipped = probe_log['details']['probes']
        self.assertEqual((failed['tested'], failed['ok'], failed['error_type'], failed['timed_out']),
                         (True, False, 'TimeoutError', True))
        self.assertEqual((failed['timeout_ms'], failed['consecutive_failures']), (1500, 1))
        self.assertEqual((skipped['tested'], skipped['ok'], skipped['consecutive_failures']), (False, None, 0))
        self.assertEqual(probe_log['details']['next_try_in_ms'], 30000)
        self.assertEqual([call.args[1].partition('?')[0] for call in get.call_args_list],
                         ['/status', '/status', '/diagnostics'])

    def test_changed_ip_rechecks_capability_during_old_target_backoff(self):
        replacement = {**DEVICE, 'ip': '192.0.2.2'}
        now = 0

        class StopAfterTwoRefreshes:
            def is_set(self):
                return now >= 10

            def wait(self, delay):
                nonlocal now
                now += 5

        with patch.object(self.collector, 'devices', side_effect=[[DEVICE], [replacement]]), \
             patch('wemos_diagnostics.time.monotonic', side_effect=lambda: now), \
             patch('wemos_diagnostics.read_json', side_effect=[
                 URLError(TimeoutError('old target')), {'diagnostics_version': 1}, batch(),
             ]) as get:
            self.collector.run(StopAfterTwoRefreshes())
        self.assertEqual([(call.args[0], call.args[1].partition('?')[0]) for call in get.call_args_list],
                         [(DEVICE['ip'], '/status'), (replacement['ip'], '/status'),
                          (replacement['ip'], '/diagnostics')])
        rows = self.handler.snapshot()['logs']
        change = next(row for row in rows if row['code'] == 'diagnostics_target_changed')
        self.assertEqual(change['details']['previous_ip'], DEVICE['ip'])
        self.assertEqual(change['details']['ip'], replacement['ip'])
        latest_summary = rows[-1]
        self.assertEqual(latest_summary['code'], 'wemos_http_probes')
        self.assertTrue(all(probe['consecutive_failures'] == 0 for probe in latest_summary['details']['probes']))

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

    def test_stream_transition_arguments_and_estimates_survive_uptime_wrap(self):
        state = DevicePoll(boot=BOOT, seq=1, supported=True)
        payload = batch(items=[
            {'seq': 2, 'uptime_ms': 2**32 - 900, 'code': 18, 'arg': 74},
            {'seq': 3, 'uptime_ms': 2**32 - 350, 'code': 19, 'arg': 550},
            {'seq': 4, 'uptime_ms': 70, 'code': 20, 'arg': 970},
        ])
        payload.update(latest_seq=4, next_seq=4, uptime_ms=100)
        with patch('wemos_diagnostics.read_json', return_value=payload):
            self.collector._poll(DEVICE, state)
        first, idle, resumed = self.handler.snapshot()['logs']
        self.assertEqual([row['code'] for row in (first, idle, resumed)],
                         ['stream_first_frame', 'stream_idle_entered', 'stream_idle_exited'])
        self.assertEqual(first['details']['applied_led_count'], 74)
        self.assertEqual([idle['details']['stream_gap_ms'], resumed['details']['stream_gap_ms']], [550, 970])
        for row, age in zip((first, idle, resumed), (1000, 450, 30)):
            details = row['details']
            self.assertEqual(details['event_age_ms_at_receipt'], age)
            self.assertEqual(
                datetime.fromisoformat(details['received_at']) -
                datetime.fromisoformat(details['event_timestamp_estimated']),
                timedelta(milliseconds=age),
            )

    def test_diagnostic_failure_evidence_takes_precedence_over_optional_status_sample(self):
        state = DevicePoll(boot=BOOT, seq=1, supported=True, next_status_poll=0)
        with patch('wemos_diagnostics.read_json', side_effect=[
            TimeoutError('optional status sample'), InvalidDiagnostics('invalid diagnostic batch'),
        ]):
            self.collector._poll(DEVICE, state)
        failure = self.handler.snapshot()['logs'][-1]
        self.assertEqual(failure['code'], 'diagnostics_invalid')
        evidence = failure['details']['probe']
        self.assertEqual(evidence['endpoint'], 'HTTP /diagnostics')
        self.assertEqual((evidence['error_type'], evidence['timed_out']), ('InvalidDiagnostics', False))

    def test_status_sample_preserves_measured_led_values(self):
        state = DevicePoll()
        status = {'diagnostics_version': 1, 'last_data_age_ms': 710, 'last_udp_age_ms': 10,
                  'receiving_data': False, 'rssi': -72, 'wifi_connected': True, 'udp_rebind_count': 2}
        with patch('wemos_diagnostics.read_json', side_effect=[status, batch()]):
            self.collector._poll(DEVICE, state)
        self.collector._log_status_sample(DEVICE, state.status_sample)
        sample = self.handler.snapshot()['logs'][-1]
        self.assertEqual(sample['code'], 'wemos_status_sample')
        observed = sample['details']
        self.assertEqual((observed['last_data_age_ms'], observed['last_udp_age_ms'], observed['rssi_dbm']),
                         (710, 10, -72))
        self.assertEqual((observed['receiving_data'], observed['udp_rebind_count']), (False, 2))
        self.assertIsNotNone(datetime.fromisoformat(observed['received_at']).tzinfo)

    def test_rotation_keeps_ids_and_archival_order_across_restart(self):
        with patch('diagnostic_logs.FILE_LIMIT', 400):
            for number in range(5):
                self.logger.info('event %s', number, extra={'source': 'wemos'})
            restarted = DiagnosticLogHandler(self.handler.path.parent)
            self.addCleanup(restarted.close)
        archives = list(self.handler.path.parent.glob('luxedge.jsonl.seg-*'))
        self.assertEqual(len(archives), 4)
        rows = restarted.snapshot()['logs']
        self.assertEqual([row['msg'] for row in rows], [f'event {index}' for index in range(5)])
        self.assertEqual(rows[-1]['msg'], 'event 4')
        self.assertEqual([row['id'] for row in rows], sorted({row['id'] for row in rows}))
        self.assertEqual([row['archive_order_us'] for row in rows], sorted({row['archive_order_us'] for row in rows}))
