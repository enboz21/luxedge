"""Thread-safe reachability evidence, independent of outgoing LED traffic."""
import threading
import time
from datetime import datetime, timedelta, timezone


def probe_result(endpoint, ip, port, timeout, skip_reason=None):
    return {'endpoint': endpoint, 'ip': ip, 'port': port, 'timeout_ms': int(timeout * 1000),
            'tested': False, 'ok': None, 'elapsed_ms': None, 'skip_reason': skip_reason}


def probe_error(error):
    reason = getattr(error, 'reason', None)
    if not isinstance(reason, BaseException):
        reason = error
    details = {'error_type': type(reason).__name__, 'timed_out': isinstance(reason, TimeoutError)}
    status = getattr(error, 'code', None)
    if type(status) is int:
        details['http_status'] = status
    return details


def describe_probe(probe):
    endpoint = probe['endpoint']
    port = probe['port']
    timeout = probe['timeout_ms']
    if not probe['tested']:
        return f'{endpoint}:{port} denenmedi ({probe["skip_reason"]})'
    outcome = 'başarılı' if probe['ok'] else ('zaman aşımı' if probe.get('timed_out') else 'başarısız')
    failures = probe.get('consecutive_failures')
    streak = f', ardışık hata: {failures}' if failures else ''
    return f'{endpoint}:{port} {outcome} ({probe["elapsed_ms"]:.1f}/{timeout} ms{streak})'


def next_probe_at(delay_seconds):
    date = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    return date.isoformat(timespec='milliseconds')


class Connectivity:
    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._sequence = 0
        self.reset('', 7777, active=False)

    def reset(self, ip, port, active=True, force=False):
        with self._lock:
            target = (ip or '', int(port), bool(active))
            if not force and getattr(self, '_target', None) == target:
                return
            self._target = target
            self._generation += 1
            self._applied = 0
            self._data = {
                'state': 'stopped' if not active else ('checking' if ip else 'unconfigured'),
                'ip': target[0], 'port': target[1],
                'udp_ok': None, 'http_ok': None, 'checked_at': None,
                'consecutive_failures': 0, 'udp_consecutive_failures': 0, 'http_consecutive_failures': 0,
            }

    def begin(self, ip, port):
        with self._lock:
            if self._target != (ip, int(port), True) or not ip:
                return None
            self._sequence += 1
            return self._generation, self._sequence

    def complete(self, token, udp_ok, http_ok, *, with_snapshot=False):
        with self._lock:
            if token is None or token[0] != self._generation or token[1] <= self._applied:
                return None if with_snapshot else False
            self._applied = token[1]
            data = self._data
            data.update(udp_ok=udp_ok, http_ok=http_ok, checked_at=time.time())
            if udp_ok is not None:
                data['udp_consecutive_failures'] = 0 if udp_ok else data['udp_consecutive_failures'] + 1
            if http_ok is not None:
                data['http_consecutive_failures'] = 0 if http_ok else data['http_consecutive_failures'] + 1
            if udp_ok or http_ok:
                data['consecutive_failures'] = 0
                data['state'] = 'connected' if udp_ok else 'http_only'
            else:
                data['consecutive_failures'] += 1
                if data['consecutive_failures'] >= 3:
                    data['state'] = 'disconnected'
            return dict(data) if with_snapshot else True

    def snapshot(self):
        with self._lock:
            return dict(self._data)


def legacy_connection(state):
    return {
        'connected': 'bağlı', 'http_only': 'bağlı',
        'checking': 'bağlanıyor', 'unconfigured': 'IP bekleniyor...',
        'stopped': 'kapalı', 'disconnected': 'bağlı değil',
    }[state]
