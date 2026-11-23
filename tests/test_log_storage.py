import io
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from backend_fixture import TEST_ROOT, backend
from diagnostic_logs import (
    DEFAULT_RETENTION, FILE_LIMIT, LIVE_EXPORT_LIMIT, MIN_RETENTION, MIB,
    RETENTION_FILE, DiagnosticLogHandler, managed_logs, prune_logs, read_retention,
)


class LogStorageTests(unittest.TestCase):
    def setUp(self):
        profile = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(profile.cleanup)
        self.root = Path(profile.name)
        self.directory = self.root / 'LuxEdge'
        self.directory.mkdir()
        self.handler = DiagnosticLogHandler(self.directory)
        self.addCleanup(self.handler.close)
        self.log = logging.Logger('storage-test')
        self.log.addHandler(self.handler)

    def put(self, name, size, modified):
        file = self.directory / name
        with file.open('wb') as stream:
            stream.truncate(size)
        os.utime(file, (modified, modified))
        return file

    def test_default_and_custom_limits_and_exact_retention_fields(self):
        config = self.directory / RETENTION_FILE
        self.assertEqual(read_retention(self.directory), DEFAULT_RETENTION)
        self.assertEqual(DEFAULT_RETENTION, 512 * MIB)
        for given, expected in [(16 * MIB, 16 * MIB), (MIN_RETENTION, MIN_RETENTION),
                                (True, DEFAULT_RETENTION), (MIN_RETENTION - 1, DEFAULT_RETENTION),
                                (9 * MIB + 1, DEFAULT_RETENTION)]:
            with self.subTest(given=given):
                config.write_text(json.dumps({'limit_bytes': given}), encoding='utf-8')
                self.assertEqual(read_retention(self.directory), expected)
        config.write_text('not json', encoding='utf-8')
        self.assertEqual(read_retention(self.directory), DEFAULT_RETENTION)

    def test_combined_limit_reserves_two_active_files_and_preserves_exports(self):
        active = [self.put('luxedge.jsonl', FILE_LIMIT, 20), self.put('luxedge-electron.jsonl', FILE_LIMIT, 21)]
        oldest = [self.put('luxedge-electron.jsonl.2', FILE_LIMIT, 3), self.put('luxedge.jsonl.1', FILE_LIMIT, 4)]
        retained = [self.put('luxedge.jsonl.seg-9000000-10000000-0011223344556677', FILE_LIMIT, 11),
                    self.put('luxedge-electron.jsonl.1', FILE_LIMIT, 12)]
        untouched = [self.put('luxedge.log', FILE_LIMIT, 1), self.put('luxedge.jsonl.99', FILE_LIMIT, 2),
                     self.put('luxedge-canli.txt', FILE_LIMIT, 1), self.put('luxedge.jsonl.custom', FILE_LIMIT, 1)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(prune_logs, self.directory, MIN_RETENTION) for _ in range(2)]
            for job in jobs:
                job.result(timeout=5)
        self.assertTrue(all(path.exists() for path in active + retained + untouched))
        self.assertTrue(all(not path.exists() for path in oldest))
        self.assertEqual(sum(entry[2].st_size for entry in managed_logs(self.directory)), MIN_RETENTION)

    def test_live_journal_paginates_8192_recent_rows_while_disk_is_unavailable(self):
        start = time.time_ns() // 1000
        with mock.patch.object(self.handler, '_persist', side_effect=OSError('test disk unavailable')):
            for index in range(LIVE_EXPORT_LIMIT + 2):
                self.log.info('live-%d', index, extra={'source': 'wemos'})
        self.assertFalse(self.handler.snapshot()['persistent'])
        self.assertEqual(len(self.handler.snapshot()['logs']), 1000)
        after = 0
        exported = []
        first = True
        while True:
            snapshot = self.handler.export_snapshot(start, 2**53 - 1, after)
            self.assertEqual(bool(snapshot['overflow']), first)
            first = False
            exported.extend(snapshot['logs'])
            if not snapshot['has_more']:
                break
            after = exported[-1]['id']
        self.assertEqual(len(exported), LIVE_EXPORT_LIMIT)
        self.assertEqual(exported[0]['msg'], 'live-2')
        self.assertEqual(exported[-1]['msg'], f'live-{LIVE_EXPORT_LIMIT + 1}')
        self.assertEqual([row['id'] for row in exported], sorted({row['id'] for row in exported}))

    def test_live_read_does_not_wait_for_log_file_writer(self):
        entered = threading.Event()
        release = threading.Event()

        def wait_for_disk(_):
            entered.set()
            release.wait(timeout=5)

        with mock.patch.object(self.handler, '_persist', side_effect=wait_for_disk), redirect_stdout(io.StringIO()):
            with ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(self.log.info, 'from Windows', extra={'source': 'windows'})
                try:
                    self.assertTrue(entered.wait(timeout=2))
                    reader = pool.submit(self.handler.export_snapshot, 1, 2**53 - 1, 0)
                    self.assertEqual(reader.result(timeout=2)['logs'][0]['msg'], 'from Windows')
                finally:
                    release.set()
                writer.result(timeout=2)

    def test_live_http_route_uses_private_token_and_disables_cors(self):
        route = object.__new__(backend.WebUIHandler)
        route.path = '/api/live-log-rows?since=10&through=20&after=0'
        route.send_error = mock.Mock()
        route._safe_send_json = mock.Mock()
        route.headers = {'X-LuxEdge-Live-Log-Token': 'incorrect'}
        with mock.patch.object(backend, 'LIVE_LOG_TOKEN', b'private-token'), mock.patch.object(
                backend, 'log_history', self.handler):
            route.serve_live_log_rows()
            route.send_error.assert_called_once_with(403)
            route._safe_send_json.assert_not_called()
            route.headers = {'X-LuxEdge-Live-Log-Token': 'private-token'}
            route.serve_live_log_rows()
        self.assertEqual(route._safe_send_json.call_args.kwargs, {'allow_origin': False})
        self.assertEqual(route._safe_send_json.call_args.args[0]['logs'], [])
