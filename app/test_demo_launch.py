"""Evaluator launch stays repeatable and isolated from configured live services."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request


class DemoLaunch(unittest.TestCase):
    def test_demo_launch_ignores_live_write_settings_and_restarts_fresh(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, CONNECT_ENV_FILE=str(Path(tmp) / 'absent.env'),
                       CONNECT_APPLY_MODE='sandbox', YEXT_API_KEY='test-not-a-key',
                       YEXT_BASE_URL='http://127.0.0.1:1',
                       YEXT_SANDBOX_MIRROR='FI-D0001:test-entity',
                       CONNECT_MOCK_URL='http://127.0.0.1:1',
                       CONNECT_RUNTIME_DIR=tmp, CONNECT_LIVE_CHECKS='',
                       OPENSHIFT_AI_URL='', OPENSHIFT_AI_TOKEN='', PYTHONUTF8='1')
            for run in range(2):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    env['PORT'] = str(sock.getsockname()[1])
                base = 'http://127.0.0.1:' + env['PORT']
                with open(Path(tmp) / 'server.log', 'w', encoding='utf-8') as log:
                    server = subprocess.Popen([sys.executable, '-u', 'app/app.py', '--demo'],
                                              cwd=root, env=env, stdout=log, stderr=log)
                    try:
                        deadline = time.monotonic() + 45
                        while True:
                            try:
                                response = urllib.request.urlopen(base + '/demo', timeout=2)
                                break
                            except OSError:
                                if server.poll() is not None or time.monotonic() > deadline:
                                    self.fail('Demo did not start: ' + (Path(tmp) / 'server.log').read_text(encoding='utf-8'))
                                time.sleep(.1)
                        with response:
                            self.assertTrue(response.url.endswith('/w/FI-D0017/christmas'))
                            html = response.read().decode('utf-8')
                        self.assertIn('Yes, update all', html)
                        self.assertIn('Local simulation.', html)
                        self.assertNotIn('configured Yext sandbox mirror', html)
                        def post(suffix, form):
                            data = urllib.parse.urlencode(form).encode()
                            with urllib.request.urlopen(base + '/w/FI-D0017/christmas' + suffix,
                                                        data=data, timeout=10) as response:
                                return response.read().decode('utf-8')
                        if run == 0:
                            self.assertIn('Save these hours', post('/reply', {'msg': '24 Dec 10-14'}))
                            self.assertIn('We saved your hours:', post('/reply', {'msg': 'CONFIRM'}))
                            self.assertIn('Put back', post('/reply', {'msg': 'UNDO'}))
                            with urllib.request.urlopen(base + '/w/FI-D0017/christmas/reset', timeout=10) as reset:
                                self.assertIn('Yes, update all', reset.read().decode('utf-8'))
                            self.assertIn('Done', post('', {'a': 'yes'}))
                    finally:
                        server.terminate()
                        server.wait(timeout=10)
            self.assertFalse((Path(tmp) / 'audit.jsonl').exists())
            self.assertFalse((Path(tmp) / 'sandbox').exists())
