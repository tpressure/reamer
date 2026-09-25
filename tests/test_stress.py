import argparse
import io
import json
import os
import signal
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from client import StressManager, main
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
        read_fd, self.stats_fd = os.pipe()
        self.process.stdout = os.fdopen(read_fd, 'rb')
        self.addCleanup(self.process.stdout.close)
        self.addCleanup(os.close, self.stats_fd)
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
        with patch('client.time.monotonic', return_value=10):
            self.apply('stop', 'two')
            self.assertEqual(self.manager.snapshot()['cpu']['state'], 'stopping')
        self.kill.assert_any_call(12345, signal.SIGTERM)
        self.assertEqual(self.kill.call_count, 1)
        with patch('client.time.monotonic', return_value=12):
            self.assertEqual(self.manager.snapshot()['cpu']['state'], 'stopping')
        self.kill.assert_any_call(12345, signal.SIGKILL)
        with patch('client.time.monotonic', return_value=50):
            self.assertEqual(self.manager.snapshot()['cpu']['state'], 'stopping')
        self.assertEqual(self.kill.call_count, 2)
        self.process.wait.assert_not_called()
        self.process.poll.return_value = -signal.SIGKILL
        self.assertEqual(self.manager.snapshot()['cpu']['state'], 'idle')
        self.assertEqual(self.manager.snapshot()['cpu']['error'], '')
        calls = self.kill.call_count
        self.apply('stop', 'two')
        self.assertEqual(self.kill.call_count, calls)

    def test_slow_requested_sigterm_is_not_an_error(self):
        self.apply('start', 'one', 'memory')
        self.apply('stop', 'two', 'memory')
        self.process.poll.return_value = -signal.SIGTERM
        state = self.manager.snapshot()['memory']
        self.assertEqual(state['state'], 'idle')
        self.assertEqual(state['error'], '')

    def test_unsolicited_sigterm_is_still_an_error(self):
        self.apply('start', 'one', 'memory')
        self.process.poll.return_value = -signal.SIGTERM
        self.assertEqual(self.manager.snapshot()['memory']['state'], 'error')

    def test_duplicate_stop_does_not_postpone_escalation_or_allow_restart(self):
        self.apply('start', 'one')
        with patch('client.time.monotonic', return_value=10):
            self.apply('stop', 'two')
        with patch('client.time.monotonic', return_value=11):
            self.apply('stop', 'three')
            self.apply('start', 'four')
        self.assertEqual(self.manager.stop_deadlines['cpu'], 12)
        self.assertEqual(self.manager.snapshot()['cpu']['state'], 'stopping')
        self.assertEqual(self.spawn.call_count, 1)

    def test_heartbeats_continue_through_long_memory_teardown(self):
        clock = [0.0]
        heartbeats = []
        args = argparse.Namespace(host='localhost', port=12345, client_id='test', interval=0.5, retry_delay=0)
        replies = [
            {'type': 'heartbeat_ack', 'commands': [{'id': 'start', 'kind': 'memory', 'action': 'start'}]},
            {'type': 'heartbeat_ack', 'commands': [{'id': 'stop', 'kind': 'memory', 'action': 'stop'}]},
        ] + [{'type': 'heartbeat_ack', 'commands': []}] * 20
        connection = Mock()
        socket_context = Mock()
        socket_context.__enter__ = Mock(return_value=connection)
        socket_context.__exit__ = Mock(return_value=False)
        connection.makefile.return_value = io.StringIO(''.join(json.dumps(reply) + '\n' for reply in replies))

        def send(payload):
            if len(heartbeats) == 16:
                raise KeyboardInterrupt
            heartbeats.append((clock[0], json.loads(payload)))

        def sleep(seconds):
            clock[0] += seconds

        connection.sendall.side_effect = send
        self.process.poll.side_effect = lambda: -signal.SIGTERM if clock[0] >= 6 else None
        # Any process wait in the heartbeat loop is a regression, even if quick.
        self.process.wait.side_effect = AssertionError('Heartbeat loop waited for process exit')
        with patch('client.parse_args', return_value=args), patch('client.StressManager', return_value=self.manager), patch('client.socket.create_connection', return_value=socket_context), patch('client.time.monotonic', side_effect=lambda: clock[0]), patch('client.time.sleep', side_effect=sleep):
            with self.assertRaises(KeyboardInterrupt):
                main()
        self.assertEqual([timestamp for timestamp, _ in heartbeats], [i * 0.5 for i in range(16)])
        for _, payload in heartbeats[2:12]:
            self.assertEqual(payload['stress']['memory']['state'], 'stopping')
        self.assertEqual(heartbeats[-1][1]['stress']['memory']['state'], 'idle')
        self.assertTrue(all(not payload['stress']['memory']['error'] for _, payload in heartbeats))
        self.process.wait.assert_not_called()

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

    def test_bandwidth_handles_partial_lines_latest_sample_and_expiration(self):
        self.apply('start', 'one', 'memory')
        self.assertIsNone(self.manager.snapshot()['memory']['write_mibps'])
        os.write(self.stats_fd, b'Running 4 threads\n2026-09-25T12:00:00.000+0200 read_mibps:0.00 write_mibps:2048.')
        self.assertIsNone(self.manager.snapshot()['memory']['write_mibps'])
        os.write(self.stats_fd, b'50\n2026-09-25T12:00:01.000+0200 read_mibps:0.00 write_mibps:3072.25\n')
        with patch('client.time.monotonic', return_value=10):
            self.assertEqual(self.manager.snapshot()['memory']['write_mibps'], 3072.25)
        with patch('client.time.monotonic', return_value=16):
            self.assertIsNone(self.manager.snapshot()['memory']['write_mibps'])
        self.process.wait.assert_not_called()

    def test_bandwidth_rejects_invalid_output_and_clears_on_stop(self):
        self.apply('start', 'one', 'memory')
        os.write(self.stats_fd, b'write_mibps:nan\nwrite_mibps:inf\nwrite_mibps:-1\nwrite_mibps:12oops\n')
        self.assertIsNone(self.manager.snapshot()['memory']['write_mibps'])
        os.write(self.stats_fd, b'write_mibps:128.00\n')
        self.assertEqual(self.manager.snapshot()['memory']['write_mibps'], 128)
        self.apply('stop', 'two', 'memory')
        os.write(self.stats_fd, b'write_mibps:256.00\n')
        self.assertIsNone(self.manager.snapshot()['memory']['write_mibps'])
        self.process.poll.return_value = -signal.SIGTERM
        self.assertIsNone(self.manager.snapshot()['memory']['write_mibps'])
        self.assertTrue(self.process.stdout.closed)
        self.assertEqual(self.manager.bandwidth_buffer, b'')


