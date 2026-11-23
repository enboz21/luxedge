"""Consent-gated, time-bounded Windows WFP audit session and watchdog transport."""

import base64
import ctypes
import os
import secrets
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from pathlib import PureWindowsPath

from windows_audit_helper import AUDIT_HELPER
from windows_wfp import DEFENDER_CHANNEL, SECURITY_CHANNEL, SecurityEvents, query_wfp_audit_policy

SESSION_SECONDS = 300
RUNNING_STATES = ('starting', 'active', 'stopping')
AUDIT_IDS = ('5152', '5157')
MAX_LINE = 66_000
ACCESS_DENIED = (5, 1314)
TERMINAL_REASONS = {
    'session_expired', 'stopped', 'backend_exit', 'cancelled', 'audit_snapshot_failed', 'security_read_unavailable',
    'policy_busy', 'policy_changed', 'partial_apply_failed', 'restore_failed', 'startup_failed',
}


class PipeSecurityAttributes(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', wintypes.BOOL)]


class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ('size', wintypes.DWORD), ('mask', wintypes.ULONG), ('window', wintypes.HWND),
        ('verb', wintypes.LPCWSTR), ('file', wintypes.LPCWSTR), ('parameters', wintypes.LPCWSTR),
        ('directory', wintypes.LPCWSTR), ('show', ctypes.c_int), ('instance', wintypes.HINSTANCE),
        ('id_list', ctypes.c_void_p), ('class_name', wintypes.LPCWSTR), ('class_key', wintypes.HKEY),
        ('hot_key', wintypes.DWORD), ('icon', wintypes.HANDLE), ('process', wintypes.HANDLE),
    ]


def win_function(library, name, result, *parameters):
    function = getattr(library, name)
    function.argtypes = parameters
    function.restype = result
    return function


