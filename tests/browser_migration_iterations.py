"""Run with python3 tests/browser_migration_iterations.py (requires Firefox)."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import HeartbeatServer

PROBE = '''<script>
window.addEventListener('load', async () => {
  const errors = [];
  const byId = id => document.getElementById(id);
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const until = async test => {
    for (let i=0; i<80; i++) { if(test()) return; await wait(100); }
    throw new Error('Timed out');
  };
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const points = () => [...document.querySelectorAll('#statistics-chart circle')];
  const metric = name => document.querySelector(`[data-metric="${name}"]`).click();
  const key = (target, key) => target.dispatchEvent(new KeyboardEvent('keydown', {key, shiftKey:true, bubbles:true}));
  try {
    byId('statistic-tab').click();
    await until(() => points().length === 24);
    metric('iterations');
    check(points().length === 18, 'Unknown counts must be omitted');
    check(byId('statistics-reading').textContent === '4 iterations', 'Latest count incorrect');
    check(points().some(point => point.getAttribute('aria-label').includes('0 iterations')), 'Zero iterations lost');
    check(byId('statistics-chart-title').textContent === 'Migration iterations over time', 'Wrong title');
    const range = byId('migration-time-range'), start = byId('migration-time-start');
    const end = byId('migration-time-end'), middle = byId('migration-time-window');
    check(!range.hidden && !start.disabled, 'Missing time slider');
    key(start, 'ArrowRight'); key(end, 'ArrowLeft');
    check(points().length > 0 && points().length < 18, 'Slider did not narrow range');
    const selected = start.getAttribute('aria-valuenow');
    await wait(2400);
    check(start.getAttribute('aria-valuenow') === selected, 'Refresh moved interval');
    const width = Number(end.getAttribute('aria-valuenow')) - Number(selected);
    // Test the pointer drag calculation; native pointer capture requires physical input.
    middle.setPointerCapture = () => {}; middle.hasPointerCapture = () => true;
    middle.dispatchEvent(new PointerEvent('pointerdown', {button:0, pointerId:1, clientX:200}));
    middle.dispatchEvent(new PointerEvent('pointermove', {pointerId:1, clientX:220}));
    middle.dispatchEvent(new Event('lostpointercapture'));
    check(Number(start.getAttribute('aria-valuenow')) > Number(selected), 'Window did not pan');
    check(Math.abs(Number(end.getAttribute('aria-valuenow')) - Number(start.getAttribute('aria-valuenow')) - width) < .001, 'Pan changed width');
    byId('migration-time-reset').click();
    check(points().length === 18, 'Full range did not restore points');
    key(start, 'ArrowRight'); key(end, 'Home');
    check(byId('statistics-chart').textContent.includes('No migrations in this interval') && !range.hidden, 'Empty interval cannot expand');
    byId('migration-time-reset').click();
    points()[0].focus();
    check(byId('statistics-point').textContent.includes('iterations before switchover'), 'Point inspection missing');
    const picker = byId('statistics-vm');
    picker.value = picker.options[1].value; picker.dispatchEvent(new Event('change'));
    await until(() => points().length === 3);
    check(byId('statistics-rows').children.length === 6, 'VM filter hid fleet summary');
    metric('migration');
    check(points().length === 4 && !range.hidden, 'Downtime chart broken');
    metric('cpu_percent');
    check(range.hidden, 'CPU shows migration slider');
    picker.value = picker.options[2].value; picker.dispatchEvent(new Event('change'));
    await until(() => byId('statistics-chart-note').textContent.includes(picker.value));
    metric('iterations');
    check(points().length === 0 && byId('statistics-chart').textContent.includes('No migration iteration counts yet'), 'Missing-data feedback incorrect');
  } catch(error) { errors.push(error.message); }
  await fetch('/_test/result', {method:'POST', body:JSON.stringify(errors)});
});
</script>'''


class MigrationIterationsBrowserTest(unittest.TestCase):
    def test_iteration_chart_and_time_range(self):
        firefox = shutil.which('firefox')
        if not firefox:
            self.skipTest('Firefox is required for this browser test')
        server = HeartbeatServer('127.0.0.1', 0, http_port=0)
        self.addCleanup(server.migrations.db.close)
        for vm in range(6):
            identity = f'00000000-0000-4000-8000-{vm + 1:012}'
            events = [{'vm_uuid': identity, 'at': f'2026-09-28T{10 + i:02}:{vm:02}:00Z',
                       'downtime_ms': 50 + i, 'iterations': None if vm == 1 else i}
                      for i in range(4)]
            # Include a count of four on the last VM and a missing legacy event.
            if vm == 5:
                events[0]['iterations'] = None
                events[-1]['iterations'] = 4
            if vm == 0:
                events[-1]['iterations'] = None
            server.migrations.ingest({'node': 'compute-a', 'events': events, 'placements': []})
        original = server.render_status_page
        server.render_status_page = lambda: original().replace('</body>', PROBE + '</body>')
        done, errors, captured = threading.Event(), [], []

        def capture(address, handler):
            class ProbeHandler(handler):
                def do_POST(self):
                    if self.path != '/_test/result':
                        return super().do_POST()
                    errors.extend(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                    self.send_response(204)
                    self.end_headers()
                    done.set()
            httpd = ThreadingHTTPServer(address, ProbeHandler)
            captured.append(httpd)
            return httpd

        with patch('server.ThreadingHTTPServer', side_effect=capture):
            server.start_http_server()
        self.addCleanup(captured[0].server_close)
        self.addCleanup(captured[0].shutdown)
        with tempfile.TemporaryDirectory(prefix='reamer-iterations-browser-') as profile:
            browser = subprocess.Popen([firefox, '--headless', '--no-remote', '--profile', profile,
                                        f'http://127.0.0.1:{captured[0].server_port}/'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertTrue(done.wait(30), 'Browser did not finish')
                self.assertEqual(errors, [])
            finally:
                browser.terminate()
                browser.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
