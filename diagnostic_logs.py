"""Bounded shared log history for PC messages and Wemos events."""
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

HISTORY_LIMIT = 1000
LIVE_EXPORT_LIMIT = 8192
LIVE_EXPORT_PAGE = 512
FILE_LIMIT = 2 * 1024 * 1024
ROW_LIMIT = 8192
MIB = 1024 * 1024
DEFAULT_RETENTION = 512 * MIB
MIN_RETENTION = 8 * MIB
MAX_RETENTION = 4096 * MIB
RETENTION_FILE = 'luxedge-log-retention.json'
LOG_FILE = re.compile(r'(luxedge(?:-electron)?\.jsonl)(?:\.[1-5]|\.seg-(\d{1,16})-(\d{1,16})-[a-f0-9]{16})?')
STREAM_THREAD = 'ambilight-stream'
STREAM_SAMPLER_THREAD = 'ambilight-stream-diagnostics'
STREAM_LOG_QUEUE_LIMIT = 256
STREAM_LOG_WARNING_RESERVE = 32


def read_retention(directory):
    try:
        path = directory / RETENTION_FILE
        if not stat.S_ISREG(path.lstat().st_mode):
            return DEFAULT_RETENTION
        with path.open('rb') as source:
            limit = json.loads(source.read(256)).get('limit_bytes')
        if type(limit) is int and MIN_RETENTION <= limit <= MAX_RETENTION and limit % MIB == 0:
            return limit
    except (OSError, ValueError, AttributeError, RecursionError):
        pass
    return DEFAULT_RETENTION


def managed_logs(directory):
    try:
        paths = directory.iterdir()
        results = []
        for path in paths:
            match = LOG_FILE.fullmatch(path.name)
            if not match:
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                results.append((path, match, info))
        return results
    except FileNotFoundError:
        return []


def archive_order_us(row):
    order = row.get('archive_order_us')
    if type(order) is int and 0 < order < 2**53:
        return order
    try:
        timestamp = row.get('timestamp', '').replace('Z', '+00:00')
        date = datetime.fromisoformat(timestamp)
        return int(date.timestamp() * 1000) * 1000 if date.tzinfo is not None else 0
    except (ValueError, OSError, OverflowError, AttributeError):
        return 0


def archive_sort_key(entry):
    path, match, info = entry
    end = int(match[3]) if match[3] else 0
    return end or info.st_mtime_ns // 1000, path.name


def prune_logs(directory, limit):
    # Two active files can grow between rotations. Only completed, managed files are removable.
    files = [entry for entry in managed_logs(directory) if entry[0].name != entry[1][1]]
    budget = limit - 2 * FILE_LIMIT
    total = sum(entry[2].st_size for entry in files)
    for path, _, info in sorted(files, key=archive_sort_key):
        if total <= budget:
            break
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        total -= info.st_size


class StreamLogWriter:
    """Keep stream logging off the frame thread, with reserved space for incidents."""
    def __init__(self, write):
        self.write = write
        self.queue = deque()
        self.condition = threading.Condition()
        self.stopping = False
        self.dropped = 0
        self.thread = threading.Thread(target=self.run, name='stream-log-writer', daemon=True)
        self.thread.start()

    def submit(self, record):
        limit = STREAM_LOG_QUEUE_LIMIT
        if record.levelno < logging.WARNING:
            limit -= STREAM_LOG_WARNING_RESERVE
        with self.condition:
            if self.stopping or len(self.queue) >= limit:
                self.dropped += 1
            else:
                self.queue.append(record)
            self.condition.notify()

    def stop(self):
        with self.condition:
            self.stopping = True
            self.condition.notify()
        self.thread.join(timeout=3)

    def run(self):
        next_drop_report = 0
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.queue or self.dropped or self.stopping)
                now = time.monotonic()
                report_due = self.dropped and (now >= next_drop_report or self.stopping)
                if report_due:
                    dropped, self.dropped = self.dropped, 0
                    next_drop_report = now + 5
                else:
                    dropped = 0
                if self.queue:
                    record = self.queue.popleft()
                elif self.stopping and not dropped:
                    return
                else:
                    record = None
                    if not dropped:
                        self.condition.wait(max(0, next_drop_report - now))
            if record is not None:
                self.write(record)
            if dropped:
                warning = logging.LogRecord('luxedge', logging.WARNING, __file__, 0,
                                            'Akış günlük kuyruğunda %s kayıt kaybedildi', (dropped,), None)
                warning.code = 'stream_log_queue_dropped'
                warning.count = dropped
                warning.details = {'dropped_records': dropped, 'queue_capacity': STREAM_LOG_QUEUE_LIMIT}
                self.write(warning)


