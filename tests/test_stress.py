import signal
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from client import StressManager
from server import HeartbeatServer


def report(cpu='idle', memory='idle', command_id=''):
    return {
        'cpu': {'state': cpu, 'last_command': command_id, 'error': ''},
        'memory': {'state': memory, 'last_command': '', 'error': ''},
    }


class StressManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = StressManager()
        self.process = Mock(pid=12345)
        self.process.poll.return_value = None
        self.process.wait.return_value = 0
        self.spawn = patch('client.subprocess.Popen', return_value=self.process).start()
        self.kill = patch('client.os.killpg').start()
        self.command = patch.object(self.manager, 'command', return_value=['dummy-stress']).start()
        self.addCleanup(patch.stopall)

    def apply(self, action, command_id, kind='cpu'):
        self.manager.apply({'id': command_id, 'kind': kind, 'action': action})

    def test_start_is_idempotent_and_status_survives_reconnect(self):
        self.apply('start', 'one')
        self.apply('start', 'one')
        self.apply('start', 'two')
        self.assertEqual(self.spawn.call_count, 1)
        self.assertTrue(self.spawn.call_args.kwargs['start_new_session'])
        for _ in range(3):
            self.assertEqual(self.manager.snapshot()['cpu']['state'], 'running')
        self.assertEqual(self.manager.snapshot()['cpu']['last_command'], 'two')

    def test_stop_terminates_workers_and_escalates(self):
        self.apply('start', 'one')
        self.process.wait.side_effect = [subprocess.TimeoutExpired('dummy', 2), 0]
        self.apply('stop', 'two')
        self.kill.assert_any_call(12345, signal.SIGTERM)
        self.kill.assert_any_call(12345, signal.SIGKILL)
        self.assertEqual(self.manager.snapshot()['cpu']['state'], 'idle')
        calls = self.kill.call_count
        self.apply('stop', 'two')
        self.assertEqual(self.kill.call_count, calls)

    def test_failures_are_reported_and_retry_is_explicit(self):
        self.spawn.side_effect = FileNotFoundError('stress-ng missing')
        self.apply('start', 'one')
        state = self.manager.snapshot()['cpu']
        self.assertEqual(state['state'], 'error')
        self.assertIn('missing', state['error'])
        self.apply('start', 'one')
        self.assertEqual(self.spawn.call_count, 1)
        self.spawn.side_effect = None
        self.apply('start', 'two')
        self.assertEqual(self.manager.snapshot()['cpu']['state'], 'running')
        self.process.poll.return_value = 9
        self.assertEqual(self.manager.snapshot()['cpu']['state'], 'error')
        self.assertNotIn('cpu', self.manager.processes)

    def test_shutdown_stops_both_tests(self):
        self.apply('start', 'one')
        self.apply('start', 'two', 'memory')
        self.manager.close()
        self.assertEqual(self.manager.processes, {})
        self.assertEqual(self.process.wait.call_count, 2)

    def test_arbitrary_commands_are_ignored(self):
        for command in [None, [], {'id': 'x', 'kind': 'shell', 'action': 'start'}, {'id': 'x', 'kind': 'cpu', 'action': 'exec'}, {'id': '', 'kind': 'cpu', 'action': 'start'}]:
            self.manager.apply(command)
        self.spawn.assert_not_called()


