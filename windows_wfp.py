"""Read-only Windows network security evidence for failed Wemos traffic."""

import ctypes
import ipaddress
import ntpath
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone


MAX_PENDING = 32
MAX_EVENTS = 64
RECENT_MS = 30_000
EVENT_GRACE_SECONDS = 0.5
DEFENDER_QUERY_GRACE_SECONDS = 1.5
QUERY_DEADLINE_SECONDS = 0.75
RECHECK_SECONDS = 5
EVENT_NAMESPACE = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
EVENT_TIME_FRACTION = re.compile(r'\.([0-9]+)(?=Z$|[+-][0-9]{2}:[0-9]{2}$)')
WFP_PROVIDER = 'Microsoft-Windows-Security-Auditing'
DEFENDER_PROVIDER = 'Microsoft-Windows-Windows Defender'
SECURITY_CHANNEL = 'Security'
DEFENDER_CHANNEL = DEFENDER_PROVIDER + '/Operational'
WFP_AUDIT_GUIDS = {
    5152: uuid.UUID('0cce9225-69ae-11d9-bed3-505054503030'),
    5157: uuid.UUID('0cce9226-69ae-11d9-bed3-505054503030'),
}
POLICY_AUDIT_EVENT_FAILURE = 0x2
POLICY_REFRESH_SECONDS = 30
TCP = 6
UDP = 17


def transport_error(error):
    """HTTP error responses already crossed the network."""
    reason = getattr(error, 'reason', error)
    return not isinstance(error, urllib.error.HTTPError) and isinstance(reason, OSError)


def address(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def number(value):
    try:
        return int(value, 16 if value.lower().startswith('0x') else 10)
    except (AttributeError, ValueError):
        return None


def path_key(path):
    if path.startswith(('\\\\?\\', '\\??\\')):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))


def parse_event_time(timestamp):
    # Python 3.10 accepts microseconds, while WinEvt can emit longer fractions.
    timestamp = EVENT_TIME_FRACTION.sub(lambda part: '.' + part[1][:6].ljust(6, '0'), timestamp)
    if timestamp.endswith('Z'):
        timestamp = timestamp[:-1] + '+00:00'
    return datetime.fromisoformat(timestamp)


def executable_paths():
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    query_device = kernel32.QueryDosDeviceW
    query_device.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    query_device.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32_768)
    paths = set()
    for executable in (sys.executable, os.path.realpath(sys.executable)):
        executable = path_key(executable)
        paths.add(executable)
        drive, suffix = ntpath.splitdrive(executable)
        if len(drive) == 2 and drive.endswith(':') and query_device(drive, buffer, len(buffer)):
            paths.add(path_key(buffer.value + suffix))
    return paths


def query_wfp_audit_policy():
    """Read system-level failure audit bits without adjusting token privileges."""
    from ctypes import wintypes

    class Guid(ctypes.Structure):
        _fields_ = [('data1', ctypes.c_uint32), ('data2', ctypes.c_uint16),
                    ('data3', ctypes.c_uint16), ('data4', ctypes.c_uint8 * 8)]

    class AuditPolicyInformation(ctypes.Structure):
        _fields_ = [('subcategory', Guid), ('flags', wintypes.ULONG), ('category', Guid)]

    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    info_pointer = ctypes.POINTER(AuditPolicyInformation)
    query = advapi.AuditQuerySystemPolicy
    query.argtypes = (ctypes.POINTER(Guid), wintypes.ULONG, ctypes.POINTER(info_pointer))
    query.restype = ctypes.c_ubyte
    free = advapi.AuditFree
    free.argtypes = (ctypes.c_void_p,)
    free.restype = None
    result = {}
    for event_id, guid in WFP_AUDIT_GUIDS.items():
        subcategory = Guid.from_buffer_copy(guid.bytes_le)
        policy = info_pointer()
        if not query(ctypes.byref(subcategory), 1, ctypes.byref(policy)):
            code = ctypes.get_last_error()
            result[str(event_id)] = 'access_denied' if code in (5, 1314) else 'unavailable'
            continue
        if not policy:
            result[str(event_id)] = 'unavailable'
            continue
        try:
            enabled = bool(policy.contents.flags & POLICY_AUDIT_EVENT_FAILURE)
            result[str(event_id)] = 'enabled' if enabled else 'disabled'
        finally:
            free(policy)
    return result


