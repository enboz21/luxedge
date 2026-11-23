import ctypes
import logging
import threading
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timezone
from unittest import mock
from xml.sax.saxutils import escape

from backend_fixture import backend
import windows_wfp as wfp
from wemos_diagnostics import DevicePoll, WemosDiagnostics


PID = 4556
EXE = r'C:\Program Files\LuxEdge\lush_backend.exe'
DEVICE_EXE = r'\device\harddiskvolume2\program files\luxedge\lush_backend.exe'
PATHS = {wfp.path_key(path) for path in (EXE, DEVICE_EXE)}
REMOTE, LOCAL, LOCAL_PORT = '192.0.2.17', '192.0.2.4', 49278
STAMP = '2026-11-22T18:52:14.327456700Z'
SECONDS = datetime(2026, 11, 22, 18, 52, 14, tzinfo=timezone.utc).timestamp()
HTTP = wfp.Attempt('HTTP /status', REMOTE, 80, wfp.TCP, SECONDS, SECONDS + .4, ('outbound', 'inbound'), None, None)
PING = wfp.Attempt('UDP PING/PONG', REMOTE, 7777, wfp.UDP, SECONDS, SECONDS + .4,
                   ('outbound', 'inbound'), LOCAL, LOCAL_PORT)


def xml_record(provider, channel, event_id, timestamp, data, version=0, record=321323):
    fields = ''.join(f'<Data Name="{name}">{escape(str(value))}</Data>' for name, value in data.items())
    return (
        '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>'
        f'<Provider Name="{provider}"/><EventID>{event_id}</EventID><Version>{version}</Version>'
        f'<TimeCreated SystemTime="{timestamp}"/><EventRecordID>{record}</EventRecordID>'
        f'<Execution ProcessID="4" ThreadID="4456"/><Channel>{channel}</Channel></System>'
        f'<EventData>{fields}</EventData></Event>'
    )


def wfp_event(event_id=5157, timestamp=STAMP, version=1, **fields):
    # Field names and inbound token match Microsoft Learn's 5152 and 5157 XML examples.
    values = {'ProcessID' if event_id == 5157 else 'ProcessId': str(PID), 'Application': DEVICE_EXE,
              'Direction': '%%14593', 'SourceAddress': LOCAL, 'SourcePort': LOCAL_PORT,
              'DestAddress': REMOTE, 'DestPort': 7777, 'Protocol': 17, 'FilterRTID': 110398,
              'LayerName': '%%14610', 'LayerRTID': 44}
    values.update(fields)
    return xml_record(wfp.WFP_PROVIDER, wfp.SECURITY_CHANNEL, event_id, timestamp, values, version=version)


def defender_event(event_id=1126, *, timestamp='2026-11-22T18:52:15.452631Z', **fields):
    # The provider's MpEvMsg.dll 4.18.26010.5-0 WEVT_TEMPLATE has these exact seven fields for both IDs.
    values = {'Product Name': 'Microsoft Defender Antivirus', 'Product Version': '4.18.26010.5',
              'ID': '{8CA9032C-687D-4B38-90A1-232C6566971A}', 'Detection Time': '2026-11-22T18:52:14.327Z',
              'User': 'S-1-5-21-1-2-3-1001', 'Destination': f'http://{REMOTE}/status', 'Process Name': EXE}
    values.update(fields)
    return xml_record(wfp.DEFENDER_PROVIDER, wfp.DEFENDER_CHANNEL, event_id, timestamp, values)


