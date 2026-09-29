import json
import re
import unittest
from unittest.mock import patch

from server import HeartbeatServer

VM = '37914fc2-9f9a-4979-b3ea-641e1be1d233'
OTHER = '12345678-1234-1234-1234-123456789012'


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.server = HeartbeatServer('127.0.0.1', 0)
        self.addCleanup(self.server.migrations.db.close)
        self.quiet = patch.object(self.server, 'print_clients_locked')
        self.quiet.start()
        self.addCleanup(self.quiet.stop)

    def report(self, identity=VM, count=3):
        self.server.migrations.ingest({'node': 'compute-a', 'events': [
            {'vm_uuid': identity, 'at': f'2026-09-28T{index // 60:02}:{index % 60:02}:00Z', 'downtime_ms': index + 10, 'iterations': index % 5 + 1}
            for index in range(count)], 'placements': [
                {'vm_uuid': identity, 'instance': 'instance-64', 'active_at': '2026-09-28T00:00:00Z', 'ended_at': None}]})

    def test_empty_statistics_and_default_tabs(self):
        data = self.server.statistics_data()
        self.assertEqual(data['vms'], [])
        self.assertEqual(data['events'], [])
        page = self.server.render_status_page()
        self.assertIn('id="overview-tab" role="tab" aria-selected="true"', page)
        self.assertIn('id="details-panel" role="tabpanel" aria-labelledby="details-tab" hidden', page)
        self.assertIn('id="statistic-panel" role="tabpanel" aria-labelledby="statistic-tab" hidden', page)
        self.assertEqual(page.count('data-metric='), 4)

    def test_fleet_squares_keep_positions_as_health_changes(self):
        for name in ['c-red', 'a-green', 'b-<yellow>']:
            self.server.record_heartbeat(name, 'peer', {})
        # Snapshot order is alphabetical; exact threshold boundaries stay inclusive.
        with patch.object(self.server, 'get_heartbeat_age_ms', side_effect=[5000, 10000, 10001]):
            grid = self.server.render_status_content('overview')
        squares = re.findall(r'class="vm-square ([^"]+)" data-client-id="([^"]+)"', grid)
        self.assertEqual(squares, [('status-healthy', 'a-green'),
                                  ('status-warning', 'b-&lt;yellow&gt;'), ('status-stale', 'c-red')])
        with patch.object(self.server, 'get_heartbeat_age_ms', side_effect=[10001, 5000, 10000]):
            updated = self.server.render_status_content('overview')
        self.assertEqual(re.findall(r'data-client-id="([^"]+)"', updated), [name for _, name in squares])
        self.assertIn('aria-label="b-&lt;yellow&gt; · Healthy · View details"', updated)
        self.server.clear_clients()
        self.assertNotIn('class="vm-square ', self.server.render_status_content('overview'))

    def test_history_uses_timestamps_and_limits_chart_not_summary(self):
        self.report(count=250)
        data = self.server.statistics_data()
        self.assertEqual(data['vms'][0]['migration']['count'], 250)
        self.assertEqual(len(data['events']), 200)
        self.assertEqual(data['events'][0]['downtime_ms'], 60)
        self.assertEqual(data['events'][-1]['downtime_ms'], 259)
        self.assertEqual(data['events'][-1]['iterations'], 5)
        self.assertEqual(data['events'], sorted(data['events'], key=lambda event: event['at']))

    def test_selected_vm_history_keeps_all_vm_overview(self):
        self.report()
        self.report(OTHER, 1)
        data = self.server.statistics_data(OTHER)
        self.assertEqual(data['selected'], OTHER)
        self.assertEqual(len(data['vms']), 2)
        self.assertEqual(len(data['events']), 1)
        self.assertEqual(data['events'][0]['vm_uuid'], OTHER)
        self.assertIsNone(self.server.statistics_data('unknown')['selected'])

    def test_dmi_join_and_renamed_client_are_one_vm(self):
        self.report()
        self.server.record_heartbeat('old-name', 'peer', {}, vm_uuid=VM)
        self.server.record_heartbeat('new-name', 'peer', {}, vm_uuid=VM)
        data = self.server.statistics_data()
        self.assertEqual(len(data['vms']), 1)
        self.assertEqual(data['vms'][0]['name'], VM)
        self.assertEqual(data['vms'][0]['migration']['last_ms'], 12)

    def test_compute_only_vm_retained_after_reset(self):
        self.report()
        self.server.record_heartbeat('guest', 'peer', {}, vm_uuid=VM)
        self.server.clear_clients()
        data = self.server.statistics_data()
        self.assertEqual(data['vms'][0]['name'], VM)
        self.assertFalse(data['vms'][0]['connected'])
        self.assertEqual(len(data['events']), 3)

    def test_usage_window_current_values_and_missing_samples(self):
        with patch('server.time.monotonic', return_value=100):
            self.server.record_heartbeat('guest', 'peer', {'cpu_percent': 20, 'memory_percent': 40}, vm_uuid=VM)
            self.server.record_heartbeat('guest', 'peer', {'cpu_percent': 40}, vm_uuid=VM)
        with patch('server.time.monotonic', return_value=105), patch('server.time.time', return_value=1000):
            vm = self.server.statistics_data()['vms'][0]
        self.assertEqual(vm['history'], [{'at': 995, 'cpu_percent': 30, 'memory_percent': 40}])
        self.assertEqual(vm['cpu_percent'], 30)
        with patch('server.time.monotonic', return_value=111):
            vm = self.server.statistics_data()['vms'][0]
        self.assertIsNone(vm['cpu_percent'])
        self.assertEqual(len(vm['history']), 1)
        with patch('server.time.monotonic', return_value=160):
            vm = self.server.statistics_data()['vms'][0]
        self.assertEqual(vm['history'], [])

    def test_legacy_client_does_not_get_another_vms_migrations(self):
        self.report()
        self.server.record_heartbeat('<guest>', 'peer', {'cpu_percent': 50})
        data = self.server.statistics_data('client:<guest>')
        self.assertEqual(data['events'], [])
        self.assertEqual(len(data['vms']), 2)
        self.assertEqual(next(vm['name'] for vm in data['vms'] if vm['id'] == 'client:<guest>'), '<guest>')
        json.dumps(data, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