@dataclass(frozen=True)
class Attempt:
    operation: str
    remote_ip: str
    remote_port: int
    protocol: int
    started: float
    finished: float
    directions: tuple
    local_ip: str | None
    local_port: int | None

    @property
    def key(self):
        return self.operation, self.remote_ip, self.remote_port


def event_seconds(timestamp):
    try:
        event_at = parse_event_time(timestamp)
        return event_at.timestamp() if event_at.tzinfo is not None else None
    except (ValueError, OSError, OverflowError):
        return None


def in_attempt(timestamp, attempt):
    seconds = event_seconds(timestamp)
    return seconds is not None and attempt.started - EVENT_GRACE_SECONDS <= seconds <= attempt.finished + EVENT_GRACE_SECONDS


def event_fields(xml, provider_name, event_ids):
    root = ET.fromstring(xml)
    system = root.find('e:System', EVENT_NAMESPACE)
    if system is None:
        return None
    provider = system.find('e:Provider', EVENT_NAMESPACE)
    event_time = system.find('e:TimeCreated', EVENT_NAMESPACE)
    event_id = number(system.findtext('e:EventID', '', EVENT_NAMESPACE))
    record_id = number(system.findtext('e:EventRecordID', '', EVENT_NAMESPACE))
    if (provider is None or provider.get('Name') != provider_name or event_time is None
            or event_id not in event_ids or record_id is None):
        return None
    timestamp = event_time.get('SystemTime', '')
    if event_seconds(timestamp) is None:
        return None
    fields = {field.get('Name', '').casefold(): (field.text or '').strip()
              for field in root.findall('e:EventData/e:Data', EVENT_NAMESPACE)}
    return {'event_id': event_id, 'event_record_id': record_id, 'event_at': timestamp}, fields


def event_match(xml, attempt, pid, paths):
    parsed = event_fields(xml, WFP_PROVIDER, WFP_AUDIT_GUIDS)
    if parsed is None:
        return None
    event, fields = parsed
    if not in_attempt(event['event_at'], attempt):
        return None
    application = fields.get('application', '')
    if (not application or number(fields.get('processid')) != pid or path_key(application) not in paths
            or number(fields.get('protocol')) != attempt.protocol):
        return None
    direction = {'%%14592': 'inbound', '%%14593': 'outbound'}.get(fields.get('direction'))
    if direction not in attempt.directions:
        return None
    source = address(fields.get('sourceaddress')), number(fields.get('sourceport'))
    destination = address(fields.get('destaddress')), number(fields.get('destport'))
    if None in source or None in destination:
        return None
    # Inbound events can put the local receiving endpoint in SourceAddress.
    endpoints = ((source, destination, 'destination'),)
    if direction == 'inbound':
        endpoints = ((destination, source, 'source'), *endpoints)
    match = next(((local, remote_field) for local, remote, remote_field in endpoints
                  if remote == (attempt.remote_ip, attempt.remote_port)
                  and (attempt.local_ip is None or local[0] == attempt.local_ip)
                  and (attempt.local_port is None or local[1] == attempt.local_port)), None)
    if match is None:
        return None
    local, remote_field = match
    return {
        **event,
        'pid': pid, 'application': application, 'protocol': 'TCP' if attempt.protocol == TCP else 'UDP',
        'direction': direction, 'remote_endpoint_field': remote_field,
        'local_ip': local[0], 'local_port': local[1],
        'local_address_checked': attempt.local_ip is not None,
        'local_port_checked': attempt.local_port is not None,
        'attribution': 'strong',
        'correlation_scope': 'socket_endpoint' if attempt.local_port is not None else 'process_endpoint',
        'filter_runtime_id': number(fields.get('filterrtid')),
        'filter_origin': (fields.get('filterorigin') or '')[:256] or None,
        'interface_index': number(fields.get('interfaceindex')),
    }


