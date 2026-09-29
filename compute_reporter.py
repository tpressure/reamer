#!/usr/bin/env python3
"""Incrementally report Cloud Hypervisor migration logs; Python standard library only."""
import argparse
import json
import os
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import UUID

DEFAULT_HOST = "127.0.0.1"
STAMP = re.compile(r'cloud-hypervisor:\s+(\S+Z):')
VM_UUID = re.compile(r'system_uuid:\s*Some\("([0-9a-fA-F-]{36})"\)')
COMPLETE = re.compile(r'Migration completed after [0-9.]+s with a downtime of ([0-9]+)ms\b')
PRECOPY = re.compile(r'Precopy(?P<converged> converged)?: iter=(?P<iteration>[0-9]+)\b')
EVENT = re.compile(r'Event: source = vm event = ([\w-]+)\b')
LOG_NAME = re.compile(r'(instance-[\w-]+)\.log(?:\.\d+)?$')


class Reporter:
    def __init__(self, log_dir, state_file):
        self.log_dir = Path(log_dir)
        self.db = sqlite3.connect(state_file)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, dev INTEGER, ino INTEGER, offset INTEGER,
                vm_uuid TEXT, anchor BLOB);
            CREATE TABLE IF NOT EXISTS events (
                vm_uuid TEXT, at TEXT, downtime_ms INTEGER, UNIQUE(vm_uuid, at));
            CREATE TABLE IF NOT EXISTS placements (
                vm_uuid TEXT PRIMARY KEY, instance TEXT, active_at TEXT, ended_at TEXT);
            CREATE TABLE IF NOT EXISTS identities (instance TEXT PRIMARY KEY, vm_uuid TEXT);
        ''')
        if 'iterations' not in {row[1] for row in self.db.execute('PRAGMA table_info(events)')}:
            # Re-read retained logs once when upgrading, to enrich cached events.
            with self.db:
                self.db.execute('ALTER TABLE events ADD COLUMN iterations INTEGER')
                self.db.execute('DELETE FROM files')
        self.db.execute('''CREATE TABLE IF NOT EXISTS iteration_state (
            vm_uuid TEXT PRIMARY KEY, at TEXT, iterations INTEGER)''')
        self.epoch = None
        self.sent = 0

    def record_line(self, instance, vm_uuid, line):
        identity = VM_UUID.search(line)
        if identity:
            vm_uuid = str(UUID(identity[1]))
            self.db.execute('INSERT OR REPLACE INTO identities VALUES (?, ?)', (instance, vm_uuid))
        stamp = STAMP.search(line)
        if not vm_uuid or not stamp:
            return vm_uuid
        try:
            at = datetime.fromisoformat(stamp[1].replace('Z', '+00:00')).astimezone(timezone.utc).isoformat(timespec='microseconds')
        except ValueError:
            return vm_uuid
        complete = COMPLETE.search(line)
        event = EVENT.search(line)
        precopy = PRECOPY.search(line)
        if complete:
            progress = self.db.execute('SELECT at, iterations FROM iteration_state WHERE vm_uuid = ?', (vm_uuid,)).fetchone()
            iterations = progress[1] if progress and progress[0] <= at else None
            old = self.db.execute('SELECT iterations FROM events WHERE vm_uuid = ? AND at = ?', (vm_uuid, at)).fetchone()
            if old is not None and old[0] is None and iterations is not None:
                # Give enriched events a new rowid so an already-running uploader
                # sends them again even when backfill spans several scan batches.
                next_rowid = self.db.execute('SELECT coalesce(max(rowid), 0) + 1 FROM events').fetchone()[0]
                self.db.execute('UPDATE events SET rowid = ?, iterations = ? WHERE vm_uuid = ? AND at = ?',
                                (next_rowid, iterations, vm_uuid, at))
            self.db.execute('INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)', (vm_uuid, at, int(complete[1]), iterations))
        reset = complete or (event and event[1] in (
            'migration-started', 'migration-receive-started', 'migration-receive-finished',
            'booted', 'deleted', 'shutdown'))
        if reset or precopy:
            # Precopy indices start at zero. "converged" reports the next index,
            # while "Precopy complete" includes the stopped-VM transfer and is ignored.
            count = None if reset else int(precopy['iteration']) + (0 if precopy['converged'] else 1)
            self.db.execute('''INSERT INTO iteration_state VALUES (?, ?, ?)
                ON CONFLICT(vm_uuid) DO UPDATE SET at = excluded.at, iterations = excluded.iterations
                WHERE iteration_state.at <= excluded.at''', (vm_uuid, at, count))
        active = event and event[1] in ('booted', 'migration-receive-finished')
        ended = complete or (event and event[1] in ('deleted', 'shutdown'))
        if active or ended:
            self.db.execute('INSERT OR IGNORE INTO placements VALUES (?, ?, NULL, NULL)', (vm_uuid, instance))
            column = 'active_at' if active else 'ended_at'
            self.db.execute(f'UPDATE placements SET {column} = ?, instance = ? WHERE vm_uuid = ? AND ({column} IS NULL OR {column} < ?)', (at, instance, vm_uuid, at))
        return vm_uuid

    def scan(self):
        # Enumerating also discovers new VMs and numbered, uncompressed rotations.
        progressed = False
        paths = [path for path in self.log_dir.iterdir() if LOG_NAME.fullmatch(path.name)]
        # Read older numbered rotations first, so their VM identity is available
        # when the current file continues without another configuration dump.
        def order(path):
            parts = path.name.split('.log')
            return parts[0], -int(parts[1].lstrip('.') or '0')
        for path in sorted(paths, key=order):
            match = LOG_NAME.fullmatch(path.name)
            if not match:
                continue
            try:
                progressed = self.scan_file(path, match[1]) or progressed
            except OSError as exc:
                print(f'{path}: {exc}', file=sys.stderr, flush=True)

        return progressed

    def scan_file(self, path, instance):
        with path.open('rb') as stream, self.db:
            stat = os.fstat(stream.fileno())
            old = self.db.execute('SELECT dev, ino, offset, vm_uuid, anchor FROM files WHERE path = ?', (str(path),)).fetchone()
            known = self.db.execute('SELECT vm_uuid FROM identities WHERE instance = ?', (instance,)).fetchone()
            offset, vm_uuid = 0, known[0] if known else None
            if old and (stat.st_dev, stat.st_ino) == old[:2] and stat.st_size >= old[2]:
                stream.seek(max(0, old[2] - len(old[4])))
                if stream.read(len(old[4])) == old[4]:
                    if old[3] is not None or vm_uuid is None:
                        offset, vm_uuid = old[2:4]
            stream.seek(offset)
            initial_offset = offset
            # Bound each scan so a busy VM cannot starve the other logs or uploads.
            limit = offset + 1024 * 1024
            while stream.tell() < limit:
                start = stream.tell()
                line = stream.readline(256 * 1024)
                if not line:
                    break
                if not line.endswith(b'\n') and len(line) < 256 * 1024:
                    stream.seek(start)  # Wait for an unfinished write.
                    break
                vm_uuid = self.record_line(instance, vm_uuid, line.decode('utf-8', errors='replace'))
            offset = stream.tell()
            stream.seek(max(0, offset - 64))
            anchor = stream.read(min(offset, 64))
            cursor = (stat.st_dev, stat.st_ino, offset, vm_uuid, anchor)
            if old != cursor:
                self.db.execute('INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?)', (str(path), *cursor))
            return offset > initial_offset

    def upload(self, host, port, node):
        events = self.db.execute('SELECT rowid, vm_uuid, at, downtime_ms, iterations FROM events WHERE rowid > ? ORDER BY rowid LIMIT 256', (self.sent,)).fetchall()
        placements = [dict(zip(('vm_uuid', 'instance', 'active_at', 'ended_at'), row)) for row in self.db.execute('SELECT * FROM placements')]
        # Split placements too, to keep every request below the server's size limit.
        for offset in range(0, max(1, len(placements)), 256):
            payload = {'node': node, 'events': [dict(zip(('vm_uuid', 'at', 'downtime_ms', 'iterations'), row[1:])) for row in events] if offset == 0 else [], 'placements': placements[offset:offset + 256]}
            request = Request(f'http://{host}:{port}/compute-report', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=10) as response:
                reply = json.load(response)
            epoch = reply.get('epoch')
            if not isinstance(epoch, str) or not epoch:
                raise ValueError('Invalid compute-report acknowledgement')
            if epoch != self.epoch:
                self.epoch, self.sent = epoch, 0
                return  # Replay retained events after a server restart.
            if offset == 0 and events:
                self.sent = events[-1][0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--port', type=int, default=2222, help='Reamer HTTP port (default: 2222)')
    parser.add_argument('--node', default=socket.gethostname(), help='Unique compute-node name')
    parser.add_argument('--log-dir', type=Path, default=Path('/var/log/libvirt/ch'))
    parser.add_argument('--state-file', type=Path, default=Path('/var/lib/reamer-compute/state.sqlite3'))
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--once', action='store_true', help='Scan once and upload all cached records, then exit')
    args = parser.parse_args()
    if args.interval <= 0 or not args.node or len(args.node) > 255:
        parser.error('Use a positive interval and a node name of 1–255 characters')
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    reporter = Reporter(args.log_dir, args.state_file)
    try:
        while True:
            try:
                reporter.scan()
                reporter.upload(args.host, args.port, args.node)
                if args.once:
                    # Catch up large logs and replay batches until fully acknowledged.
                    while True:
                        progressed = reporter.scan()
                        maximum = reporter.db.execute('SELECT coalesce(max(rowid), 0) FROM events').fetchone()[0]
                        reporter.upload(args.host, args.port, args.node)
                        if not progressed and reporter.sent >= maximum:
                            break
                    return
            except (OSError, ValueError) as exc:
                print(f'Compute report failed: {exc}', file=sys.stderr, flush=True)
                if args.once:
                    raise SystemExit(1)
            time.sleep(args.interval)
    finally:
        reporter.db.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
