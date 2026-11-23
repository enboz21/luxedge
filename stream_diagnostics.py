"""Per-target observations at the PC's UDP socket boundary."""
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from diagnostic_logs import STREAM_SAMPLER_THREAD


def utc_milliseconds(timestamp_ns):
    if timestamp_ns is None:
        return None
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, timezone.utc).isoformat(timespec='milliseconds')


@dataclass
class SendErrors:
    first_ns: int
    last_ns: int
    count: int
    last_error: dict

    def details(self):
        return {
            'first_error_at': utc_milliseconds(self.first_ns),
            'last_error_at': utc_milliseconds(self.last_ns),
            'consecutive_errors': self.count,
            **self.last_error,
        }


@dataclass
class TargetWindow:
    fps: float
    started: float
    last_success: float
    attempted: int = 0
    accepted: int = 0
    accepted_bytes: int = 0
    errors: int = 0
    longest_gap: float = 0
    last_success_ns: int | None = None
    error_run: SendErrors | None = None

    def sample(self, now):
        elapsed = now - self.started
        values = {
            'target_fps': self.fps,
            'measured_fps': round(self.accepted / elapsed, 2),
            'window_ms': round(elapsed * 1000, 1),
            'longest_gap_ms': round(max(self.longest_gap, now - self.last_success) * 1000, 1),
            'attempted_packets': self.attempted,
            'accepted_packets': self.accepted,
            'accepted_bytes': self.accepted_bytes,
            'send_errors': self.errors,
            'last_successful_send_at': utc_milliseconds(self.last_success_ns),
        }
        if self.error_run is not None:
            values.update(self.error_run.details())
        self.started = now
        self.attempted = self.accepted = self.accepted_bytes = self.errors = 0
        self.longest_gap = 0
        return values


class StreamDiagnostics:
    def __init__(self, log):
        self.log = log
        self.targets = {}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name=STREAM_SAMPLER_THREAD, daemon=True)

    def configure(self, targets, fps):
        with self.lock:
            now = time.perf_counter()
            configured = {}
            for target in targets:
                state = self.targets.get(target)
                if state is None:
                    state = TargetWindow(fps, now, now)
                state.fps = fps
                configured[target] = state
            self.targets = configured

    def attempt(self, target):
        with self.lock:
            self.targets[target].attempted += 1

    def accepted(self, target, byte_count):
        with self.lock:
            now = time.perf_counter()
            timestamp = time.time_ns()
            state = self.targets[target]
            state.longest_gap = max(state.longest_gap, now - state.last_success)
            state.last_success = now
            state.last_success_ns = timestamp
            state.accepted += 1
            state.accepted_bytes += byte_count
            error_run, state.error_run = state.error_run, None
        if error_run is not None:
            details = {'last_successful_send_at': utc_milliseconds(timestamp), **error_run.details()}
            self.log.info('PC UDP %s:%s: yerel gönderim düzeldi (%s hata, ilk %s, son %s)',
                          target[1], target[2], error_run.count, details['first_error_at'],
                          details['last_error_at'], extra=self.extra(target, 'udp_send_recovered', details))

    def failed(self, target, error):
        error_info = {'error_type': type(error).__name__, 'error_message': str(error)[:256]}
        for field in ('winerror', 'errno'):
            value = getattr(error, field, None)
            if type(value) is int:
                error_info[field] = value
        timestamp = time.time_ns()
        with self.lock:
            state = self.targets[target]
            state.errors += 1
            if state.error_run is not None:
                state.error_run.last_ns = timestamp
                state.error_run.count += 1
                state.error_run.last_error = error_info
                return
            state.error_run = SendErrors(timestamp, timestamp, 1, error_info)
            details = state.error_run.details()
        self.log.error('PC UDP %s:%s: yerel gönderim hatası başladı (%s)', target[1], target[2],
                       error_info['error_message'], extra=self.extra(target, 'udp_send_error_started', details))

    def run(self):
        while not self.stopped.wait(1):
            self.report()
        self.report(final=True)

    def report(self, final=False):
        with self.lock:
            now = time.perf_counter()
            reports = [(target, state.sample(now)) for target, state in self.targets.items()
                       if now > state.started and (not final or state.attempted or state.accepted or state.errors)]
        for target, sample in reports:
            sample['partial_window'] = final
            message = ('PC UDP %s:%s: %.2f/%.2f FPS, en uzun boşluk %.1f ms, %s deneme, '
                       '%s kabul (%s bayt), %s hata, son yerel gönderim: %s')
            values = (target[1], target[2], sample['measured_fps'], sample['target_fps'], sample['longest_gap_ms'],
                      sample['attempted_packets'], sample['accepted_packets'], sample['accepted_bytes'],
                      sample['send_errors'], sample['last_successful_send_at'] or 'henüz yok')
            if 'consecutive_errors' in sample:
                message += ', ardışık hata: %s (ilk %s, son %s)'
                values += sample['consecutive_errors'], sample['first_error_at'], sample['last_error_at']
            self.log.info(message, *values, extra=self.extra(target, 'udp_send_sample', sample))

    def stop(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)

    @staticmethod
    def extra(target, code, details):
        device_id, ip, port = target
        return {
            'code': code,
            'device_id': device_id,
            'explanation': 'PC işletim sisteminin yerel UDP gönderim sonucu.',
            'details': {'ip': ip, 'port': port, **details},
        }
