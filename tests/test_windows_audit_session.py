"""Audit lifecycle checks without UAC, device access, or system audit writes."""

import base64
import ctypes
import os
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from ctypes import wintypes

from backend_fixture import backend
import windows_audit_session as audit
from windows_audit_helper import AUDIT_HELPER


class AuditSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = audit.WindowsAuditSession(mock.Mock())
        self.session.availability_expires = time.monotonic() + 60
        self.addCleanup(self.session.close)

    def test_read_only_status_keeps_channel_denial_and_audit_flags_distinct(self):
        reader = mock.Mock()
        denial = OSError('denied')
        denial.winerror = 5
        reader.read_channel.side_effect = [denial, ([], None)]
        reader.channel_enabled.return_value = True
        self.session.availability_expires = 0
        with mock.patch.object(audit, 'SecurityEvents', return_value=reader), \
             mock.patch.object(audit, 'query_wfp_audit_policy', return_value={'5152': 'enabled', '5157': 'enabled'}), \
             mock.patch.object(audit, 'AuditWatchdog') as watchdog:
            status = self.session.status()
        self.assertEqual(status['security_channel'], 'access_denied')
        self.assertEqual(status['defender_channel'], 'readable')
        self.assertEqual(status['system_audit_failure'], {'5152': 'enabled', '5157': 'enabled'})
        watchdog.assert_not_called()

    def test_duplicate_start_does_not_create_or_arm_second_watchdog(self):
        worker_started = threading.Event()
        release = threading.Event()

        def run(*args):
            worker_started.set()
            release.wait(2)

        with mock.patch.object(self.session, 'run', side_effect=run) as worker:
            try:
                self.assertEqual(self.session.start()['state'], 'starting')
                self.assertTrue(worker_started.wait(1))
                self.assertEqual(self.session.start()['state'], 'starting')
                self.assertEqual(self.session.stop()['state'], 'stopping')
                self.assertTrue(self.session.stop_requested.is_set())
                worker.assert_called_once_with(True, False)
            finally:
                release.set()
                self.session.thread.join(1)

    def test_elapsed_local_observation_requires_no_elevation(self):
        with mock.patch.object(audit, 'SESSION_SECONDS', 0), mock.patch.object(audit, 'AuditWatchdog') as watchdog:
            self.session.run(read_security=False, local_only=True)
        self.assertEqual((self.session.state, self.session.reason), ('restored', 'session_expired'))
        watchdog.assert_not_called()

    def test_already_enabled_and_readable_starts_local_only(self):
        self.session.availability = {
            'security_channel': 'readable', 'system_audit_failure': {'5152': 'enabled', '5157': 'enabled'},
        }
        with mock.patch.object(self.session, 'run') as worker:
            self.session.start()
            self.session.thread.join(1)
        worker.assert_called_once_with(False, True)

    def test_uac_cancellation_never_connects_or_arms_worker(self):
        with mock.patch.object(audit, 'AuditWatchdog') as watchdog:
            worker = watchdog.return_value.__enter__.return_value
            cancelled = OSError('cancelled')
            cancelled.winerror = 1223
            worker.launch.side_effect = cancelled
            self.session.run(read_security=True, local_only=False)
        self.assertEqual((self.session.state, self.session.reason), ('cancelled', 'uac_cancelled'))
        worker.connect.assert_not_called()
        worker.signal.assert_not_called()

    def test_stop_during_pending_consent_sends_disarm(self):
        with mock.patch.object(audit, 'AuditWatchdog') as watchdog:
            worker = watchdog.return_value.__enter__.return_value
            worker.launch.side_effect = lambda _: self.session.stop_requested.set()
            worker.read.return_value = b'result cancelled cancelled\n'
            self.session.run(read_security=True, local_only=False)
        worker.signal.assert_called_once_with(0)
        self.assertEqual((self.session.state, self.session.reason), ('cancelled', 'cancelled'))

    def test_unverified_watchdog_never_receives_arm(self):
        with mock.patch.object(audit, 'AuditWatchdog') as watchdog:
            worker = watchdog.return_value.__enter__.return_value
            worker.connect.return_value = False
            self.session.run(read_security=True, local_only=False)
        worker.signal.assert_not_called()
        self.assertEqual((self.session.state, self.session.reason), ('failed', 'startup_failed'))

    def test_receives_fragmented_prior_ready_proof_and_restore_result(self):
        worker = mock.Mock()
        worker.exit_code.return_value = None
        payload = base64.b64encode(b'<Event />')
        worker.read.side_effect = [b'prior 1 ', b'2\nready 1 5152\nevent ' + payload + b'\ntick limit\n',
                                   b'result restored stopped\n']
        with mock.patch.object(audit.time, 'sleep'):
            result = self.session.receive(worker)
        self.assertEqual(result, ('restored', 'stopped'))
        self.assertEqual(self.session.active_policy(), {'5152': 'enabled', '5157': 'enabled'})
        self.assertEqual(self.session.previous, {'5152': 'disabled', '5157': 'enabled'})
        self.assertEqual(self.session.changed, ['5152'])
        self.assertTrue(self.session.elevated_read)
        self.assertEqual(list(self.session.records), ['<Event />'])
        self.assertEqual(self.session.last_read_status, 'query_limit')

    def test_drains_terminal_message_already_written_when_worker_exits(self):
        worker = mock.Mock()
        worker.exit_code.return_value = 0
        worker.read.side_effect = [b'prior 0 0\nres', b'ult restored session_expired\n']
        self.assertEqual(self.session.receive(worker), ('restored', 'session_expired'))
        self.assertEqual(self.session.previous, {'5152': 'disabled', '5157': 'disabled'})
        worker.signal.assert_not_called()

    def test_watchdog_exit_conflict_and_unknown_termination_are_distinct(self):
        for code, expected in ((21, ('restore_conflict', 'policy_changed')),
                               (22, ('restore_failed', 'restore_failed')),
                               (123, ('restore_failed', 'restore_failed'))):
            with self.subTest(code=code):
                worker = mock.Mock()
                worker.read.return_value = None
                worker.exit_code.return_value = code
                self.assertEqual(self.session.receive(worker), expected)
                worker.signal.assert_called_once_with(0)

    def test_missing_watchdog_response_times_out_and_requests_restore(self):
        worker = mock.Mock()
        worker.exit_code.return_value = None
        with mock.patch.object(audit, 'SESSION_SECONDS', -21), mock.patch.object(audit.time, 'sleep'):
            self.assertEqual(self.session.receive(worker), ('restore_failed', 'restore_failed'))
        worker.signal.assert_called_once_with(0)

    def test_close_requests_stop_and_waits_for_worker(self):
        self.session.state = 'active'
        self.session.thread = mock.Mock()
        self.session.close()
        self.assertTrue(self.session.closed)
        self.assertTrue(self.session.stop_requested.is_set())
        self.assertEqual(self.session.state, 'stopping')
        self.session.thread.join.assert_called_once_with(timeout=2)


