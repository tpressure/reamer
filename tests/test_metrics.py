import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client import SystemMetrics
from server import HeartbeatServer


class SystemMetricsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.proc = Path(self.directory.name)
        self.metrics = SystemMetrics(self.proc)

    def write_cpu(self, fields):
        (self.proc / 'stat').write_text(f'cpu  {fields}\ncpu0 0 0 0 0\n')

    def test_cpu_delta_and_guest_counters(self):
        self.write_cpu('100 20 30 400 50 10 20 0 80 10')
        self.assertIsNone(self.metrics.sample()['cpu_percent'])
        # Total +100, idle +60: 40% busy; guest increases must not count twice.
        self.write_cpu('120 20 50 450 60 10 20 0 100 10')
        self.assertEqual(self.metrics.sample()['cpu_percent'], 40.0)
        self.assertIsNone(self.metrics.sample()['cpu_percent'])

    def test_cpu_counter_reset_reestablishes_baseline(self):
        self.write_cpu('100 0 0 100 0 0 0 0')
        self.metrics.sample()
        self.write_cpu('10 0 0 10 0 0 0 0')
        self.assertIsNone(self.metrics.sample()['cpu_percent'])
        self.write_cpu('20 0 0 20 0 0 0 0')
        self.assertEqual(self.metrics.sample()['cpu_percent'], 50.0)

    def test_memory_uses_available_including_reclaimable(self):
        (self.proc / 'meminfo').write_text('MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 650 kB\n')
        self.assertEqual(self.metrics.sample()['memory_percent'], 35.0)

    def test_missing_or_invalid_counters_do_not_stop_heartbeats(self):
        self.assertEqual(self.metrics.sample(), {'cpu_percent': None, 'memory_percent': None})
        self.write_cpu('broken')
        (self.proc / 'meminfo').write_text('MemTotal: 0 kB\nMemAvailable: 1 kB\n')
        self.assertEqual(self.metrics.sample(), {'cpu_percent': None, 'memory_percent': None})


class MetricsHistoryTests(unittest.TestCase):
    def setUp(self):
        self.server = HeartbeatServer('127.0.0.1', 0)
        quiet = patch.object(self.server, 'print_clients_locked')
        quiet.start()
        self.addCleanup(quiet.stop)

    def receive(self, second, metrics, client_id='client-a'):
        with patch('server.time.monotonic', return_value=second):
            self.server.record_heartbeat(client_id, '127.0.0.1:1000', metrics)

    def test_fast_samples_are_aggregated_and_bounded(self):
        for second in range(100):
            for value in (10, 20, 30):
                self.receive(second, {'cpu_percent': value})
        history = self.server.get_clients_snapshot(99)[0]['history']
        self.assertEqual(len(history), 60)
        self.assertEqual(history[0]['second'], 40)
        self.assertEqual(history[-1]['cpu_percent'], (60, 3))
        chart = self.server.render_histogram(history, 'cpu_percent', 'CPU', 99)
        self.assertEqual(chart.count('height: 20.00%'), 12)

    def test_expiry_during_disconnect_and_snapshot_isolation(self):
        self.receive(100, {'cpu_percent': 40})
        snapshot = self.server.get_clients_snapshot(159)[0]['history']
        self.assertEqual(len(snapshot), 1)
        snapshot[0]['cpu_percent'] = (99, 1)
        self.assertEqual(self.server.get_clients_snapshot(159)[0]['history'][0]['cpu_percent'], (40, 1))
        self.assertEqual(self.server.get_clients_snapshot(160)[0]['history'], [])

    def test_invalid_metrics_are_independent_and_legacy_clients_work(self):
        for value in (None, True, '10', -1, 101, float('nan'), float('inf'), 10**400, [], {}):
            self.receive(100, {'cpu_percent': value, 'memory_percent': 50})
        self.receive(101, None, 'old-client')
        self.receive(101, ['invalid'], 'old-client')
        snapshots = self.server.get_clients_snapshot(101)
        self.assertNotIn('cpu_percent', snapshots[0]['history'][0])
        self.assertEqual(snapshots[0]['history'][0]['memory_percent'], (500, 10))
        chart = self.server.render_histogram(snapshots[1]['history'], 'cpu_percent', 'CPU', 101)
        self.assertIn('No samples', chart)
        self.assertNotIn('<i ', chart)

    def test_weighted_buckets_zero_full_scale_and_gaps(self):
        self.receive(100, {'cpu_percent': 0, 'memory_percent': 100})
        self.receive(101, {'cpu_percent': 60})
        self.receive(101, {'cpu_percent': 60})
        history = self.server.get_clients_snapshot(104)[0]['history']
        cpu = self.server.render_histogram(history, 'cpu_percent', 'CPU', 104)
        memory = self.server.render_histogram(history, 'memory_percent', 'Memory', 104)
        self.assertIn('height: 40.00%', cpu)
        self.assertIn('60.0% · 3s ago', cpu)
        self.assertEqual(cpu.count('class="history-bar"'), 12)
        self.assertEqual(cpu.count('<i '), 1)
        self.assertIn('height: 100.00%', memory)
        zero = self.server.render_histogram(history[:1], 'cpu_percent', 'CPU', 104)
        self.assertIn('height: 0.00%', zero)
        self.assertNotIn('No samples', zero)

    def test_page_has_two_charts_per_client_and_safe_names(self):
        self.receive(100, {'cpu_percent': 30}, '<script>unsafe</script>')
        self.receive(100, {}, 'legacy')
        with patch('server.time.monotonic', return_value=100):
            html = self.server.render_status_content()
        self.assertEqual(html.count('class="history-chart '), 4)
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;', html)
        self.assertNotIn('<script>', html)
        self.server.clear_clients()
        self.assertEqual(self.server.get_clients_snapshot(), [])


if __name__ == '__main__':
    unittest.main()