class DiagnosticLogHandler(logging.Handler):
    def __init__(self, directory):
        super().__init__()
        self.path = Path(directory) / 'luxedge.jsonl'
        self.entries = deque(maxlen=HISTORY_LIMIT)
        self.pending = deque(maxlen=HISTORY_LIMIT)
        self.last_id = time.time_ns() // 1000
        self.last_order_us = 0
        self.persistent = True
        self.stream_writer = None
        self.export_lock = threading.Lock()
        self.export_entries = deque(maxlen=LIVE_EXPORT_LIMIT)
        self.export_evicted = None
        self.export_generation = uuid.uuid4().hex
        self.next_retention_check = 0
        self.retention_limit = None
        self._restore_history()

    def _restore_history(self):
        try:
            files = [entry for entry in managed_logs(self.path.parent) if entry[1][1] == self.path.name]
        except OSError:
            self.persistent = False
            return
        files.sort(key=lambda entry: (entry[0] == self.path, archive_sort_key(entry)), reverse=True)
        rows = {}
        for path, _, _ in files:
            try:
                with path.open('rb') as history:
                    # A damaged or externally enlarged file still has a bounded read.
                    if history.seek(0, os.SEEK_END) > FILE_LIMIT:
                        history.seek(-FILE_LIMIT, os.SEEK_END)
                        history.readline()
                    else:
                        history.seek(0)
                    for line in reversed(history.read(FILE_LIMIT).splitlines()):
                        row = self._read_row(line)
                        if row is not None:
                            rows.setdefault(row['id'], row)
                            self.last_order_us = max(self.last_order_us, archive_order_us(row))
                        if len(rows) >= HISTORY_LIMIT:
                            break
            except FileNotFoundError:
                continue
            except OSError:
                self.persistent = False
            if len(rows) >= HISTORY_LIMIT:
                break
        for row_id in sorted(rows):
            self.entries.append(rows[row_id])
            self.last_id = max(self.last_id, row_id)

    def start_stream_writer(self):
        self.stream_writer = StreamLogWriter(super().handle)

    def stop_stream_writer(self):
        if self.stream_writer is not None:
            self.stream_writer.stop()

    def handle(self, record):
        if self.stream_writer is not None and record.threadName in (STREAM_THREAD, STREAM_SAMPLER_THREAD):
            self.stream_writer.submit(record)
            return True
        return super().handle(record)

    @staticmethod
    def _read_row(line):
        if len(line) > ROW_LIMIT:
            return
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(row, dict) or type(row.get('id')) is not int:
            return
        if not 0 < row['id'] < 2**53 - 1 or not isinstance(row.get('msg'), str):
            return
        return row

    def _rotate(self, size):
        begin = end = 0
        if size <= FILE_LIMIT:
            with self.path.open('rb') as history:
                orders = [archive_order_us(row) for line in history if (row := self._read_row(line)) is not None]
            if orders and min(orders) > 0:
                begin, end = min(orders), max(orders)
        segment = f'{self.path.name}.seg-{begin}-{end}-{uuid.uuid4().hex[:16]}'
        self.path.rename(self.path.with_name(segment))

    def _enforce_retention(self, rotated):
        now = time.monotonic()
        if rotated or now >= self.next_retention_check:
            limit = read_retention(self.path.parent)
            if rotated or limit != self.retention_limit:
                prune_logs(self.path.parent, limit)
            self.retention_limit = limit
            self.next_retention_check = now + 5

    def _persist(self, row):
        encoded = (json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8', 'backslashreplace')
        if len(encoded) > ROW_LIMIT:
            raise ValueError('Log kaydı çok uzun')
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise OSError('Günlük dosyası normal bir dosya değil')
            size = info.st_size
        except FileNotFoundError:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
        rotated = size > 0 and size + len(encoded) + 1 > FILE_LIMIT
        if rotated:
            self._rotate(size)
        self._enforce_retention(rotated)
        # A process interruption may have left a partial final line.
        with self.path.open('a+b') as history:
            if history.seek(0, os.SEEK_END):
                history.seek(-1, os.SEEK_END)
                if history.read(1) != b'\n':
                    history.write(b'\n')
            history.write(encoded)

    def _flush_pending(self):
        for _ in range(min(32, len(self.pending))):
            row, delivery = self.pending[0]
            try:
                self._persist(row)
            except (OSError, TypeError, ValueError):
                self.persistent = False
                return
            self.pending.popleft()
            if delivery is not None:
                delivery['state'] = 'saved'
        self.persistent = not self.pending

    def emit(self, record):
        delivery = getattr(record, 'delivery', None)
        if delivery is not None and delivery.get('state') in ('pending', 'saved'):
            self._flush_pending()
            return
        self.last_id += 1
        self.last_order_us = max(time.time_ns() // 1000, self.last_order_us + 1)
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        row = {
            'id': self.last_id,
            'archive_order_us': self.last_order_us,
            'timestamp': timestamp.isoformat(timespec='milliseconds'),
            'time': timestamp.astimezone().time().isoformat(timespec='milliseconds'),
            'level': record.levelname,
            'msg': record.getMessage()[:512],
            'source': getattr(record, 'source', 'pc'),
            'code': getattr(record, 'code', 'pc_message'),
            'device_id': getattr(record, 'device_id', None),
            'device_name': getattr(record, 'device_name', None),
            'explanation': getattr(record, 'explanation', ''),
            'possible_causes': getattr(record, 'possible_causes', []),
            'details': getattr(record, 'details', {}),
            'count': getattr(record, 'count', 1),
        }
        self.entries.append(row)
        with self.export_lock:
            if len(self.export_entries) == self.export_entries.maxlen:
                self.export_evicted = self.export_entries[0]
            self.export_entries.append(row)
        if len(self.pending) == self.pending.maxlen:
            _, dropped = self.pending.popleft()
            if dropped is not None:
                dropped['state'] = 'dropped'
        if delivery is not None:
            delivery['state'] = 'pending'
        self.pending.append((row, delivery))
        self._flush_pending()
        if getattr(record, 'source', 'pc') == 'pc':
            try:
                print(self.format(record), flush=True)
            except (OSError, UnicodeError, ValueError):
                pass

    def snapshot(self, after=None, limit=HISTORY_LIMIT):
        with self.lock:
            if after is None:
                entries = list(self.entries)[-limit:]
            else:
                entries = [row for row in self.entries if row['id'] > after][:limit]
            return {'logs': entries, 'available': True, 'persistent': self.persistent}

    def export_snapshot(self, since, through, after, limit=LIVE_EXPORT_PAGE):
        with self.export_lock:
            rows = []
            for row in self.export_entries:
                if row['archive_order_us'] > through:
                    break
                if row['archive_order_us'] >= since and row['id'] > after:
                    rows.append(row)
                    if len(rows) > limit:
                        break
            evicted = self.export_evicted
            return {
                'logs': rows[:limit], 'has_more': len(rows) > limit,
                'generation': self.export_generation,
                'overflow': evicted is not None and evicted['id'] > after and evicted['archive_order_us'] >= since,
            }
