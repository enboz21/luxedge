import unittest
from unittest import mock

from backend_fixture import backend
from monitor_catalog import MonitorCatalog


class MonitorTests(unittest.TestCase):
    def test_startup_loads_catalog_once_without_starting_real_services(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            def patch(name, **kwargs):
                return stack.enter_context(mock.patch.object(backend, name, **kwargs))
            catalog = MonitorCatalog()
            patch('monitor_catalog', new=catalog)
            scan = patch('get_available_monitors', return_value=[{'index': 1}])
            patch('load_config', return_value={'wemos_ip': '', 'fps': 60})
            patch('get_local_ip', return_value=None)
            patch('start_web_server', return_value=None)
            patch('_run_console_mode')
            stack.enter_context(mock.patch.object(backend.threading, 'Thread'))
            stack.enter_context(mock.patch.object(backend.sys, 'argv', ['backend', '--no-tray']))
            stack.enter_context(mock.patch('builtins.print'))
            backend.main()
            scan.assert_called_once_with()
            self.assertEqual(catalog.version, 1)

    def test_catalog_enumerates_only_on_explicit_refresh(self):
        catalog = MonitorCatalog()
        enumerate_monitors = mock.Mock(return_value=[{'index': 1, 'width': 1920}])
        self.assertTrue(catalog.refresh(enumerate_monitors)['success'])
        for _ in range(100):
            self.assertEqual(catalog.snapshot()['monitors'][0]['width'], 1920)
        self.assertEqual(enumerate_monitors.call_count, 1)
        enumerate_monitors.return_value = [{'index': 1, 'width': 2560}]
        self.assertEqual(catalog.snapshot()['monitors'][0]['width'], 1920)
        self.assertEqual(catalog.refresh(enumerate_monitors)['monitors'][0]['width'], 2560)
        self.assertEqual(enumerate_monitors.call_count, 2)

    def test_failed_refresh_preserves_previous_list_and_generation(self):
        catalog = MonitorCatalog()
        catalog.refresh(lambda: [{'index': 2}])
        before = catalog.snapshot()
        failed = catalog.refresh(lambda: [])
        self.assertFalse(failed['success'])
        self.assertEqual(catalog.snapshot(), before)
        failed['monitors'][0]['index'] = 99
        self.assertEqual(catalog.snapshot(), before)

    def test_status_has_no_config_reads_or_display_enumeration(self):
        handler = object.__new__(backend.WebUIHandler)
        handler._safe_send_json = mock.Mock()
        catalog = MonitorCatalog()
        catalog.refresh(lambda: [{'index': 2, 'width': 2560}])
        with mock.patch.object(backend, 'monitor_catalog', catalog), \
             mock.patch.object(backend, 'get_available_monitors', side_effect=AssertionError('Rescan forbidden')), \
             mock.patch.object(backend, 'load_config', side_effect=AssertionError('Config read forbidden')), \
             mock.patch.object(backend, 'get_status', return_value={'uptime_start': 0, 'led_monitor_index': 2}):
            for _ in range(100):
                handler.serve_status()
        result = handler._safe_send_json.call_args.args[0]
        self.assertEqual(result['monitors'], [{'index': 2, 'width': 2560}])
        self.assertEqual(result['led_monitor_index'], 2)

    def test_refresh_endpoint_enumerates_without_saving_config(self):
        handler = object.__new__(backend.WebUIHandler)
        handler.path = '/api/monitors/refresh'
        handler._safe_send_json = mock.Mock()
        with mock.patch.object(backend, 'monitor_catalog', MonitorCatalog()), \
             mock.patch.object(backend, 'get_available_monitors', return_value=[{'index': 1}]) as scan, \
             mock.patch.object(backend, 'save_config', side_effect=AssertionError('Config write forbidden')):
            handler.do_POST()
        scan.assert_called_once_with()
        self.assertTrue(handler._safe_send_json.call_args.args[0]['success'])
