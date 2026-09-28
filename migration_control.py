"""Persistent migration requests and the controller's cached host inventory."""
import re
import secrets
import time

from migrations import vm_identity


class MigrationControl:
    def __init__(self, store):
        self.db, self.lock = store.db, store.lock
        self.controller = None
        self.seen = 0
        self.hosts = []
        with self.lock, self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS migration_requests (
                id TEXT PRIMARY KEY, vm_uuid TEXT, controller TEXT, state TEXT,
                destination TEXT, error TEXT, created REAL)''')
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(migration_requests)')}
            if 'completed_at' not in columns:
                self.db.execute('ALTER TABLE migration_requests ADD COLUMN completed_at REAL')
            self.db.execute('CREATE INDEX IF NOT EXISTS migration_requests_vm ON migration_requests(vm_uuid, created DESC)')

    def report(self, report):
        if not isinstance(report, dict):
            raise ValueError('Expected a controller report')
        controller, hosts, results = (report.get(key) for key in ('controller', 'hosts', 'results'))
        if not isinstance(controller, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', controller):
            raise ValueError('Invalid controller identity')
        if not isinstance(hosts, list) or len(hosts) > 100 or any(
            not isinstance(host, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,255}', host) for host in hosts
        ):
            raise ValueError('Invalid compute host list')
        if not isinstance(results, list) or len(results) > 128:
            raise ValueError('Too many migration results')
        for result in results:
            if (not isinstance(result, dict) or not isinstance(result.get('id'), str)
                or result.get('state') not in ('running', 'completed', 'error')
                or any(not isinstance(result.get(key, ''), str) or len(result.get(key, '')) > 500
                       for key in ('destination', 'error'))):
                raise ValueError('Invalid migration result')
        with self.lock, self.db:
            if self.controller and self.controller != controller and time.monotonic() - self.seen < 15:
                raise ValueError('Another controller agent is connected')
            self.controller, self.hosts, self.seen = controller, sorted(set(hosts)), time.monotonic()
            acknowledged = []
            for result in results:
                # An unknown ID can be forgotten after a server database reset.
                # Only matching, controller-owned requests are ever updated.
                acknowledged.append(result['id'])
                row = self.db.execute('SELECT state FROM migration_requests WHERE id=? AND controller=?',
                                      (result['id'], controller)).fetchone()
                if not row:
                    continue
                if row[0] in ('queued', 'running'):
                    self.db.execute('UPDATE migration_requests SET state=?, destination=?, error=?, completed_at=? WHERE id=?',
                                    (result['state'], result.get('destination', ''), result.get('error', ''),
                                     time.time() if result['state'] == 'completed' else None, result['id']))
            commands = [dict(zip(('id', 'vm_uuid'), row)) for row in self.db.execute(
                "SELECT id, vm_uuid FROM migration_requests WHERE controller=? AND state IN ('queued','running') ORDER BY created LIMIT 1",
                (controller,))]
            return {'commands': commands, 'acknowledged': acknowledged}

    def status(self, identity):
        with self.lock:
            row = self.db.execute('SELECT id,state,destination,error,controller,completed_at FROM migration_requests WHERE vm_uuid=? ORDER BY created DESC LIMIT 1', (identity,)).fetchone()
            status = dict(zip(('id', 'state', 'destination', 'error', 'controller', 'completed_at'), row)) if row else {}
            status['available'] = bool(self.controller and time.monotonic() - self.seen < 15 and len(self.hosts) >= 2)
            return status

    def request(self, identity):
        if not vm_identity(identity):
            raise ValueError('A VM UUID is required for migration')
        with self.lock, self.db:
            if not self.controller or time.monotonic() - self.seen >= 15 or len(self.hosts) < 2:
                raise ValueError('Wait for the controller and at least two healthy compute hosts')
            if self.db.execute("SELECT 1 FROM migration_requests WHERE vm_uuid=? AND state IN ('queued','running')", (identity,)).fetchone():
                raise ValueError('A migration is already queued or running for this VM')
            self.db.execute('INSERT INTO migration_requests (id,vm_uuid,controller,state,destination,error,created) VALUES (?,?,?,?,?,?,?)',
                            (secrets.token_hex(16), identity, self.controller, 'queued', '', '', time.time()))