def defender_match(xml, attempt, pid, paths):
    if attempt.protocol != TCP or 'outbound' not in attempt.directions:
        return None
    parsed = event_fields(xml, DEFENDER_PROVIDER, (1125, 1126))
    if parsed is None:
        return None
    event, fields = parsed
    fields = {name.replace(' ', ''): value for name, value in fields.items()}
    detection_time = fields.get('detectiontime')
    published_at = event['event_at']
    evidence_at = detection_time or published_at
    if not in_attempt(evidence_at, attempt):
        return None
    publication_delay = event_seconds(published_at) - event_seconds(evidence_at)
    if not -EVENT_GRACE_SECONDS <= publication_delay <= RECENT_MS / 1000:
        return None
    process = fields.get('processname') or fields.get('application') or ''
    target_pid = fields.get('processid')
    if target_pid and number(target_pid) != pid:
        return None
    full_path = path_key(process) in paths
    matching_basename = path_key(process) in {ntpath.basename(path) for path in paths}
    if not process or not (full_path or matching_basename):
        return None
    if not target_pid and ntpath.basename(path_key(process)) in ('python.exe', 'pythonw.exe'):
        return None
    destination = fields.get('destination') or fields.get('url') or fields.get('path') or fields.get('remoteaddress')
    if not destination:
        return None
    try:
        literal_ip = address(destination)
        authority = f'[{destination}]' if literal_ip is not None and ':' in destination else destination
        endpoint = urllib.parse.urlsplit(authority if '://' in authority else '//' + authority)
        if endpoint.username is not None or endpoint.scheme not in ('', 'http'):
            return None
        port = endpoint.port
        if port is None:
            port = 80 if endpoint.scheme == 'http' else number(fields.get('remoteport') or fields.get('destinationport'))
    except ValueError:
        return None
    port_matches = port == attempt.remote_port or port is None and attempt.remote_port == 80
    if (address(endpoint.hostname) != attempt.remote_ip or not port_matches
            or fields.get('protocol', '').casefold() not in ('', 'tcp', '6')):
        return None
    path = endpoint.path
    if path not in ('', '/', attempt.operation.removeprefix('HTTP ')):
        return None
    exact_path = path not in ('', '/')
    process_identity = 'executable_path' if full_path else 'pid_and_executable_name' if target_pid else 'executable_name'
    strong = (full_path or target_pid) and exact_path and port is not None and (detection_time or target_pid)
    return {
        **event, 'event_at': evidence_at, 'published_at': published_at,
        'timestamp_source': 'detection_time' if detection_time else 'time_created',
        'application': process, 'pid': pid if target_pid else None, 'protocol': 'TCP',
        'direction': 'outbound', 'http_path': path if exact_path else None, 'remote_port_checked': port is not None,
        'attribution': 'strong' if strong else 'weak', 'process_identity': process_identity,
        'defender_mode': 'block' if event['event_id'] == 1126 else 'audit',
    }


