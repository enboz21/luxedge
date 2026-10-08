"""Enumerate displays only at startup or on explicit user refresh."""
from copy import deepcopy
from threading import Lock


class MonitorCatalog:
    def __init__(self):
        self._lock = Lock()
        self._monitors = []
        self._version = 0

    def snapshot(self):
        with self._lock:
            return {'monitors': deepcopy(self._monitors), 'monitor_list_version': self._version}

    @property
    def version(self):
        with self._lock:
            return self._version

    def refresh(self, enumerate_monitors):
        monitors = enumerate_monitors()
        with self._lock:
            if not monitors:
                return {'success': False, 'message': 'Monitör listesi alınamadı; önceki liste korundu.',
                        'monitors': deepcopy(self._monitors), 'monitor_list_version': self._version}
            self._monitors = deepcopy(monitors)
            self._version += 1
            return {'success': True, 'monitors': deepcopy(self._monitors),
                    'monitor_list_version': self._version}
