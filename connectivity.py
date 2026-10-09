"""Thread-safe reachability evidence, independent of outgoing LED traffic."""
import threading
import time


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
                'consecutive_failures': 0,
            }

    def begin(self, ip, port):
        with self._lock:
            if self._target != (ip, int(port), True) or not ip:
                return None
            self._sequence += 1
            return self._generation, self._sequence

    def complete(self, token, udp_ok, http_ok):
        with self._lock:
            if token is None or token[0] != self._generation or token[1] <= self._applied:
                return False
            self._applied = token[1]
            data = self._data
            data.update(udp_ok=udp_ok, http_ok=http_ok, checked_at=time.time())
            if udp_ok or http_ok:
                data['consecutive_failures'] = 0
                data['state'] = 'connected' if udp_ok else 'http_only'
            else:
                data['consecutive_failures'] += 1
                if data['consecutive_failures'] >= 3:
                    data['state'] = 'disconnected'
            return True

    def snapshot(self):
        with self._lock:
            return dict(self._data)


def legacy_connection(state):
    return {
        'connected': 'bağlı', 'http_only': 'bağlı',
        'checking': 'bağlanıyor', 'unconfigured': 'IP bekleniyor...',
        'stopped': 'kapalı', 'disconnected': 'bağlı değil',
    }[state]
