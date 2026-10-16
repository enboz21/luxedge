import threading
import unittest
from unittest import mock

from device_overview import DeviceOverview
from backend_fixture import backend


class OverviewTests(unittest.TestCase):
    def test_rgb_samples_are_bounded_and_throttled(self):
        store = DeviceOverview()
        store.record_frame('a', '192.0.2.1', 7777, bytes([1, 2, 3] * 170), now=100)
        frame = store.frames[('a', '192.0.2.1', 7777)]
        self.assertEqual(len(frame['colors']), 16)
        self.assertEqual(frame['colors'][0], [1, 2, 3])
        store.record_frame('a', '192.0.2.1', 7777, bytes([9, 9, 9]), now=100.1)
        self.assertEqual(store.frames[('a', '192.0.2.1', 7777)], frame)
        store.record_frame('a', '192.0.2.1', 7777, bytes([9, 9, 9]), now=100.3)
        self.assertEqual(store.frames[('a', '192.0.2.1', 7777)]['colors'], [[9, 9, 9]])

    def test_slow_probe_does_not_block_snapshot_or_duplicate_refresh(self):
        store = DeviceOverview()
        entered, release = threading.Event(), threading.Event()
        device = dict(id='a', ip='192.0.2.1', port=7777)
        def probe(ip, timeout):
            entered.set()
            release.wait(2)
            return dict(sleep_mode=True)
        try:
            result = store.snapshot([device], probe)
            self.assertIsNone(result[0]['live'])
            self.assertTrue(entered.wait(1))
            with mock.patch('device_overview.threading.Thread') as thread:
                store.snapshot([device], probe)
                thread.assert_not_called()
        finally:
            release.set()

    def test_stream_samples_follow_each_strips_packet_slice(self):
        store = DeviceOverview()
        store.record_frame('a', '192.0.2.1', 7777, bytes([255, 0, 0] * 5 + [0, 0, 255] * 8),
                           strips=[dict(index=1, led_count=5), dict(index=3, led_count=8)])
        streams = store.frames[('a', '192.0.2.1', 7777)]['streams']
        self.assertEqual(streams['1'], [[255, 0, 0]] * 5)
        self.assertEqual(streams['3'], [[0, 0, 255]] * 8)

    def test_address_change_cannot_reuse_another_targets_state(self):
        store = DeviceOverview()
        store.refreshing = True
        store.live[('a', '192.0.2.1', 7777)] = dict(reachable=True)
        store.record_frame('a', '192.0.2.1', 7777, b'\x01\x02\x03')
        result = store.snapshot([dict(id='a', ip='192.0.2.2', port=7777)], None)
        self.assertIsNone(result[0]['live'])
        self.assertIsNone(result[0]['frame'])

    def test_probe_failure_is_recorded_as_offline(self):
        store = DeviceOverview()
        device = dict(id='a', ip='192.0.2.1', port=7777)
        store._refresh([device], mock.Mock(side_effect=OSError('offline')))
        self.assertFalse(store.live[store.key(device)]['reachable'])

    def test_api_lists_every_record_without_writing_config(self):
        handler = object.__new__(backend.WebUIHandler)
        handler._safe_send_json = mock.Mock()
        devices = [dict(id='a'), dict(id='b', enabled=False)]
        with mock.patch.object(backend, 'load_config', return_value={}), \
             mock.patch.object(backend, 'normalize_config_for_multi_device', return_value={
                 'wemos_devices': devices, 'multi_device_enabled': False}), \
             mock.patch.object(backend.device_overview, 'snapshot', return_value=devices), \
             mock.patch.object(backend, 'save_config') as save:
            handler.handle_devices_overview()
        self.assertEqual(handler._safe_send_json.call_args.args[0]['devices'], devices)
        save.assert_not_called()
