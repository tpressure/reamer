import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from controller_agent import Controller, choose_destination
from migration_control import MigrationControl
from migrations import MigrationStore
from server import HeartbeatServer

VM = '37914fc2-9f9a-4979-b3ea-641e1be1d233'
COMMAND = {'id': 'a' * 32, 'vm_uuid': VM}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.agent = Controller(Path(self.temp.name) / 'controller.sqlite')
        self.addCleanup(self.agent.db.close)
        self.agent.hosts, self.agent.hosts_at = ['compute-a', 'compute-b'], time.monotonic()

    def test_ring_two_to_ten_hosts(self):
        for count in (2, 10):
            hosts = [f'compute-{index:02}' for index in range(count)]
            for index, current in enumerate(hosts):
                self.assertEqual(choose_destination(hosts, current), hosts[(index + 1) % count])
        with self.assertRaises(ValueError):
            choose_destination(['only'], 'only')

    def test_discovery_filters_disabled_and_down_hosts_and_expires(self):
        rows = [{'Host': 'up', 'Status': 'enabled', 'State': 'up'},
                {'Host': 'down', 'Status': 'enabled', 'State': 'down'},
                {'Host': 'disabled', 'Status': 'disabled', 'State': 'up'}]
        with patch.object(self.agent, 'cli', return_value=json.dumps(rows)) as cli:
            self.agent.discover()
            self.assertEqual(self.agent.inventory(), ['up'])
            self.agent.inventory()
            self.assertEqual(cli.call_count, 1)
        self.agent.hosts_at -= 121
        self.assertEqual(self.agent.inventory(), [])

    def test_migration_and_duplicate_delivery_use_three_commands(self):
        def show(host):
            return json.dumps({'OS-EXT-SRV-ATTR:host': host, 'status': 'ACTIVE', 'OS-EXT-STS:task_state': None})
        with patch.object(self.agent, 'cli', side_effect=[show('compute-a'), '', show('compute-b')]) as cli:
            self.agent.accept(COMMAND)
            self.agent.worker.join(5)
            self.agent.accept(COMMAND)
            self.assertEqual(cli.call_count, 3)
            self.assertEqual(cli.call_args_list[1].args,
                             ('server', 'migrate', '--live-migration', '--host', 'compute-b', '--wait', VM))
        self.assertEqual(self.agent.db.execute('SELECT state,destination FROM commands').fetchone(), ('completed', 'compute-b'))

    def test_busy_vm_never_submits_migration(self):
        info = {'OS-EXT-SRV-ATTR:host': 'compute-a', 'status': 'ACTIVE', 'OS-EXT-STS:task_state': 'migrating'}
        with patch.object(self.agent, 'cli', return_value=json.dumps(info)) as cli:
            self.agent.accept(COMMAND)
            self.agent.worker.join(5)
            self.assertEqual(cli.call_count, 1)
        self.assertEqual(self.agent.db.execute('SELECT state FROM commands').fetchone()[0], 'error')

    def test_interrupted_agent_does_not_resubmit(self):
        self.agent.db.execute('INSERT INTO commands VALUES (?,?,?,?,?,0)', ('a' * 32, VM, 'running', 'compute-b', ''))
        self.agent.db.commit()
        restarted = Controller(Path(self.temp.name) / 'controller.sqlite')
        self.addCleanup(restarted.db.close)
        with patch.object(restarted, 'cli') as cli:
            restarted.accept(COMMAND)
            cli.assert_not_called()
        self.assertEqual(restarted.db.execute('SELECT state FROM commands').fetchone()[0], 'error')


class MigrationControlTests(unittest.TestCase):
    def setUp(self):
        self.store = MigrationStore(':memory:')
        self.addCleanup(self.store.db.close)
        self.control = MigrationControl(self.store)
        self.report = {'controller': 'controller-1', 'hosts': ['compute-a', 'compute-b'], 'results': []}

    def test_offline_controller_and_missing_uuid_disable_requests(self):
        with self.assertRaises(ValueError):
            self.control.request(VM)
        self.control.report(self.report)
        with self.assertRaises(ValueError):
            self.control.request(None)
        self.control.seen -= 16
        with self.assertRaises(ValueError):
            self.control.request(VM)

    def test_queue_duplicate_clicks_restart_and_acknowledgements(self):
        self.control.report(self.report)
        self.control.request(VM)
        with self.assertRaises(ValueError):
            self.control.request(VM)
        command = self.control.report(self.report)['commands'][0]
        restarted = MigrationControl(self.store)
        self.assertEqual(restarted.report(self.report)['commands'], [command])
        report = dict(self.report, results=[{'id': command['id'], 'state': 'completed', 'destination': 'compute-b'}])
        self.assertEqual(restarted.report(report)['acknowledged'], [command['id']])
        self.assertEqual(restarted.report(report)['commands'], [])
        # Delayed running reports must never roll a terminal result back.
        report['results'][0]['state'] = 'running'
        restarted.report(report)
        self.assertEqual(restarted.status(VM)['state'], 'completed')
        unknown = dict(self.report, results=[{'id': 'b' * 32, 'state': 'completed'}])
        self.assertEqual(restarted.report(unknown)['acknowledged'], ['b' * 32])

    def test_validation_does_not_replace_good_inventory(self):
        self.control.report(self.report)
        with self.assertRaises(ValueError):
            self.control.report(dict(self.report, hosts=['--bad;host']))
        self.assertTrue(self.control.status(VM)['available'])
        with self.assertRaises(ValueError):
            self.control.report(dict(self.report, controller='other'))

    def test_success_message_expires_without_restarting_on_replay(self):
        server = HeartbeatServer('127.0.0.1', 0)
        self.addCleanup(server.migrations.db.close)
        server.migration_control = self.control
        client = {'client_id': 'guest', 'vm_uuid': VM}
        self.control.report(self.report)
        self.control.request(VM)
        command = self.control.report(self.report)['commands'][0]
        report = dict(self.report, results=[{'id': command['id'], 'state': 'completed', 'destination': 'compute-b'}])
        with patch('migration_control.time.time', return_value=1000):
            self.control.report(report)
        with patch('server.time.time', return_value=1009.9):
            self.assertIn('Migrated to compute-b', server.render_migrate_control(client))
        with patch('server.time.time', return_value=1010):
            self.assertNotIn('Migrated to', server.render_migrate_control(client))
            server.migration_control = MigrationControl(self.store)
            server.migration_control.report(report)
            self.assertNotIn('Migrated to', server.render_migrate_control(client))
            self.assertEqual(server.migration_control.status(VM)['completed_at'], 1000)
