"""Run with python3 tests/browser_stress_controls.py (requires Firefox).

Exercises actual DOM form submission against Reamer, without running stress tools.
"""
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

VM = '37914fc2-9f9a-4979-b3ea-641e1be1d233'
PROBE = '''<script>
window.addEventListener('load', async () => {
  const errors = [];
  const until = async test => {
    for (let i=0; i<80; i++) { if(await test()) return; await new Promise(resolve=>setTimeout(resolve,100)); }
    throw new Error('Timed out: ' + document.getElementById('control-feedback').textContent);
  };
  const snapshot = async () => (await fetch('/_test/commands')).json();
  const form = kind => [...document.querySelectorAll('.stress-form')].find(item=>item.elements.kind.value===kind);
  try {
    document.getElementById('details-tab').click();
    for (const kind of ['cpu', 'memory']) {
      for (const action of ['start', 'stop']) {
        await until(()=>form(kind).elements.action.value===action && !form(kind).querySelector('button').disabled);
        form(kind).querySelector('button').click();
        await until(async()=> (await snapshot()).commands.some(command=>command.kind===kind && command.action===action));
        if(document.getElementById('control-feedback').textContent) throw new Error('Command error');
        await fetch('/_test/ack', {method:'POST'});
      }
    }
    form('migration').querySelector('button').click();
    await until(async()=> (await snapshot()).migration.state==='queued');
  } catch(error) { errors.push(error.message); }
  await fetch('/_test/result', {method:'POST',body:JSON.stringify(errors)});
});
</script>'''


class StressButtonsBrowserTest(unittest.TestCase):
    def test_start_stop_and_migrate_use_the_form_destination(self):
        firefox = shutil.which('firefox')
        if not firefox:
            self.skipTest('Firefox is required for this browser test')
        server = HeartbeatServer('127.0.0.1', 0, http_port=0, warning_threshold_ms=60000)
        self.addCleanup(server.migrations.db.close)
        server.print_clients_locked = lambda: None
        server.record_heartbeat('guest', 'peer', {}, supports_control=True, vm_uuid=VM)
        server.migration_control.report({'controller': 'test', 'hosts': ['a', 'b'], 'results': []})
        original = server.render_status_page
        server.render_status_page = lambda: original().replace('</body>', PROBE + '</body>')
        done, errors, requests, captured = threading.Event(), [], [], []

        def capture(address, handler):
            class ProbeHandler(handler):
                def do_GET(self):
                    if self.path != '/_test/commands':
                        return super().do_GET()
                    body = json.dumps({'commands': server.commands_for('guest'), 'migration': server.migration_control.status(VM)}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_POST(self):
                    if self.path == '/_test/ack':
                        stress = server.clients['guest']['stress'].copy()
                        for command in server.commands_for('guest'):
                            stress[command['kind']] = {'state': 'running' if command['action'] == 'start' else 'idle', 'last_command': command['id']}
                        server.record_heartbeat('guest', 'peer', {}, stress, True, VM)
                        self.send_response(204)
                        self.end_headers()
                    elif self.path == '/_test/result':
                        errors.extend(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                        self.send_response(204)
                        self.end_headers()
                        done.set()
                    else:
                        requests.append(self.path)
                        super().do_POST()
            httpd = ThreadingHTTPServer(address, ProbeHandler)
            captured.append(httpd)
            return httpd

        with patch('server.ThreadingHTTPServer', side_effect=capture):
            server.start_http_server()
        self.addCleanup(captured[0].server_close)
        self.addCleanup(captured[0].shutdown)
        with tempfile.TemporaryDirectory(prefix='reamer-stress-browser-') as profile:
            browser = subprocess.Popen([firefox, '--headless', '--no-remote', '--profile', profile,
                                        f'http://127.0.0.1:{captured[0].server_port}/'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertTrue(done.wait(30), 'Browser did not finish')
                self.assertEqual(errors, [])
                self.assertEqual(requests, ['/stress'] * 4 + ['/migrate'])
            finally:
                browser.terminate()
                browser.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