class AuditWatchdogTests(unittest.TestCase):
    def test_named_pipe_rejects_wrong_process_even_when_connected(self):
        worker = object.__new__(audit.AuditWatchdog)
        worker.api = mock.Mock()
        worker.api.ConnectNamedPipe.return_value = True
        worker.api.GetProcessId.return_value = 42
        worker.pipe = 100
        worker.process = 200

        def client_pid(_, destination):
            destination._obj.value = 41
            return True

        worker.api.GetNamedPipeClientProcessId.side_effect = client_pid
        self.assertFalse(worker.connect())
        worker.api.GetProcessId.return_value = 41
        self.assertTrue(worker.connect())


class WindowsDiagnosticRouteTests(unittest.TestCase):
    def handler(self, path, supplied=None):
        handler = object.__new__(backend.WebUIHandler)
        handler.path = path
        handler.headers = {} if supplied is None else {'X-LuxEdge-Windows-Diagnostics-Token': supplied}
        handler.send_error = mock.Mock()
        handler._safe_send_json = mock.Mock()
        return handler

    def test_exact_routes_require_token_and_send_only_fixed_actions(self):
        routes = (
            ('/api/windows-diagnostics', 'do_GET', 'status'),
            ('/api/windows-diagnostics/start', 'do_POST', 'start'),
            ('/api/windows-diagnostics/stop', 'do_POST', 'stop'),
        )
        for path, method, operation in routes:
            with self.subTest(path=path), mock.patch.object(backend, 'WINDOWS_DIAGNOSTICS_TOKEN', b'secret'), \
                 mock.patch.object(backend, 'windows_audit_session') as session:
                denied = self.handler(path, 'invalid')
                getattr(denied, method)()
                denied.send_error.assert_called_once_with(403)
                self.assertEqual(session.mock_calls, [])
                allowed = self.handler(path, 'secret')
                getattr(allowed, method)()
                action = getattr(session, operation)
                action.assert_called_once_with()
                allowed._safe_send_json.assert_called_once_with(action.return_value)

    def test_missing_bridge_token_and_route_suffix_fail_closed(self):
        with mock.patch.object(backend, 'WINDOWS_DIAGNOSTICS_TOKEN', b''), \
             mock.patch.object(backend, 'windows_audit_session') as session:
            request = self.handler('/api/windows-diagnostics/start')
            request.do_POST()
            request.send_error.assert_called_once_with(403)
            self.assertEqual(session.mock_calls, [])
        request = self.handler('/api/windows-diagnostics/start?command=arbitrary', 'secret')
        request.do_POST()
        request.send_error.assert_called_once_with(404)