class SecurityEvents:
    """Small WinEvt binding with numeric errors independent of Windows language."""

    def __init__(self):
        from ctypes import wintypes

        api = ctypes.WinDLL('wevtapi', use_last_error=True)
        handle = wintypes.HANDLE
        dword = wintypes.DWORD
        dword_ptr = ctypes.POINTER(dword)
        self.handle = handle
        self.dword = dword
        self.query = api.EvtQuery
        self.query.argtypes = (handle, wintypes.LPCWSTR, wintypes.LPCWSTR, dword)
        self.query.restype = handle
        self.next = api.EvtNext
        self.next.argtypes = (handle, dword, ctypes.POINTER(handle), dword, dword, dword_ptr)
        self.next.restype = wintypes.BOOL
        self.render = api.EvtRender
        self.render.argtypes = (handle, handle, dword, dword, ctypes.c_void_p, dword_ptr, dword_ptr)
        self.render.restype = wintypes.BOOL
        self.close = api.EvtClose
        self.close.argtypes = (handle,)
        self.close.restype = wintypes.BOOL
        self.open_config = api.EvtOpenChannelConfig
        self.open_config.argtypes = (handle, wintypes.LPCWSTR, dword)
        self.open_config.restype = handle
        self.get_config = api.EvtGetChannelConfigProperty
        self.get_config.argtypes = (handle, dword, dword, dword, ctypes.c_void_p, dword_ptr)
        self.get_config.restype = wintypes.BOOL

    def channel_enabled(self, channel):
        class BooleanVariant(ctypes.Structure):
            _fields_ = [('value', ctypes.c_uint32), ('reserved', ctypes.c_uint32),
                        ('count', self.dword), ('kind', self.dword)]

        config = self.open_config(None, channel, 0)
        if not config:
            return None
        try:
            value = BooleanVariant()
            used = self.dword()
            if self.get_config(config, 0, 0, ctypes.sizeof(value), ctypes.byref(value), ctypes.byref(used)):
                return bool(value.value) if value.kind == 13 else None  # EvtVarTypeBoolean
            return None
        finally:
            self.close(config)

    def read(self, attempt, stopped):
        # Named EventData comparisons use the Event Log service's XPath subset.
        ip = attempt.remote_ip
        ip_variants = (ip, f'::ffff:{ip}', f'::FFFF:{ip}')
        addresses = ' or '.join(
            f'Data[@Name="{field}"]="{value}"'
            for field in ('SourceAddress', 'DestAddress') for value in ip_variants
        )
        expression = (
            '*[System[(EventID=5152 or EventID=5157) and '
            f'TimeCreated[timediff(@SystemTime) <= {RECENT_MS}]] and EventData[{addresses}]]'
        )
        return self.read_channel(SECURITY_CHANNEL, expression, stopped)

    def read_defender(self, stopped):
        expression = (
            '*[System[(EventID=1125 or EventID=1126) and '
            f'TimeCreated[timediff(@SystemTime) <= {RECENT_MS}]]]'
        )
        return self.read_channel(DEFENDER_CHANNEL, expression, stopped)

    def read_channel(self, channel, expression, stopped):
        # EvtQueryChannelPath | EvtQueryReverseDirection
        results = self.query(None, channel, expression, 0x201)
        if not results:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            handles = (self.handle * 1)()
            returned = self.dword()
            deadline = time.monotonic() + QUERY_DEADLINE_SECONDS
            events = []
            for _ in range(MAX_EVENTS):
                remaining = int((deadline - time.monotonic()) * 1000)
                if stopped.is_set() or remaining <= 0:
                    return events, 'query_timeout'
                if not self.next(results, 1, handles, remaining, 0, ctypes.byref(returned)):
                    error = ctypes.get_last_error()
                    if error == 259:  # ERROR_NO_MORE_ITEMS
                        return events, None
                    if error in (1460, 258):
                        return events, 'query_timeout'
                    raise ctypes.WinError(error)
                try:
                    events.append(self.event_xml(handles[0]))
                finally:
                    self.close(handles[0])
            return events, 'query_limit'
        finally:
            self.close(results)

    def event_xml(self, handle):
        size = self.dword()
        count = self.dword()
        # EvtRenderEventXml returns a UTF-16 buffer size in bytes.
        self.render(None, handle, 1, 0, None, ctypes.byref(size), ctypes.byref(count))
        if not 0 < size.value <= 64 * 1024:
            return ''
        buffer = ctypes.create_unicode_buffer(size.value // ctypes.sizeof(ctypes.c_wchar))
        if not self.render(None, handle, 1, size.value, buffer, ctypes.byref(size), ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value


class WfpDiagnostics:
    def __init__(self, log, *, audit_session=None):
        self.log = log
        self.audit_session = audit_session
        self.enabled = sys.platform == 'win32'
        self.pending = queue.Queue(maxsize=MAX_PENDING)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name='windows-wfp-diagnostics', daemon=True)
        self.reported = OrderedDict()
        self.statuses = OrderedDict()

    def start(self):
        if self.enabled:
            self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=1.5)

    def submit(self, operation, ip, port, protocol, started, finished=None, *,
               directions=('outbound', 'inbound'), local=None):
        if not self.enabled or self.stopped.is_set() or not self.thread.is_alive():
            return
        remote_ip = address(ip)
        if remote_ip is None or ':' in remote_ip:
            return
        local_ip, local_port = local or (None, None)
        local_ip = address(local_ip)
        if finished is None:
            finished = time.time()
        attempt = Attempt(operation, remote_ip, port, protocol, started, finished,
                          directions, None if local_ip == '0.0.0.0' else local_ip, local_port or None)
        try:
            self.pending.put_nowait(attempt)
        except queue.Full:
            pass

    def run(self):
        reader = paths = None
        recent = OrderedDict()
        audit_policy = {}
        refresh_policy_at = 0
        pid = os.getpid()
        while not self.stopped.is_set():
            try:
                attempt = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            now = time.time()
            if (now - attempt.finished > RECENT_MS / 1000 - EVENT_GRACE_SECONDS
                    or now - recent.get(attempt.key, 0) < RECHECK_SECONDS):
                continue
            self.remember(recent, attempt.key, now, MAX_PENDING)
            defender = attempt.protocol == TCP and 'outbound' in attempt.directions
            grace = DEFENDER_QUERY_GRACE_SECONDS if defender else EVENT_GRACE_SECONDS
            if self.stopped.wait(max(0, attempt.finished + grace - now)):
                return
            if time.monotonic() >= refresh_policy_at:
                try:
                    audit_policy = query_wfp_audit_policy()
                except (OSError, AttributeError):
                    audit_policy = {str(event_id): 'unavailable' for event_id in WFP_AUDIT_GUIDS}
                refresh_policy_at = time.monotonic() + POLICY_REFRESH_SECONDS
            try:
                if reader is None:
                    reader, paths = SecurityEvents(), executable_paths()
            except OSError as error:
                self.report_error(attempt, SECURITY_CHANNEL, error, audit_policy)
                continue
            active_policy = self.audit_session.active_policy() if self.audit_session is not None else None
            self.scan(reader, attempt, pid, paths, SECURITY_CHANNEL, active_policy or audit_policy)
            if defender and not self.stopped.is_set():
                self.scan(reader, attempt, pid, paths, DEFENDER_CHANNEL)

    def scan(self, reader, attempt, pid, paths, channel, audit_policy=None):
        elevated = False
        try:
            if channel == SECURITY_CHANNEL:
                events, status = reader.read(attempt, self.stopped)
                match = event_match
            else:
                events, status = reader.read_defender(self.stopped)
                match = defender_match
        except OSError as error:
            captured = None
            if (channel == SECURITY_CHANNEL and self.audit_session is not None
                    and getattr(error, 'winerror', None) in (5, 1314)):
                captured = self.audit_session.read_events(self.stopped)
            if captured is None:
                self.report_error(attempt, channel, error, audit_policy)
                return
            events, status = captured
            match = event_match
            elevated = True
        found = False
        for xml in events:
            try:
                evidence = match(xml, attempt, pid, paths)
            except ET.ParseError:
                status = status or 'query_incomplete'
                continue
            if evidence is None:
                continue
            found = True
            if elevated:
                evidence['elevated_security_read'] = True
            record = channel, evidence['event_record_id'], evidence['event_at']
            if record not in self.reported and not self.stopped.is_set():
                self.remember(self.reported, record, True, MAX_EVENTS * 4)
                self.report_match(attempt, channel, evidence, audit_policy)
        if found:
            if status:
                self.report_status(attempt, channel, status, audit_policy)
            else:
                self.remember(self.statuses, (channel, attempt.key), ('matched',), MAX_PENDING * 2)
            return
        if status is None:
            if audit_policy and all(value == 'disabled' for value in audit_policy.values()):
                status = 'audit_disabled'
            elif channel == DEFENDER_CHANNEL and reader.channel_enabled(channel) is False:
                status = 'channel_disabled'
        self.report_status(attempt, channel, status or 'no_match', audit_policy)

    def report_match(self, attempt, channel, evidence, audit_policy):
        if channel == SECURITY_CHANNEL:
            code = 'windows_wfp_block'
            message = 'WFP engelleme olayı'
            explanation = 'Windows Filtreleme Platformu bu uç için bir engel kaydetti. Filtre kökeni varsa ayrıntılarda gösterilir.'
            write = self.log.warning
        elif evidence['event_id'] == 1126 and evidence['attribution'] == 'weak':
            code = 'windows_defender_observation'
            message = 'Microsoft Defender Ağ Koruması olası eşleşmesi (zayıf ilişkilendirme)'
            explanation = 'Bu gözlem işlem, HTTP uç noktası veya olay zamanı açısından kısmi bir eşleşme gösteriyor.'
            write = self.log.info
        elif evidence['event_id'] == 1126:
            code = 'windows_defender_block'
            message = 'Microsoft Defender Ağ Koruması engelleme olayı'
            explanation = 'Defender, eşleşen işlem ve hedef için engelleme modunda bir olay kaydetti.'
            write = self.log.warning
        else:
            code = 'windows_defender_audit'
            message = 'Microsoft Defender Ağ Koruması denetim olayı'
            explanation = 'Defender bu olayı denetim modunda kaydetti. 1125 yalnızca denetim kaydıdır.'
            write = self.log.info
        extra = self.extra(attempt, code, event_channel=channel, **evidence)
        if audit_policy:
            extra['details']['system_audit_failure'] = audit_policy
        extra['explanation'] = explanation
        if code in ('windows_wfp_block', 'windows_defender_block'):
            extra['possible_causes'] = [message]
        write('[Windows] %s %s:%s: %s (%s)', attempt.operation, attempt.remote_ip, attempt.remote_port,
              message, evidence['event_id'], extra=extra)

    def report_error(self, attempt, channel, error, audit_policy):
        code = getattr(error, 'winerror', None)
        status = {
            5: 'access_denied', 1314: 'access_denied', 15007: 'channel_unavailable',
            1460: 'query_timeout', 258: 'query_timeout',
        }.get(code, 'query_failed')
        self.report_status(attempt, channel, status, audit_policy, code)

    def report_status(self, attempt, channel, status, audit_policy=None, error_code=None):
        key = channel, attempt.key
        state = status, tuple((audit_policy or {}).items()), error_code
        if self.stopped.is_set() or self.statuses.get(key) == state:
            return
        self.remember(self.statuses, key, state, MAX_PENDING * 2)
        message = {
            'access_denied': 'olay günlüğünü okuma izni yok',
            'channel_unavailable': 'olay günlüğü kullanılamıyor',
            'channel_disabled': 'olay günlüğü kapalı',
            'audit_disabled': '5152 ve 5157 için sistem hata denetimi kapalı',
            'query_failed': 'olay sorgusu başarısız oldu',
            'query_timeout': 'olay sorgusu zaman aşımına uğradı',
            'query_limit': 'olay sorgusu kayıt sınırına ulaştı',
            'query_incomplete': 'olay sorgusunda okunamayan kayıt var',
            'no_match': 'denemeyle eşleşen kayıt bulunamadı',
        }[status]
        name = 'WFP' if channel == SECURITY_CHANNEL else 'Defender Ağ Koruması'
        code = 'windows_wfp_query' if channel == SECURITY_CHANNEL else 'windows_defender_query'
        extra = self.extra(attempt, code, event_channel=channel, query_status=status, winerror=error_code)
        if audit_policy:
            extra['details']['system_audit_failure'] = audit_policy
        if status in ('no_match', 'audit_disabled', 'channel_disabled'):
            extra['explanation'] = 'Bu sorguyla ağ engeli olup olmadığı belirlenemedi.'
        self.log.info('[Windows] %s %s:%s · %s: %s',
                      attempt.operation, attempt.remote_ip, attempt.remote_port, name, message, extra=extra)

    @staticmethod
    def remember(cache, key, value, limit):
        cache[key] = value
        cache.move_to_end(key)
        if len(cache) > limit:
            cache.popitem(last=False)

    @staticmethod
    def extra(attempt, code, **details):
        return {
            'source': 'windows',
            'code': code,
            'details': {
                'operation': attempt.operation, 'ip': attempt.remote_ip, 'port': attempt.remote_port,
                'attempt_started_at': datetime.fromtimestamp(attempt.started, timezone.utc).isoformat(),
                'attempt_finished_at': datetime.fromtimestamp(attempt.finished, timezone.utc).isoformat(),
                **details,
            },
        }
