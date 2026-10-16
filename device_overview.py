"""Read-only dashboard telemetry; probes never block the HTTP or capture thread."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class DeviceOverview:
    def __init__(self):
        self.lock = threading.Lock()
        self.live = {}
        self.frames = {}
        self.refreshing = False
        self.next_refresh = 0

    @staticmethod
    def key(device):
        return (device.get('id'), device.get('ip'), device.get('port', 7777))

    def record_frame(self, device_id, ip, port, data, mode='stream', now=None, strips=None):
        now = time.time() if now is None else now
        key = (device_id, ip, port)
        with self.lock:
            previous = self.frames.get(key)
            if previous and now - previous['updated_at'] < 0.25:
                return
        count = len(data) // 3
        if not count:
            return
        # At most sixteen actual outgoing LED colors; no extra screen capture.
        def sample(payload):
            size = len(payload) // 3
            return [list(payload[3 * (i * (size - 1) // max(1, min(size, 16) - 1)):
                                 3 * (i * (size - 1) // max(1, min(size, 16) - 1)) + 3])
                    for i in range(min(size, 16))]
        colors = sample(data)
        streams, offset = {}, 0
        for strip in strips or []:
            size = int(strip.get('led_count', 0)) * 3
            if size > 0 and offset + size <= len(data):
                streams[str(strip['index'])] = sample(data[offset:offset + size])
            offset += size
        with self.lock:
            self.frames[key] = dict(colors=colors, streams=streams, mode=mode, updated_at=now)

    def snapshot(self, devices, probe, now=None):
        now = time.time() if now is None else now
        with self.lock:
            keys = {self.key(d) for d in devices}
            self.live = {k: v for k, v in self.live.items() if k in keys}
            self.frames = {k: v for k, v in self.frames.items() if k in keys}
            result = [dict(d, live=self.live.get(self.key(d)),
                           frame=self.frames.get(self.key(d))) for d in devices]
            if not self.refreshing and now >= self.next_refresh:
                self.refreshing = True
                threading.Thread(target=self._refresh, args=(list(devices), probe), daemon=True).start()
            return result

    def _refresh(self, devices, probe):
        def read(device):
            try:
                status = probe(device.get('ip', ''), timeout=1)
            except Exception:
                status = None
            return self.key(device), dict(reachable=status is not None, status=status,
                                          checked_at=time.time())
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                for key, value in pool.map(read, devices):
                    with self.lock:
                        self.live[key] = value
        finally:
            with self.lock:
                self.refreshing = False
                self.next_refresh = time.time() + 5


device_overview = DeviceOverview()
