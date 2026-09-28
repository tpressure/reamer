#!/usr/bin/env python3
"""Run on one OpenStack controller with the usual OpenStack admin environment."""
import argparse
import fcntl
import json
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import UUID

DEFAULT_HOST = "127.0.0.1"


def choose_destination(hosts, current):
    hosts = sorted(set(hosts))
    candidates = [host for host in hosts if host != current]
    if not candidates:
        raise ValueError('No other enabled, healthy compute host is available')
    # A simple ring spreads repeated migrations over all available nodes.
    return next((host for host in candidates if host > current), candidates[0])


class Controller:
    def __init__(self, state_file, executable='openstack'):
        self.executable = executable
        self.lock = threading.Lock()
        self.db = sqlite3.connect(state_file, check_same_thread=False)
        self.db.execute('''CREATE TABLE IF NOT EXISTS commands (
            id TEXT PRIMARY KEY, vm_uuid TEXT, state TEXT, destination TEXT, error TEXT, acknowledged INTEGER)''')
        # An interrupted CLI may already have submitted the migration. Never resubmit it.
        self.db.execute("UPDATE commands SET state='error', error='Controller restarted during migration; verify the VM location before retrying', acknowledged=0 WHERE state='running'")
        self.db.commit()
        self.hosts, self.hosts_at = [], 0
        self.worker = None

    def cli(self, *args, timeout=60):
        result = subprocess.run([self.executable, '--os-compute-api-version', '2.30', *args],
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise ValueError((result.stderr.strip() or result.stdout.strip() or 'OpenStack command failed')[-500:])
        return result.stdout

    def discover(self):
        rows = json.loads(self.cli('compute', 'service', 'list', '--service', 'nova-compute', '-f', 'json'))
        if not isinstance(rows, list):
            raise ValueError('Invalid compute service inventory')
        hosts = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid compute service entry')
            if row.get('Status') == 'enabled' and row.get('State') == 'up':
                host = row.get('Host')
                if not isinstance(host, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,255}', host):
                    raise ValueError('Invalid compute host name')
                hosts.add(host)
        with self.lock:
            self.hosts, self.hosts_at = sorted(hosts), time.monotonic()

    def inventory(self):
        return list(self.hosts) if time.monotonic() - self.hosts_at < 120 else []

    def report(self, host, port, controller):
        with self.lock:
            rows = self.db.execute('SELECT id,state,destination,error FROM commands WHERE acknowledged=0 LIMIT 128').fetchall()
            payload = {'controller': controller, 'hosts': self.inventory(),
                       'results': [dict(zip(('id', 'state', 'destination', 'error'), row)) for row in rows]}
        request = Request(f'http://{host}:{port}/controller-report', data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=10) as response:
            reply = json.load(response)
        with self.lock, self.db:
            for identity in reply['acknowledged']:
                self.db.execute("UPDATE commands SET acknowledged=1 WHERE id=? AND state != 'running'", (identity,))
        for command in reply['commands']:
            self.accept(command)

    def accept(self, command):
        identity, vm = command['id'], str(UUID(command['vm_uuid']))
        if not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise ValueError('Invalid migration command ID')
        with self.lock:
            if self.db.execute('SELECT 1 FROM commands WHERE id=?', (identity,)).fetchone():
                # Resend cached results when an acknowledgement was lost or the server restarted.
                self.db.execute('UPDATE commands SET acknowledged=0 WHERE id=?', (identity,))
                self.db.commit()
                return
            if self.worker and self.worker.is_alive():
                return
            self.db.execute('INSERT INTO commands VALUES (?,?,?,?,?,0)', (identity, vm, 'running', '', ''))
            self.db.commit()
            self.worker = threading.Thread(target=self.migrate, args=(identity, vm), daemon=True)
            self.worker.start()

    def update(self, identity, state, destination='', error=''):
        with self.lock, self.db:
            self.db.execute('UPDATE commands SET state=?,destination=?,error=?,acknowledged=0 WHERE id=?',
                            (state, destination, error[:500], identity))

    def migrate(self, identity, vm):
        destination = ''
        try:
            info = json.loads(self.cli('server', 'show', vm, '-f', 'json'))
            if not isinstance(info, dict):
                raise ValueError('Invalid OpenStack server response')
            current = info.get('OS-EXT-SRV-ATTR:host')
            if not isinstance(current, str) or not current:
                raise ValueError('OpenStack did not report the VM host; check admin credentials')
            if info.get('status') != 'ACTIVE' or info.get('OS-EXT-STS:task_state') not in (None, ''):
                raise ValueError('VM must be ACTIVE with no task in progress')
            with self.lock:
                destination = choose_destination(self.inventory(), current)
            self.update(identity, 'running', destination)
            self.cli('server', 'migrate', '--live-migration', '--host', destination, '--wait', vm, timeout=3600)
            info = json.loads(self.cli('server', 'show', vm, '-f', 'json'))
            if (not isinstance(info, dict) or info.get('OS-EXT-SRV-ATTR:host') != destination
                or info.get('status') != 'ACTIVE' or info.get('OS-EXT-STS:task_state') not in (None, '')):
                raise ValueError('Migration did not finish on the selected host; check OpenStack before retrying')
            self.update(identity, 'completed', destination)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.update(identity, 'error', destination, str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--port', type=int, default=2222)
    parser.add_argument('--controller', default=socket.gethostname())
    parser.add_argument('--state-file', type=Path, default=Path('/var/lib/reamer-controller/state.sqlite3'))
    parser.add_argument('--openstack', default='openstack', help='OpenStack CLI executable')
    parser.add_argument('--interval', type=float, default=2)
    args = parser.parse_args()
    if args.interval <= 0 or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', args.controller):
        parser.error('Use a positive interval and a valid controller name')
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    with args.state_file.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        agent = Controller(args.state_file, args.openstack)
        next_discovery = 0
        while True:
            if time.monotonic() >= next_discovery:
                # Inventory refresh never blocks controller heartbeats or command execution.
                next_discovery = time.monotonic() + 60
                def discover():
                    try:
                        agent.discover()
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        print(f'Host discovery failed: {exc}', file=sys.stderr, flush=True)
                threading.Thread(target=discover, daemon=True).start()
            try:
                agent.report(args.host, args.port, args.controller)
            except (OSError, ValueError, KeyError) as exc:
                print(f'Controller report failed: {exc}', file=sys.stderr, flush=True)
            time.sleep(args.interval)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