class StressSizingTests(unittest.TestCase):
    @patch('client.os.sched_getaffinity', return_value=set(range(8)))
    @patch('client.Path.read_text', return_value='MemTotal: 8388608 kB\nMemAvailable: 1000 kB\n')
    def test_all_cpu_workers_and_half_total_memory(self, *_):
        self.assertEqual(StressManager.command('cpu'), ['stress-ng', '--cpu', '8', '--cpu-load', '100', '--timeout', '0'])
        self.assertEqual(StressManager.command('memory'), ['memtouch', '--num_threads', '6', '--thread_mem', '682', '--rw_ratio', '100', '--stat_file', '/dev/stdout', '--stat_ival', '1000'])

    @patch('client.os.sched_getaffinity', return_value={0})
    @patch('client.Path.read_text', return_value='MemTotal: 1048576 kB\n')
    def test_single_vcpu_uses_one_worker(self, *_):
        self.assertEqual(StressManager.command('memory')[2:5], ['1', '--thread_mem', '512'])

    @patch('client.os.sched_getaffinity', return_value=set(range(7)))
    @patch('client.Path.read_text', return_value='MemTotal: 1048576 kB\n')
    def test_odd_counts_round_down(self, *_):
        self.assertEqual(StressManager.command('memory')[2:5], ['5', '--thread_mem', '102'])

    @patch('client.os.sched_getaffinity', return_value=set(range(16)))
    @patch('client.Path.read_text', return_value='MemTotal: 134217728 kB\n')
    def test_128_gib_budget_is_shared_across_twelve_workers(self, *_):
        command = StressManager.command('memory')
        workers, per_worker_mib = int(command[2]), int(command[4])
        self.assertEqual(workers, 12)
        self.assertEqual(per_worker_mib, 5461)
        budget_mib = 64 * 1024
        self.assertLessEqual(workers * per_worker_mib, budget_mib)
        self.assertLess(budget_mib - workers * per_worker_mib, workers)


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

    def test_stopping_state_survives_ack_and_server_restart(self):
        self.server.request_stress('client-a', 'cpu', 'stop')
        command = self.server.commands_for('client-a')[0]
        states = report('stopping', command_id=command['id'])
        self.heartbeat(states)
        self.assertEqual(self.server.commands_for('client-a'), [])
        for server in (self.server, HeartbeatServer('127.0.0.1', 0)):
            with patch.object(server, 'print_clients_locked'):
                server.record_heartbeat('client-a', 'peer', {}, states, True)
            self.assertIn('aria-disabled="true">Stopping CPU…</button>', server.render_status_content())
            with self.assertRaises(ValueError):
                server.request_stress('client-a', 'cpu', 'start')

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

    def test_bandwidth_display_and_validation(self):
        states = report(memory='running')
        for value, reading in [(2048, 'Write 2.00 GiB/s'), (12.5, 'Write 12.5 MiB/s'), (0, 'Write 0.0 MiB/s'), (None, 'Write —')]:
            states['memory']['write_mibps'] = value
            self.heartbeat(states)
            self.assertIn(reading, self.server.render_status_content())
        for invalid in [True, -1, float('nan'), float('inf'), 10**1000, '<script>', {}, []]:
            states['memory']['write_mibps'] = invalid
            self.heartbeat(states)
            self.assertIsNone(self.server.clients['client-a']['stress']['memory']['write_mibps'])
        states['memory']['write_mibps'] = 2048
        self.heartbeat(states)
        self.server.clients['client-a']['last_heartbeat'] = datetime.now(timezone.utc) - timedelta(seconds=60)
        self.assertIn('Write —', self.server.render_status_content())
        self.assertNotIn('Write 2.00', self.server.render_status_content())
        for state in ['idle', 'stopping', 'error']:
            states['memory']['state'] = state
            self.heartbeat(states)
            self.assertNotIn('stress-bandwidth', self.server.render_status_content())


if __name__ == '__main__':
    unittest.main()