class WindowsMatcherTests(unittest.TestCase):
    def test_wfp_win11_fields_and_exact_outbound_socket(self):
        event = wfp_event(FilterOrigin='{74C39319-E784-4A0F-82FC-B8966AC6B31B}', InterfaceIndex=4,
                          DestAddress=f'::ffff:{REMOTE}')
        matched = wfp.event_match(event, PING, PID, PATHS)
        self.assertEqual(matched['event_id'], 5157)
        self.assertEqual(matched['pid'], PID)
        self.assertEqual(matched['event_at'], STAMP)
        self.assertEqual(matched['protocol'], 'UDP')
        self.assertEqual(matched['direction'], 'outbound')
        self.assertEqual(matched['remote_endpoint_field'], 'destination')
        self.assertEqual(matched['attribution'], 'strong')
        self.assertEqual(matched['correlation_scope'], 'socket_endpoint')
        self.assertTrue(matched['local_address_checked'])
        self.assertEqual(matched['filter_runtime_id'], 110398)
        self.assertEqual(matched['filter_origin'], '{74C39319-E784-4A0F-82FC-B8966AC6B31B}')
        self.assertEqual(matched['interface_index'], 4)
        without_socket = wfp.event_match(event, replace(PING, local_port=None), PID, PATHS)
        self.assertEqual(without_socket['correlation_scope'], 'process_endpoint')

    def test_wfp_older_5152_matches_both_inbound_orientations(self):
        for source, destination, expected in [((REMOTE, 7777), (LOCAL, LOCAL_PORT), 'source'),
                                               ((LOCAL, LOCAL_PORT), (REMOTE, 7777), 'destination')]:
            with self.subTest(orientation=expected):
                event = wfp_event(5152, version=0, Direction='%%14592', SourceAddress=source[0], SourcePort=source[1],
                                  DestAddress=destination[0], DestPort=destination[1])
                match = wfp.event_match(event, PING, PID, PATHS)
                self.assertEqual(match['event_id'], 5152)
                self.assertEqual(match['remote_endpoint_field'], expected)
                self.assertIsNone(match['filter_origin'])
                self.assertIsNone(match['interface_index'])
                self.assertIsNone(wfp.event_match(event, replace(PING, directions=('outbound',)), PID, PATHS))

    def test_wfp_ignores_system_execution_pid_and_unrelated_traffic(self):
        cases = [{'ProcessID': 999}, {'Application': r'C:\Other\lush_backend.exe'}, {'Protocol': 6},
                 {'DestPort': 7778}, {'SourcePort': LOCAL_PORT + 1}, {'DestAddress': '192.0.2.18'},
                 {'timestamp': '2026-11-22T18:52:13.000Z'}, {'timestamp': '2026-11-22T18:52:14.327'}]
        for fields in cases:
            with self.subTest(fields=fields):
                self.assertIsNone(wfp.event_match(wfp_event(**fields), PING, PID, PATHS))
        self.assertIsNone(wfp.event_match(wfp_event(ProcessID=','.join(('4556', '4'))), PING, 4, PATHS))

    def test_defender_uses_detection_time_and_full_url_with_delayed_publication(self):
        event = defender_event(**{'Destination': f'http://{REMOTE}:80/status?query=ignored'})
        result = wfp.defender_match(event, HTTP, PID, PATHS)
        self.assertEqual(result['event_at'], '2026-11-22T18:52:14.327Z')
        self.assertEqual(result['published_at'], '2026-11-22T18:52:15.452631Z')
        self.assertEqual(result['timestamp_source'], 'detection_time')
        self.assertEqual(result['http_path'], '/status')
        self.assertEqual(result['attribution'], 'strong')
        self.assertIsNone(result['pid'])  # System.Execution is not the affected process.
        self.assertNotIn('query', result)
        self.assertIsNone(wfp.defender_match(event, PING, PID, PATHS))

    def test_defender_weak_and_unrelated_are_distinct(self):
        for fields in [{'Process Name': 'lush_backend.exe'}, {'Destination': REMOTE},
                       {'Destination': f'http://{REMOTE}'}]:
            with self.subTest(fields=fields):
                match = wfp.defender_match(defender_event(**fields), HTTP, PID, PATHS)
                self.assertEqual(match['attribution'], 'weak')
        missing_detection = defender_event(timestamp=STAMP, **{'Detection Time': ''})
        self.assertEqual(wfp.defender_match(missing_detection, HTTP, PID, PATHS)['attribution'], 'weak')
        for fields in [{'Process Name': 'firefox.exe'}, {'Process Name': r'C:\Other\lush_backend.exe'},
                       {'Destination': f'http://{REMOTE}/diagnostics'}, {'Destination': f'http://{REMOTE}:81/status'},
                       {'Destination': f'https://{REMOTE}/status'}, {'Destination': 'http://unrelated.example/status'},
                       {'Detection Time': '2026-11-22T18:52:12.000Z'}, {'Detection Time': 'not a timestamp'}]:
            with self.subTest(fields=fields):
                self.assertIsNone(wfp.defender_match(defender_event(**fields), HTTP, PID, PATHS))


