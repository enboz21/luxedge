import threading
import io
import json
import unittest
from unittest import mock
from connectivity import Connectivity
from backend_fixture import backend


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.tracker = Connectivity()
        self.tracker.reset('192.0.2.1', 7777)

    def result(self, udp, http):
        token = self.tracker.begin('192.0.2.1', 7777)
        self.assertTrue(self.tracker.complete(token, udp, http))
        return self.tracker.snapshot()

    def test_success_protocol_transition_and_unchecked_http(self):
        state = self.result(True, None)
        self.assertEqual(state['state'], 'connected')
        self.assertIsNone(state['http_ok'])
        self.assertEqual(self.result(False, True)['state'], 'http_only')
        self.assertEqual(self.result(True, True)['state'], 'connected')

    def test_three_failures_disconnect_and_one_success_recovers(self):
        self.result(True, None)
        for _ in range(2):
            self.assertEqual(self.result(False, False)['state'], 'connected')
        self.assertEqual(self.result(False, False)['state'], 'disconnected')
        self.assertEqual(self.result(False, True)['state'], 'http_only')
        self.assertEqual(self.tracker.snapshot()['consecutive_failures'], 0)

    def test_initial_failures_do_not_claim_connection(self):
        self.assertEqual(self.result(False, False)['state'], 'checking')
        self.result(False, False)
        self.assertEqual(self.result(False, False)['state'], 'disconnected')

    def test_target_port_stop_and_restart_invalidate_pending_results(self):
        for ip, port, active in [('192.0.2.2',7777,True), ('192.0.2.1',7778,True),
                                 ('192.0.2.1',7777,False), ('',7777,True)]:
            self.tracker.reset('192.0.2.1',7777,force=True)
            pending = self.tracker.begin('192.0.2.1',7777)
            self.tracker.reset(ip,port,active=active)
            self.assertFalse(self.tracker.complete(pending,True,True))
            self.assertIsNone(self.tracker.snapshot()['checked_at'])

    def test_late_manual_or_automatic_result_cannot_overwrite_newer_result(self):
        older = self.tracker.begin('192.0.2.1',7777)
        newer = self.tracker.begin('192.0.2.1',7777)
        self.assertTrue(self.tracker.complete(newer,False,True))
        self.assertFalse(self.tracker.complete(older,True,True))
        self.assertEqual(self.tracker.snapshot()['state'],'http_only')

    def test_status_ignores_fps_and_sent_packets(self):
        with mock.patch.object(backend,'connectivity',self.tracker), \
             mock.patch.dict(backend.app_status, {'actual_fps':60,'packets_sent':1000,'connection':'bağlı'}):
            for _ in range(3): self.result(False,False)
            self.assertEqual(backend.get_status()['connection'],'bağlı değil')
            self.assertEqual(backend.get_status()['connectivity']['state'],'disconnected')

    def test_probe_fallback_and_full_manual_test_share_checks(self):
        with mock.patch.object(backend,'ping_wemos',return_value=True), \
             mock.patch.object(backend,'check_wemos_http',return_value=True) as http:
            self.assertEqual(backend.probe_wemos('192.0.2.1'),(True,None))
            http.assert_not_called()
            self.assertEqual(backend.test_wemos_connection('192.0.2.1')[:2],(True,True))
            http.assert_called_once()
        with mock.patch.object(backend,'ping_wemos',return_value=False), \
             mock.patch.object(backend,'check_wemos_http',return_value=True):
            self.assertEqual(backend.probe_wemos('192.0.2.1'),(False,True))

    def test_cancel_during_probe_cannot_publish(self):
        cancel = threading.Event()
        def probe(*args):
            cancel.set()
            return True,None
        with mock.patch.object(backend,'connectivity',self.tracker), \
             mock.patch.object(backend,'running',True), \
             mock.patch.object(backend,'probe_wemos',side_effect=probe):
            backend.wemos_connectivity_checker('192.0.2.1',7777,cancel)
        self.assertEqual(self.tracker.snapshot()['state'],'checking')

    def test_manual_endpoint_updates_same_tracker(self):
        handler = object.__new__(backend.WebUIHandler)
        handler._safe_send_json = mock.Mock()
        with mock.patch.object(backend,'connectivity',self.tracker), \
             mock.patch.dict(backend.app_status, {'wemos_ip':'192.0.2.1','wemos_port':7777}), \
             mock.patch.object(backend,'test_wemos_connection',return_value=(True,False,'UDP')):
            handler.handle_test_wemos_connection()
        self.assertTrue(handler._safe_send_json.call_args.args[0]['applied'])
        self.assertEqual(self.tracker.snapshot()['state'],'connected')

    def test_saved_target_invalidates_pending_probe_before_worker_reload(self):
        pending = self.tracker.begin('192.0.2.1',7777)
        handler = object.__new__(backend.WebUIHandler)
        body = json.dumps({'wemos_port':7778}).encode()
        handler.headers = {'Content-Length':str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._safe_send_json = mock.Mock()
        with mock.patch.object(backend,'connectivity',self.tracker), \
             mock.patch.dict(backend.app_status), \
             mock.patch.object(backend,'running',True), \
             mock.patch.object(backend,'load_config',return_value={'wemos_ip':'192.0.2.1','wemos_port':7777}), \
             mock.patch.object(backend,'save_config',return_value=True):
            handler.handle_config_update()
            self.assertTrue(handler._safe_send_json.call_args.args[0]['success'])
            self.assertFalse(self.tracker.complete(pending,True,True))
            self.assertEqual(backend.get_status()['connectivity']['port'],7778)
            self.assertEqual(backend.get_status()['connectivity']['state'],'checking')

    def test_late_manual_endpoint_response_is_marked_obsolete(self):
        handler = object.__new__(backend.WebUIHandler)
        handler._safe_send_json = mock.Mock()
        def probe(*args):
            self.tracker.reset('192.0.2.2',7777)
            return True,True,'old result'
        with mock.patch.object(backend,'connectivity',self.tracker), \
             mock.patch.dict(backend.app_status, {'wemos_ip':'192.0.2.1','wemos_port':7777}), \
             mock.patch.object(backend,'test_wemos_connection',side_effect=probe):
            handler.handle_test_wemos_connection()
        self.assertFalse(handler._safe_send_json.call_args.args[0]['applied'])
        self.assertEqual(self.tracker.snapshot()['state'],'checking')