class StressSizingTests(unittest.TestCase):
    @patch('client.os.sched_getaffinity', return_value=set(range(8)))
    @patch('client.Path.read_text', return_value='MemTotal: 8388608 kB\nMemAvailable: 1000 kB\n')
    def test_all_cpu_workers_and_half_total_memory(self, *_):
        self.assertEqual(StressManager.command('cpu'), ['stress-ng', '--cpu', '8', '--cpu-load', '100', '--timeout', '0'])
        self.assertEqual(StressManager.command('memory'), ['memtouch', '--num_threads', '4', '--thread_mem', '1024', '--rw_ratio', '50'])

    @patch('client.os.sched_getaffinity', return_value={0})
    @patch('client.Path.read_text', return_value='MemTotal: 1048576 kB\n')
    def test_single_vcpu_uses_one_worker(self, *_):
        self.assertEqual(StressManager.command('memory')[2:5], ['1', '--thread_mem', '512'])

    @patch('client.os.sched_getaffinity', return_value=set(range(7)))
    @patch('client.Path.read_text', return_value='MemTotal: 1048576 kB\n')
    def test_odd_counts_round_down(self, *_):
        self.assertEqual(StressManager.command('memory')[2:5], ['3', '--thread_mem', '170'])


class StressProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = HeartbeatServer('127.0.0.1', 0)
        patch.object(self.server, 'print_clients_locked').start()
        self.addCleanup(patch.stopall)
        self.heartbeat()

    def heartbeat(self, states=None):
        self.server.record_heartbeat('client-a', 'peer', {}, states or report(), True)

    def test_commands_repeat_until_acknowledged(self):
        self.server.request_stress('client-a', 'cpu', 'start')
        command = self.server.commands_for('client-a')[0]
        self.assertEqual(self.server.commands_for('client-a'), [command])
        self.heartbeat(report('running', command_id=command['id']))
        self.assertEqual(self.server.commands_for('client-a'), [])

    def test_stop_replaces_pending_start_and_old_ack_cannot_clear_stop(self):
        self.server.request_stress('client-a', 'cpu', 'start')
        old = self.server.commands_for('client-a')[0]
        self.server.request_stress('client-a', 'cpu', 'stop')
        self.heartbeat(report('running', command_id=old['id']))
        self.assertEqual(self.server.commands_for('client-a')[0]['action'], 'stop')

    def test_running_report_restores_controls_after_server_restart(self):
        new_server = HeartbeatServer('127.0.0.1', 0)
        with patch.object(new_server, 'print_clients_locked'):
            new_server.record_heartbeat('client-a', 'peer', {}, report('running', 'running'), True)
        html = new_server.render_status_content()
        self.assertIn('>Stop CPU</button>', html)
        self.assertIn('>Stop memory</button>', html)
        new_server.request_stress('client-a', 'memory', 'stop')
        self.assertEqual(new_server.commands_for('client-a')[0]['kind'], 'memory')
        self.assertEqual(new_server.commands_for('client-a')[0]['action'], 'stop')

    def test_stale_client_can_be_stopped_but_not_started(self):
        self.server.clients['client-a']['last_heartbeat'] = datetime.now(timezone.utc) - timedelta(seconds=60)
        with self.assertRaises(ValueError):
            self.server.request_stress('client-a', 'cpu', 'start')
        self.server.request_stress('client-a', 'cpu', 'stop')
        self.assertEqual(self.server.commands_for('client-a')[0]['action'], 'stop')

    def test_old_starts_expire_but_stops_survive_outage(self):
        with patch('server.time.monotonic', return_value=10):
            self.server.request_stress('client-a', 'cpu', 'start')
            self.server.request_stress('client-a', 'memory', 'stop')
        with patch('server.time.monotonic', return_value=41):
            commands = self.server.commands_for('client-a')
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]['action'], 'stop')
        self.assertIn('expired', self.server.clients['client-a']['control_error'])

    def test_legacy_client_controls_are_disabled_and_errors_are_escaped(self):
        self.server.record_heartbeat('old', 'peer', {})
        with self.assertRaises(ValueError):
            self.server.request_stress('old', 'cpu', 'start')
        states = report('error')
        states['cpu']['error'] = '<script>bad</script>'
        self.heartbeat(states)
        html = self.server.render_status_content()
        self.assertIn('Upgrade this client', html)
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', html)
        self.assertNotIn('<script>', html)


if __name__ == '__main__':
    unittest.main()