class AuditWatchdog:
    """Private pipe, authenticated by the Windows PID of the launched UAC process."""

    def __init__(self):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.shell = ctypes.WinDLL('shell32', use_last_error=True)
        self.security = ctypes.WinDLL('advapi32', use_last_error=True)
        self.nonce = secrets.token_hex(16)
        self.pipe = self.process = None
        self.close_handle = win_function(self.api, 'CloseHandle', wintypes.BOOL, wintypes.HANDLE)

    def current_user_sid(self, release):
        handle_ptr = ctypes.POINTER(wintypes.HANDLE)
        open_token = win_function(self.security, 'OpenProcessToken', wintypes.BOOL,
                                  wintypes.HANDLE, wintypes.DWORD, handle_ptr)
        get_info = win_function(self.security, 'GetTokenInformation', wintypes.BOOL, wintypes.HANDLE,
                                ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        to_string = win_function(self.security, 'ConvertSidToStringSidW', wintypes.BOOL,
                                 ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
        token = wintypes.HANDLE()
        if not open_token(wintypes.HANDLE(-1), 0x8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            required = wintypes.DWORD()
            get_info(token, 1, None, 0, ctypes.byref(required))  # TokenUser
            if not required.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(required.value)
            if not get_info(token, 1, buffer, len(buffer), ctypes.byref(required)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid = wintypes.LPWSTR()
            if not to_string(ctypes.c_void_p.from_buffer(buffer), ctypes.byref(sid)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return sid.value
            finally:
                release(sid)
        finally:
            self.close_handle(token)

    def __enter__(self):
        # Permit only this user, built-in administrators, and SYSTEM.
        descriptor = ctypes.c_void_p()
        convert = win_function(self.security, 'ConvertStringSecurityDescriptorToSecurityDescriptorW',
                               wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD,
                               ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
        release = win_function(self.api, 'LocalFree', ctypes.c_void_p, ctypes.c_void_p)
        user_sid = self.current_user_sid(release)
        rules = f'D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;{user_sid})'
        if not convert(rules, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes = PipeSecurityAttributes(ctypes.sizeof(PipeSecurityAttributes), descriptor, False)
            create_pipe = win_function(self.api, 'CreateNamedPipeW', wintypes.HANDLE, wintypes.LPCWSTR,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PipeSecurityAttributes))
            pipe = create_pipe(r'\\.\pipe\LuxEdgeWfp-' + self.nonce, 0x80003, 0x9, 1, 4096, 131_072, 0,
                               ctypes.byref(attributes))  # first instance, duplex, nonblocking, local only
            if pipe == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            self.pipe = pipe
        finally:
            release(descriptor)
        return self

    def __exit__(self, *_):
        if self.pipe:
            self.close_handle(self.pipe)
        if self.process:
            self.close_handle(self.process)

    def launch(self, read_security):
        get_system = win_function(self.api, 'GetSystemDirectoryW', wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
        buffer = ctypes.create_unicode_buffer(32_768)
        length = get_system(buffer, len(buffer))
        if not 0 < length < len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        directory = PureWindowsPath(buffer.value)
        powershell = directory / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        script = (AUDIT_HELPER.replace('__NONCE__', self.nonce)
                  .replace('__BACKEND_PID__', str(os.getpid()))
                  .replace('__READ_SECURITY__', '$true' if read_security else '$false')
                  .replace('__SESSION_SECONDS__', str(SESSION_SECONDS)))
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        info = ShellExecuteInfo(size=ctypes.sizeof(ShellExecuteInfo), mask=0x140, verb='runas',
                                file=str(powershell), directory=str(directory), show=0,
                                parameters='-NoProfile -NonInteractive -EncodedCommand ' + encoded)
        launch = win_function(self.shell, 'ShellExecuteExW', wintypes.BOOL, ctypes.POINTER(ShellExecuteInfo))
        ole = ctypes.WinDLL('ole32')
        initialize = win_function(ole, 'CoInitializeEx', ctypes.c_long, ctypes.c_void_p, wintypes.DWORD)
        uninitialize = win_function(ole, 'CoUninitialize', None)
        if initialize(None, 0x6) < 0:  # STA with OLE 1 DDE disabled
            raise OSError('Shell COM initialization failed')
        try:
            if not launch(ctypes.byref(info)) or not info.process:
                raise ctypes.WinError(ctypes.get_last_error())
            self.process = info.process
        finally:
            uninitialize()

    def connect(self):
        connect = win_function(self.api, 'ConnectNamedPipe', wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p)
        get_client = win_function(self.api, 'GetNamedPipeClientProcessId', wintypes.BOOL,
                                  wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
        get_pid = win_function(self.api, 'GetProcessId', wintypes.DWORD, wintypes.HANDLE)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            connected = connect(self.pipe, None)
            error = ctypes.get_last_error()
            if connected or error == 535:  # ERROR_PIPE_CONNECTED
                pid = wintypes.ULONG()
                if get_client(self.pipe, ctypes.byref(pid)) and pid.value:
                    return pid.value == get_pid(self.process)
                # The first nonblocking ConnectNamedPipe call can succeed before any client arrives.
                error = ctypes.get_last_error()
                if connected:
                    error = 536
            if error not in (536, 232, 233) or self.exit_code() is not None:
                break
            time.sleep(0.05)
        return False

    def signal(self, value):
        write = win_function(self.api, 'WriteFile', wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        written = wintypes.DWORD()
        data = ctypes.create_string_buffer(bytes([value]))
        return bool(write(self.pipe, data, 1, ctypes.byref(written), None) and written.value == 1)

    def read(self):
        read = win_function(self.api, 'ReadFile', wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p,
                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        buffer = ctypes.create_string_buffer(8192)
        count = wintypes.DWORD()
        if read(self.pipe, buffer, len(buffer), ctypes.byref(count), None):
            return buffer.raw[:count.value]
        if ctypes.get_last_error() == 232:  # pipe connected, no bytes yet
            return b''
        return None

    def exit_code(self):
        get = win_function(self.api, 'GetExitCodeProcess', wintypes.BOOL, wintypes.HANDLE,
                           ctypes.POINTER(wintypes.DWORD))
        code = wintypes.DWORD()
        if get(self.process, ctypes.byref(code)) and code.value != 259:
            return code.value
        return None


class WindowsAuditSession:
    def __init__(self, log):
        self.log = log
        self.lock = threading.Condition()
        self.state = 'idle' if sys.platform == 'win32' else 'unsupported'
        self.reason = None
        self.deadline = None
        self.previous = {}
        self.changed = []
        self.elevated_read = False
        self.stop_requested = threading.Event()
        self.thread = None
        self.closed = False
        self.records = deque(maxlen=256)
        self.last_read = 0.0
        self.last_read_status = None
        self.availability = {}
        self.availability_expires = 0

    def status(self):
        if sys.platform == 'win32' and time.monotonic() >= self.availability_expires:
            self.refresh_availability()
        with self.lock:
            return self.snapshot()

    def snapshot(self):
        remaining = None if self.deadline is None else max(0, round(self.deadline - time.monotonic()))
        current_audit = self.active_policy() or self.availability.get('system_audit_failure', {})
        return {
            'state': self.state, 'reason': self.reason, 'session_seconds': SESSION_SECONDS,
            'remaining_seconds': remaining, 'security_channel': self.availability.get('security_channel', 'unavailable'),
            'defender_channel': self.availability.get('defender_channel', 'unavailable'),
            'system_audit_failure': current_audit, 'previous_audit_failure': dict(self.previous),
            'changed_audit_failure': list(self.changed), 'elevated_security_read': self.elevated_read,
        }

    def active_policy(self):
        with self.lock:
            if self.state == 'active' and self.previous:
                return {**self.previous, **dict.fromkeys(self.changed, 'enabled')}
            return None

    def refresh_availability(self):
        result = {}
        checking = threading.Event()
        try:
            reader = SecurityEvents()
            for key, channel in (('security_channel', SECURITY_CHANNEL), ('defender_channel', DEFENDER_CHANNEL)):
                try:
                    _, status = reader.read_channel(channel, '*[System[EventID=0]]', checking)
                    if status is None:
                        result[key] = 'disabled' if reader.channel_enabled(channel) is False else 'readable'
                    else:
                        result[key] = 'unavailable'
                except OSError as error:
                    result[key] = 'access_denied' if getattr(error, 'winerror', None) in ACCESS_DENIED else 'unavailable'
        except (OSError, AttributeError):
            pass
        try:
            result['system_audit_failure'] = query_wfp_audit_policy()
        except (OSError, AttributeError):
            result['system_audit_failure'] = dict.fromkeys(AUDIT_IDS, 'unavailable')
        with self.lock:
            self.availability = result
            self.availability_expires = time.monotonic() + 5

    def start(self):
        self.status()
        with self.lock:
            if self.closed or self.state == 'unsupported' or self.state in RUNNING_STATES:
                return self.snapshot()
            self.stop_requested.clear()
            self.state = 'starting'
            self.reason = None
            self.previous = {}
            self.changed = []
            self.records.clear()
            self.last_read = 0
            self.last_read_status = None
            self.deadline = None
            read_security = self.availability.get('security_channel') != 'readable'
            audit = self.availability.get('system_audit_failure', {})
            local_only = not read_security and all(audit.get(event_id) == 'enabled' for event_id in AUDIT_IDS)
            self.thread = threading.Thread(target=self.run, args=(read_security, local_only),
                                           name='windows-audit-session', daemon=True)
            self.thread.start()
            return self.snapshot()

    def stop(self):
        with self.lock:
            if self.state in ('starting', 'active'):
                self.state = 'stopping'
                self.stop_requested.set()
                self.lock.notify_all()
            return self.snapshot()

    def close(self):
        with self.lock:
            self.closed = True
        self.stop()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def read_events(self, stopped):
        with self.lock:
            if not self.elevated_read or self.state != 'active':
                return None
            last_read = self.last_read
            if not stopped.is_set():
                self.lock.wait_for(lambda: self.last_read != last_read or self.state != 'active', 0.8)
            status = self.last_read_status if self.last_read != last_read else 'query_timeout'
            return list(self.records), status

    def set_state(self, state, reason=None):
        with self.lock:
            if state == 'active' and self.stop_requested.is_set():
                return
            self.state = state
            self.reason = reason
            self.availability_expires = 0
            if state == 'active':
                self.deadline = time.monotonic() + SESSION_SECONDS
            else:
                self.deadline = None
                self.elevated_read = False
            self.lock.notify_all()
            evidence = self.snapshot()
        level = self.log.warning if state in ('restore_conflict', 'restore_failed') else self.log.info
        message = {
            'active': 'Geçici Windows denetim oturumu başladı',
            'restored': 'Geçici Windows denetim oturumu sona erdi',
            'cancelled': 'Geçici Windows denetim oturumu iptal edildi',
            'restore_conflict': 'Windows denetim ilkesi oturum sırasında değişti',
            'restore_failed': 'Önceki Windows denetim ilkesi doğrulanamadı',
            'failed': 'Geçici Windows denetim oturumu başlatılamadı',
        }[state]
        level(message, extra={'source': 'windows', 'code': 'windows_audit_session', 'details': evidence})

    def run(self, read_security, local_only):
        if local_only:
            with self.lock:
                self.previous = dict(self.availability.get('system_audit_failure', {}))
            self.set_state('active')
            requested = self.stop_requested.wait(SESSION_SECONDS)
            self.set_state('restored', 'stopped' if requested else 'session_expired')
            return
        try:
            with AuditWatchdog() as watchdog:
                if self.stop_requested.is_set():
                    self.set_state('cancelled', 'cancelled')
                    return
                watchdog.launch(read_security)
                if not watchdog.connect():
                    self.set_state('failed', 'startup_failed')
                    return
                arm = 0 if self.stop_requested.is_set() else 1
                if not watchdog.signal(arm):
                    self.set_state('failed', 'startup_failed')
                    return
                result = self.receive(watchdog, stop_sent=not arm)
            self.set_state(*result)
        except (OSError, AttributeError) as error:
            if getattr(error, 'winerror', None) == 1223:
                self.set_state('cancelled', 'uac_cancelled')
            else:
                self.set_state('failed', 'startup_failed')

    def receive(self, watchdog, *, stop_sent=False):
        buffer = b''
        deadline = time.monotonic() + SESSION_SECONDS + 20
        while time.monotonic() < deadline:
            if self.stop_requested.is_set() and not stop_sent:
                stop_sent = watchdog.signal(0)
            data = watchdog.read()
            if data:
                buffer += data
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if len(line) <= MAX_LINE:
                        outcome = self.handle_line(line)
                        if outcome is not None:
                            return outcome
                if len(buffer) > MAX_LINE:
                    break
            if data is None or (not data and watchdog.exit_code() is not None):
                break
            if not data:
                time.sleep(0.05)
        if not stop_sent:
            watchdog.signal(0)
        for _ in range(20):
            code = watchdog.exit_code()
            if code is not None:
                return {
                    0: ('restored', 'stopped'),
                    21: ('restore_conflict', 'policy_changed'),
                    22: ('restore_failed', 'restore_failed'),
                    23: ('cancelled', 'cancelled'),
                    24: ('failed', 'startup_failed'),
                }.get(code, ('restore_failed', 'restore_failed'))
            time.sleep(0.05)
        return 'restore_failed', 'restore_failed'

    def handle_line(self, line):
        fields = line.split(b' ', 2)
        if fields[0] == b'prior' and len(fields) == 3 and all(mask in b'0123' and len(mask) == 1 for mask in fields[1:]):
            with self.lock:
                self.previous = {event_id: 'enabled' if int(mask) & 2 else 'disabled'
                                 for event_id, mask in zip(AUDIT_IDS, fields[1:])}
        elif fields[0] == b'ready' and len(fields) == 3:
            with self.lock:
                self.elevated_read = fields[1] == b'1'
                self.changed = [event_id for event_id in AUDIT_IDS if event_id.encode() in fields[2].split(b',')]
            self.set_state('active')
        elif fields[0] == b'event' and len(fields) == 2:
            try:
                xml = base64.b64decode(fields[1], validate=True).decode('utf-8')
            except (ValueError, UnicodeError):
                return None
            with self.lock:
                self.records.append(xml)
        elif fields[0] == b'tick':
            with self.lock:
                self.last_read = time.monotonic()
                self.last_read_status = 'query_limit' if len(fields) == 2 and fields[1] == b'limit' else None
                self.lock.notify_all()
        elif fields[0] == b'result' and len(fields) == 3:
            state = fields[1].decode('ascii', errors='replace')
            reason = fields[2].decode('ascii', errors='replace')
            if state in ('restored', 'cancelled', 'failed', 'restore_conflict', 'restore_failed') and reason in TERMINAL_REASONS:
                return state, reason
        return None
