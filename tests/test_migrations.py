import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from client import guest_uuid
from compute_reporter import Reporter
from migrations import MigrationStore
from server import HeartbeatServer

VM = '37914fc2-9f9a-4979-b3ea-641e1be1d233'
OTHER = '12345678-1234-1234-1234-123456789012'
FIXTURE = Path(__file__).parent / 'fixtures' / 'migration-source.log'


def line(at, text):
    return f'"cloud-hypervisor: {at}Z: <vmm> INFO:test.rs:1 -- {text}"\n'


def identity(vm=VM):
    return line('2026-09-23T10:00:00', f'system_uuid: Some("{vm}")')


def completed(at='2026-09-26T00:00:00', downtime=42):
    return line(at, f'Migration completed after 0.3s with a downtime of {downtime}ms (goal was 300ms)')


def event(at, name):
    return line(at, f'Event: source = vm event = {name} ')


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.path = self.logs / 'instance-00000064.log'
        self.reporter = Reporter(self.logs, self.root / 'reporter.sqlite')
        self.store = MigrationStore(self.root / 'server.sqlite')
        self.addCleanup(self.reporter.db.close)
        self.addCleanup(self.store.db.close)

    def report(self, node='compute-a'):
        return {'node': node,
                'events': [dict(zip(('vm_uuid', 'at', 'downtime_ms'), row)) for row in self.reporter.db.execute('SELECT * FROM events')],
                'placements': [dict(zip(('vm_uuid', 'instance', 'active_at', 'ended_at'), row)) for row in self.reporter.db.execute('SELECT * FROM placements')]}

    def test_supplied_log_counts_only_sender_completions(self):
        self.path.write_text(FIXTURE.read_text())
        self.reporter.scan()
        self.store.ingest(self.report())
        result = self.store.summary(VM)
        self.assertEqual([result[key] for key in ('count', 'min_ms', 'avg_ms', 'max_ms', 'last_ms')], [4, 58, 81.5, 111, 111])
        self.assertIsNone(result['node'])  # Final shutdown in supplied log.
        self.assertFalse(self.reporter.scan())

    def test_two_nodes_late_reports_and_deduplication(self):
        self.path.write_text(identity() + event('2026-09-23T10:00:01', 'booted') + completed())
        self.reporter.scan()
        old = self.report()
        self.store.ingest(old)
        newer = {'node': 'compute-b', 'events': old['events'], 'placements': [{'vm_uuid': VM, 'instance': 'instance-64', 'active_at': '2026-09-26T00:00:01Z', 'ended_at': None}]}
        self.store.ingest(newer)
        self.store.ingest(old)
        result = self.store.summary(VM)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['node'], 'compute-b')
        newer['events'] = [{'vm_uuid': VM, 'at': '2026-09-25T00:00:00Z', 'downtime_ms': 80}]
        self.store.ingest(newer)
        self.assertEqual(self.store.summary(VM)['last_ms'], 42)
        self.assertEqual(self.store.summary(VM)['avg_ms'], 61)

    def test_fragmented_lines_rotation_truncation_and_restart(self):
        self.path.write_text(identity() + completed().rstrip('\n'))
        self.reporter.scan()
        self.assertEqual(self.report()['events'], [])
        with self.path.open('a') as stream:
            stream.write('\n')
        self.reporter.scan()
        self.path.rename(self.logs / 'instance-00000064.log.1')
        self.path.write_text(identity() + completed('2026-09-27T00:00:00', 10))
        self.reporter.scan()
        self.path.write_text(identity() + completed('2026-09-28T00:00:00', 20))  # Same-size copytruncate/rewrite.
        self.reporter.scan()
        again = Reporter(self.logs, self.root / 'reporter.sqlite')
        self.addCleanup(again.db.close)
        again.scan()
        self.assertEqual(len(self.report()['events']), 3)

    def test_reused_instance_name_keeps_uuids_separate(self):
        self.path.write_text(identity() + completed() + identity(OTHER) + completed())
        self.reporter.scan()
        self.store.ingest(self.report())
        self.assertEqual(self.store.summary(VM)['count'], 1)
        self.assertEqual(self.store.summary(OTHER)['count'], 1)

    def test_rotation_continues_without_repeating_vm_identity(self):
        self.path.write_text(identity() + completed())
        self.reporter.scan()
        self.path.rename(self.logs / 'instance-00000064.log.1')
        self.path.write_text(completed('2026-09-27T00:00:00', 20))
        self.reporter.scan()
        self.assertEqual(len(self.report()['events']), 2)
        fresh = Reporter(self.logs, self.root / 'fresh.sqlite')
        self.addCleanup(fresh.db.close)
        fresh.scan()
        self.assertEqual(fresh.db.execute('SELECT count(*) FROM events').fetchone()[0], 2)

    def test_receiver_resumes_and_announcers_are_not_downtime(self):
        self.path.write_text(identity() + event('2026-09-24T00:00:00', 'resumed') + line('2026-09-24T00:00:01', 'Post migration announce (async): 1/4') + event('2026-09-24T00:00:02', 'migration-receive-finished'))
        self.reporter.scan()
        self.store.ingest(self.report())
        stats = self.store.summary(VM)
        self.assertEqual(stats['count'], 0)
        self.assertEqual(stats['node'], 'compute-a')

    def test_database_survives_server_restart(self):
        self.path.write_text(identity() + completed())
        self.reporter.scan()
        self.store.ingest(self.report())
        restarted = MigrationStore(self.root / 'server.sqlite')
        self.addCleanup(restarted.db.close)
        restarted.ingest(self.report())
        self.assertEqual(restarted.summary(VM)['count'], 1)
        self.assertNotEqual(restarted.epoch, self.store.epoch)

    def test_invalid_report_is_atomic(self):
        for value in [-1, True, '80', float('nan'), 2**54]:
            report = {'node': 'n', 'events': [{'vm_uuid': VM, 'at': '2026-09-23T00:00:00Z', 'downtime_ms': 12}, {'vm_uuid': VM, 'at': '2026-09-24T00:00:00Z', 'downtime_ms': value}], 'placements': []}
            with self.assertRaises(ValueError):
                self.store.ingest(report)
            self.assertIsNone(self.store.summary(VM))
        for bad in [None, [], {}, {'node': 'n', 'events': [{}], 'placements': []}]:
            with self.assertRaises(ValueError):
                self.store.ingest(bad)

    def test_stale_node_is_marked(self):
        self.path.write_text(identity() + event('2026-09-23T10:00:01', 'booted'))
        self.reporter.scan()
        with patch('migrations.time.time', return_value=0):
            self.store.ingest(self.report())
        with patch('migrations.time.time', return_value=31):
            self.assertTrue(self.store.summary(VM)['node_stale'])

    def test_guest_identity_and_page_matching(self):
        with patch('client.Path.read_text', return_value=VM.upper() + '\n'):
            self.assertEqual(guest_uuid(), VM)
        with patch('client.Path.read_text', side_effect=PermissionError):
            self.assertIsNone(guest_uuid())
        server = HeartbeatServer('127.0.0.1', 0)
        self.addCleanup(server.migrations.db.close)
        self.path.write_text(identity() + completed() + event('2026-09-26T00:00:01', 'migration-receive-finished'))
        self.reporter.scan()
        server.migrations.ingest(self.report('<node>'))
        with patch.object(server, 'print_clients_locked'):
            server.record_heartbeat('random-guest-name', 'peer', {}, vm_uuid=VM)
        page = server.render_status_page()
        self.assertIn('Node <strong>&lt;node&gt;</strong>', page)
        self.assertIn('Last downtime <strong>42 ms</strong>', page)
        self.assertIn('Show migration details', page)
        self.assertIn('.migration-row { display: none; }', page)

    def test_real_http_report_retry_epoch_and_validation(self):
        server = HeartbeatServer('127.0.0.1', 0, http_port=0)
        self.addCleanup(server.migrations.db.close)
        servers = []
        def capture(address, handler):
            httpd = ThreadingHTTPServer(address, handler)
            servers.append(httpd)
            return httpd
        with patch('server.ThreadingHTTPServer', side_effect=capture):
            server.start_http_server()
        httpd = servers[0]
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.path.write_text(identity() + completed())
        self.reporter.scan()
        for _ in range(3):
            self.reporter.upload('127.0.0.1', httpd.server_port, 'compute-a')
        self.assertEqual(server.migrations.summary(VM)['count'], 1)
        server.migrations.db.execute('DELETE FROM migrations')
        server.migrations.db.commit()
        server.migrations.epoch = 'new-server'
        self.reporter.upload('127.0.0.1', httpd.server_port, 'compute-a')
        self.reporter.upload('127.0.0.1', httpd.server_port, 'compute-a')
        self.assertEqual(server.migrations.summary(VM)['count'], 1)
        request = Request(f'http://127.0.0.1:{httpd.server_port}/compute-report', data=b'{bad', headers={'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