POWERSHELL_POLICY_TEST = r"""
$ErrorActionPreference = 'Stop'
$text = [Console]::In.ReadToEnd().Replace('__SESSION_SECONDS__', '300')
$tokens = $null
$errors = $null
$parsed = [Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw $errors[0] }

function Import-TestFunction([string]$Name) {
    $function = $parsed.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $Name
    }, $false)
    if ($null -eq $function) { throw ('missing function ' + $Name) }
    return [ScriptBlock]::Create($function.Extent.Text)
}
function Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

# Load just the binding and a native read-only query. The privileged script body
# and its writer never enter the PowerShell execution path in this test.
. (Import-TestFunction 'Bind-Audit')
. (Import-TestFunction 'Get-Policy')
$guids = @{5152 = [Guid]'0cce9225-69ae-11d9-bed3-505054503030'}
$native = Bind-Audit
Check ($null -ne $native.GetMethod('AuditQuerySystemPolicy')) 'native query binding missing'
Check ($null -ne $native.GetMethod('AuditSetSystemPolicy')) 'native writer binding missing'
try {
    $mask = Get-Policy 5152
    Check ($mask -in 0, 1, 2, 3) 'invalid read-only native mask'
} catch {
    if ($_.Exception.Message -ne 'audit_snapshot_failed') { throw }
}

. (Import-TestFunction 'Apply-Audit')
. (Import-TestFunction 'Restore-Audit')

function Get-Policy([int]$Id) {
    return $testPolicy[$Id]
}
function Set-Policy([int]$Id, [int]$Value) {
    $testWrites.Add("$Id=$Value")
    if ($testFault -eq 'restore' -and $Id -eq 5157 -and $Value -eq 0) { throw 'mock restore failure' }
    $testPolicy[$Id] = $Value
    if ($testFault -eq 'apply' -and $Id -eq 5157 -and $Value -eq 2) { throw 'mock partial write failure' }
}
function Reset-Test([int]$Packet, [int]$Connection, [string]$Fault = '') {
    $script:testPolicy = @{5152 = $Packet; 5157 = $Connection}
    $script:testBefore = $testPolicy.Clone()
    $script:testChanged = @{}
    $script:testWrites = [Collections.Generic.List[string]]::new()
    $script:testFault = $Fault
    $script:testStop = [Threading.Tasks.TaskCompletionSource[int]]::new()
}

Reset-Test 2 1
Apply-Audit $testBefore $testChanged $testStop.Task
Check ($testPolicy[5152] -eq 2 -and $testPolicy[5157] -eq 3) 'unrequested or missing policy change'
Check (($testWrites -join ',') -eq '5157=3') 'previously enabled failure bit was rewritten'
Check ((Restore-Audit $testBefore $testChanged) -eq 'restored') 'restoring preserved success bit failed'
Check ($testPolicy[5157] -eq 1) 'prior success bit was not preserved'

Reset-Test 0 0 'apply'
$failure = $null
try { Apply-Audit $testBefore $testChanged $testStop.Task } catch { $failure = $_.Exception.Message }
Check ($failure -eq 'mock partial write failure') 'mock partial write did not fail'
Check ((Restore-Audit $testBefore $testChanged) -eq 'restored') 'failed partial write did not restore'
Check (($testWrites -join ',') -eq '5152=2,5157=2,5157=0,5152=0') 'partial rollback skipped a changed policy'

Reset-Test 1 0
Apply-Audit $testBefore $testChanged $testStop.Task
$testPolicy[5152] = 2
Check ((Restore-Audit $testBefore $testChanged) -eq 'restore_conflict') 'external change failed to signal conflict'
Check ($testPolicy[5152] -eq 2 -and $testPolicy[5157] -eq 0) 'conflicting policy was overwritten'

Reset-Test 0 0 'restore'
Apply-Audit $testBefore $testChanged $testStop.Task
Check ((Restore-Audit $testBefore $testChanged) -eq 'restore_failed') 'restore failure was not retained'
Check ($testPolicy[5152] -eq 0 -and $testPolicy[5157] -eq 2) 'failure prevented attempting the other restore'

Reset-Test 0 0
$testStop.SetResult(0)
$failure = $null
try { Apply-Audit $testBefore $testChanged $testStop.Task } catch { $failure = $_.Exception.Message }
Check ($failure -eq 'cancelled' -and $testWrites.Count -eq 0) 'cancelled session attempted a write'

Reset-Test 0 0
$testPolicy[5152] = 1
$failure = $null
try { Apply-Audit $testBefore $testChanged $testStop.Task } catch { $failure = $_.Exception.Message }
Check ($failure -eq 'policy_changed' -and $testWrites.Count -eq 0) 'apply overwrote a competing policy change'
"""


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell 5.1 required')
class PowerShellAuditPolicyTests(unittest.TestCase):
    def test_native_read_binding_and_isolated_policy_lifecycle(self):
        executable = Path(os.environ['SystemRoot']) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        encoded = base64.b64encode(POWERSHELL_POLICY_TEST.encode('utf-16-le')).decode('ascii')
        result = subprocess.run(
            [str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
            input=AUDIT_HELPER, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_native_private_pipe_authenticates_unprivileged_test_client(self):
        executable = Path(os.environ['SystemRoot']) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        with audit.AuditWatchdog() as worker:
            handle_ptr = ctypes.POINTER(wintypes.HANDLE)
            open_token = audit.win_function(worker.security, 'OpenProcessToken', wintypes.BOOL,
                                            wintypes.HANDLE, wintypes.DWORD, handle_ptr)
            get_info = audit.win_function(worker.security, 'GetTokenInformation', wintypes.BOOL, wintypes.HANDLE,
                                          ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
            token = wintypes.HANDLE()
            self.assertTrue(open_token(wintypes.HANDLE(-1), 0x8, ctypes.byref(token)))
            try:
                container, used = wintypes.DWORD(), wintypes.DWORD()
                self.assertTrue(get_info(token, 29, ctypes.byref(container), ctypes.sizeof(container),
                                         ctypes.byref(used)))  # TokenIsAppContainer
                if container.value:
                    self.skipTest('AppContainer requires package SID access outside the production pipe ACL')
            finally:
                worker.close_handle(token)
            source = """
$ErrorActionPreference = 'Stop'
$pipe = [IO.Pipes.NamedPipeClientStream]::new('.', '__PIPE__',
    [IO.Pipes.PipeDirection]::InOut, [IO.Pipes.PipeOptions]::Asynchronous,
    [Security.Principal.TokenImpersonationLevel]::Anonymous)
try {
    $pipe.Connect(3000)
    $command = $pipe.ReadByte()
    if ($command -ne 1) { throw 'Unexpected watchdog arm byte' }
    $response = [Text.Encoding]::ASCII.GetBytes('ack')
    $pipe.Write($response, 0, $response.Length)
} finally {
    $pipe.Dispose()
}
""".replace('__PIPE__', 'LuxEdgeWfp-' + worker.nonce)
            encoded = base64.b64encode(source.encode('utf-16-le')).decode('ascii')
            with subprocess.Popen(
                [str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
            ) as process:
                open_process = audit.win_function(worker.api, 'OpenProcess', wintypes.HANDLE, wintypes.DWORD,
                                                  wintypes.BOOL, wintypes.DWORD)
                worker.process = open_process(0x1000, False, process.pid)
                try:
                    self.assertTrue(worker.process)
                    if not worker.connect():
                        console, _ = process.communicate(timeout=4)
                        self.fail(console.decode(errors='replace'))
                    self.assertTrue(worker.signal(1))
                    output = b''
                    deadline = time.monotonic() + 3
                    while len(output) < 3 and time.monotonic() < deadline:
                        part = worker.read()
                        if part is None:
                            break
                        output += part
                        if not part:
                            time.sleep(0.02)
                    self.assertEqual(output, b'ack')
                    console, _ = process.communicate(timeout=2)
                    self.assertEqual(process.returncode, 0, console.decode(errors='replace'))
                finally:
                    if process.poll() is None:
                        process.kill()


if __name__ == '__main__':
    unittest.main()
