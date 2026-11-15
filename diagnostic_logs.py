"""Bounded shared log history for PC messages and Wemos events."""
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

HISTORY_LIMIT = 1000
FILE_LIMIT = 2 * 1024 * 1024
BACKUPS = 5
ROW_LIMIT = 8192


class DiagnosticLogHandler(logging.Handler):
    def __init__(self, directory):
        super().__init__()
        self.path = Path(directory) / 'luxedge.jsonl'
        self.entries = deque(maxlen=HISTORY_LIMIT)
        self.pending = deque(maxlen=HISTORY_LIMIT)
        self.last_id = time.time_ns() // 1000
        self.persistent = True
        for index in range(BACKUPS, -1, -1):
            path = self.path.with_suffix(f'.jsonl.{index}') if index else self.path
            try:
                with path.open('rb') as history:
                    # A damaged or externally enlarged file still has a bounded read.
                    if history.seek(0, os.SEEK_END) > FILE_LIMIT:
                        history.seek(-FILE_LIMIT, os.SEEK_END)
                        history.readline()
                    else:
                        history.seek(0)
                    for line in history:
                        self._restore(line)
            except FileNotFoundError:
                continue
            except OSError:
                self.persistent = False

    def _restore(self, line):
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
        if self.entries and row['id'] <= self.entries[-1]['id']:
            return
        self.entries.append(row)
        self.last_id = max(self.last_id, row['id'])

    def _persist(self, row):
        encoded = (json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8', 'backslashreplace')
        if len(encoded) > ROW_LIMIT:
            raise ValueError('Log kaydı çok uzun')
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
        if size + len(encoded) > FILE_LIMIT:
            for index in range(BACKUPS, 0, -1):
                source = self.path.with_suffix(f'.jsonl.{index - 1}') if index > 1 else self.path
                try:
                    source.replace(self.path.with_suffix(f'.jsonl.{index}'))
                except FileNotFoundError:
                    continue
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
        row = {
            'id': self.last_id,
            'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds'),
            'time': time.strftime('%H:%M:%S', time.localtime(record.created)),
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
