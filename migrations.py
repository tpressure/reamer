"""Durable, idempotent aggregation of compute-node migration observations."""
import secrets
import sqlite3
import threading
import time
import weakref
from datetime import datetime, timezone
from uuid import UUID


def vm_identity(value):
    try:
        identity = UUID(value) if isinstance(value, str) else None
        return str(identity) if identity and identity.int not in (0, 2**128 - 1) else None
    except ValueError:
        return None


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('Invalid event timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Timestamp must include a timezone')
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, OverflowError):
        raise ValueError('Invalid event timestamp') from None


class MigrationStore:
    def __init__(self, path=':memory:'):
        self.lock = threading.Lock()
        self.epoch = secrets.token_hex(16)
        self.db = sqlite3.connect(path, check_same_thread=False)
        weakref.finalize(self, self.db.close)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS migrations (
                vm_uuid TEXT, at TEXT, downtime_ms INTEGER, PRIMARY KEY(vm_uuid, at));
            CREATE TABLE IF NOT EXISTS placements (
                vm_uuid TEXT, node TEXT, instance TEXT, active_at TEXT, ended_at TEXT,
                PRIMARY KEY(vm_uuid, node));
            CREATE TABLE IF NOT EXISTS nodes (node TEXT PRIMARY KEY, seen REAL);
        ''')

    def ingest(self, report):
        if not isinstance(report, dict):
            raise ValueError('Expected an object')
        node = report.get('node')
        if not isinstance(node, str) or not 1 <= len(node) <= 255 or any(ord(c) < 32 for c in node):
            raise ValueError('Invalid compute-node name')
        events, placements = report.get('events'), report.get('placements')
        if not isinstance(events, list) or not isinstance(placements, list) or len(events) > 256 or len(placements) > 256:
            raise ValueError('Expected batches of at most 256 events and placements')
        clean_events, clean_placements = [], []
        for event in events:
            identity = vm_identity(event.get('vm_uuid')) if isinstance(event, dict) else None
            if not identity:
                raise ValueError('Invalid VM UUID')
            value = event.get('downtime_ms')
            if type(value) is not int or not 0 <= value <= 2**53:
                raise ValueError('Invalid downtime')
            clean_events.append((identity, timestamp(event.get('at')), value))
        for placement in placements:
            identity = vm_identity(placement.get('vm_uuid')) if isinstance(placement, dict) else None
            if not identity:
                raise ValueError('Invalid VM UUID')
            instance = placement.get('instance')
            if not isinstance(instance, str) or len(instance) > 255:
                raise ValueError('Invalid instance name')
            active = timestamp(placement['active_at']) if placement.get('active_at') is not None else None
            ended = timestamp(placement['ended_at']) if placement.get('ended_at') is not None else None
            clean_placements.append((identity, node, instance, active, ended))
        with self.lock, self.db:
            self.db.executemany('INSERT OR IGNORE INTO migrations VALUES (?, ?, ?)', clean_events)
            for identity, source, instance, active, ended in clean_placements:
                self.db.execute('INSERT OR IGNORE INTO placements VALUES (?, ?, ?, NULL, NULL)', (identity, source, instance))
                for column, value in (('active_at', active), ('ended_at', ended)):
                    if value is not None:
                        self.db.execute(f'UPDATE placements SET {column} = ?, instance = ? WHERE vm_uuid = ? AND node = ? AND ({column} IS NULL OR {column} < ?)', (value, instance, identity, source, value))
            self.db.execute('INSERT OR REPLACE INTO nodes VALUES (?, ?)', (node, time.time()))
        return {'epoch': self.epoch}

    def summary(self, identity):
        if not identity:
            return None
        with self.lock:
            count, minimum, average, maximum = self.db.execute('SELECT count(*), min(downtime_ms), avg(downtime_ms), max(downtime_ms) FROM migrations WHERE vm_uuid = ?', (identity,)).fetchone()
            last = self.db.execute('SELECT at, downtime_ms FROM migrations WHERE vm_uuid = ? ORDER BY at DESC LIMIT 1', (identity,)).fetchone()
            location = self.db.execute('SELECT p.node, p.instance, p.active_at, p.ended_at, n.seen FROM placements p JOIN nodes n USING(node) WHERE vm_uuid = ? AND active_at IS NOT NULL ORDER BY active_at DESC, p.node LIMIT 1', (identity,)).fetchone()
        if not count and not location:
            return None
        result = {'count': count, 'min_ms': minimum, 'avg_ms': average, 'max_ms': maximum,
                  'last_ms': last[1] if last else None, 'last_at': last[0] if last else None,
                  'node': None, 'node_stale': False, 'instance': None}
        if location:
            node, instance, active, ended, seen = location
            result.update(instance=instance, node_stale=time.time() - seen > 30)
            if ended is None or ended < active:
                result['node'] = node
        return result