class WindowsQueryTests(unittest.TestCase):
    def setUp(self):
        self.log = mock.Mock(spec=logging.Logger)
        self.collector = wfp.WfpDiagnostics(self.log)
        self.reader = mock.Mock(spec=wfp.SecurityEvents)

    def test_distinguishes_strong_defender_block_audit_and_weak_observation(self):
        cases = [(defender_event(1126), self.log.warning, 'windows_defender_block'),
                 (defender_event(1125), self.log.info, 'windows_defender_audit'),
                 (defender_event(1126, **{'Process Name': 'lush_backend.exe'}), self.log.info,
                  'windows_defender_observation')]
        for xml, writer, code in cases:
            with self.subTest(code=code):
                self.collector.reported.clear()
                self.log.reset_mock()
                self.reader.read_defender.return_value = ([xml], None)
                self.collector.scan(self.reader, HTTP, PID, PATHS, wfp.DEFENDER_CHANNEL)
                extra = writer.call_args.kwargs['extra']
                self.assertEqual(extra['source'], 'windows')
                self.assertEqual(extra['code'], code)
                self.assertEqual('possible_causes' in extra, code == 'windows_defender_block')

    def test_dedup_preserves_evidence_and_incomplete_queries_are_visible(self):
        self.reader.read.return_value = ([wfp_event(), '<bad'], 'query_limit')
        for _ in range(2):
            self.collector.scan(self.reader, PING, PID, PATHS, wfp.SECURITY_CHANNEL)
        self.log.warning.assert_called_once()
        self.log.info.assert_called_once()
        evidence = self.log.warning.call_args.kwargs['extra']
        self.assertEqual(evidence['code'], 'windows_wfp_block')
        status = self.log.info.call_args.kwargs['extra']['details']
        self.assertEqual(status['query_status'], 'query_limit')

    def test_disabled_unknown_denied_and_timed_out_statuses_stay_distinct(self):
        for response, audit, expected in [(([], None), {'5152': 'disabled', '5157': 'disabled'}, 'audit_disabled'),
                                          (([], None), {'5152': 'access_denied', '5157': 'disabled'}, 'no_match'),
                                          (([], 'query_timeout'), {}, 'query_timeout'),
                                          ((['<bad'], None), {}, 'query_incomplete')]:
            with self.subTest(status=expected):
                self.reader.read.return_value = response
                self.collector.scan(self.reader, PING, PID, PATHS, wfp.SECURITY_CHANNEL, audit)
                self.assertEqual(self.log.info.call_args.kwargs['extra']['details']['query_status'], expected)
        for error, expected in [(5, 'access_denied'), (1314, 'access_denied'), (15007, 'channel_unavailable'),
                                (1460, 'query_timeout'), (1, 'query_failed')]:
            with self.subTest(winerror=error):
                self.reader.read.side_effect = ctypes.WinError(error)
                self.collector.scan(self.reader, PING, PID, PATHS, wfp.SECURITY_CHANNEL)
                details = self.log.info.call_args.kwargs['extra']['details']
                self.assertEqual((details['query_status'], details['winerror']), (expected, error))
        self.reader.read_defender.return_value = ([], None)
        self.reader.channel_enabled.return_value = False
        self.collector.scan(self.reader, HTTP, PID, PATHS, wfp.DEFENDER_CHANNEL)
        self.assertEqual(self.log.info.call_args.kwargs['extra']['details']['query_status'], 'channel_disabled')

    def test_native_query_is_bounded_closes_handles_and_uses_read_only_filters(self):
        reader = wfp.SecurityEvents.__new__(wfp.SecurityEvents)
        reader.handle = ctypes.c_void_p
        reader.dword = ctypes.c_uint32
        reader.query = mock.Mock(return_value=42)
        reader.close = mock.Mock()
        reader.event_xml = mock.Mock(side_effect=lambda handle: f'event-{handle}')

        def next_event(results, size, handles, timeout, flags, returned):
            handles[0] = 43
            return True

        reader.next = mock.Mock(side_effect=next_event)
        with mock.patch.object(wfp, 'MAX_EVENTS', 3), mock.patch.object(wfp.time, 'monotonic', return_value=10):
            self.assertEqual(reader.read(PING, threading.Event()), (['event-43'] * 3, 'query_limit'))
        _, channel, expression, flags = reader.query.call_args.args
        self.assertEqual(channel, 'Security')
        self.assertIn('EventID=5152 or EventID=5157', expression)
        self.assertIn(f'Data[@Name="SourceAddress"]="{REMOTE}"', expression)
        self.assertIn(f'Data[@Name="DestAddress"]="{REMOTE}"', expression)
        self.assertEqual(flags, 0x201)
        self.assertEqual(reader.close.call_args_list, [mock.call(43)] * 3 + [mock.call(42)])
        reader.next.side_effect = None
        reader.next.return_value = False
        with mock.patch.object(wfp.ctypes, 'get_last_error', return_value=1460):
            self.assertEqual(reader.read_defender(threading.Event()), ([], 'query_timeout'))
        self.assertIn('EventID=1125 or EventID=1126', reader.query.call_args.args[2])
        with mock.patch.object(wfp.ctypes, 'get_last_error', return_value=259):
            self.assertEqual(reader.read_defender(threading.Event()), ([], None))

    def test_submit_caps_pending_queries_and_keeps_ephemeral_port(self):
        with mock.patch.object(self.collector.thread, 'is_alive', return_value=True):
            for _ in range(wfp.MAX_PENDING + 4):
                self.collector.submit('UDP PING/PONG', f'::ffff:{REMOTE}', 7777, wfp.UDP, SECONDS,
                                      SECONDS + .5, local=('0.0.0.0', LOCAL_PORT))
        self.assertEqual(self.collector.pending.qsize(), wfp.MAX_PENDING)
        attempt = self.collector.pending.get_nowait()
        self.assertEqual((attempt.remote_ip, attempt.local_ip, attempt.local_port), (REMOTE, None, LOCAL_PORT))

    def test_udp_and_http_transport_hooks_target_the_original_operation(self):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = LOCAL, LOCAL_PORT
        sock.recvfrom.side_effect = TimeoutError()
        submit = mock.Mock()
        with mock.patch.object(backend.wfp_diagnostics, 'submit', submit), mock.patch.object(backend.socket, 'socket', return_value=sock):
            self.assertFalse(backend.ping_wemos(REMOTE, 7777))
        self.assertEqual(submit.call_args.args[:4], ('UDP PING/PONG', REMOTE, 7777, wfp.UDP))
        self.assertEqual(submit.call_args.kwargs['local'], (LOCAL, LOCAL_PORT))
        self.assertEqual(submit.call_args.kwargs['directions'], ('outbound', 'inbound'))
        submit.reset_mock()
        with mock.patch.object(backend.wfp_diagnostics, 'submit', submit), mock.patch.object(
                backend.urllib.request, 'urlopen', side_effect=urllib.error.URLError(TimeoutError())):
            self.assertFalse(backend.check_wemos_http(REMOTE))
        self.assertEqual(submit.call_args.args[:4], ('HTTP /status', REMOTE, 80, wfp.TCP))
        submit.reset_mock()
        collector = WemosDiagnostics(self.log, [], lambda: [], wfp_submit=submit)
        state = DevicePoll(probes={'/diagnostics': {}})
        with mock.patch('wemos_diagnostics.read_json', side_effect=urllib.error.URLError(TimeoutError())):
            with self.assertRaises(urllib.error.URLError):
                collector._read_probe(state, REMOTE, '/diagnostics', '/diagnostics?after=1')
        self.assertEqual(submit.call_args.args[:4], ('HTTP /diagnostics', REMOTE, 80, wfp.TCP))
        submit.reset_mock()
        with mock.patch('wemos_diagnostics.read_json', side_effect=urllib.error.HTTPError('http://example.test', 404, '', {}, None)):
            with self.assertRaises(urllib.error.HTTPError):
                collector._read_probe(state, REMOTE, '/diagnostics', '/diagnostics?after=1')
        submit.assert_not_called()
