"""Exercise the actual CLI server lifecycle over localhost, without Bilibili."""
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request, urlopen
import zipfile


class ServerExitTests(unittest.TestCase):
    def test_real_server_downloads_zip_cleans_only_this_run_and_exits(self):
        for encoding in ('utf-8', 'cp936', 'cp1252'):
            with self.subTest(encoding=encoding):
                self.check_server_lifecycle(encoding)

    def check_server_lifecycle(self, encoding):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('app.py', 'core.py', 'session.py'):
                shutil.copyfile(source / name, root / name)
            for name in ('templates', 'static'):
                shutil.copytree(source / name, root / name)
            old = root / 'downloads' / 'old-run'
            old.mkdir(parents=True)
            (old / 'audio.m4a').write_bytes(b'old audio')
            launcher = root / 'launch.py'
            launcher.write_text('''import core
import runpy

def runner(url, folder, update):
    (folder / 'audio.m4a').write_bytes(b'sample audio')
    return {'title': 'sample', 'filename': 'sample.m4a', 'duration': 1, 'size': 12}

core.extract_audio = runner
runpy.run_path('app.py', run_name='__main__')
''', encoding='utf-8')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([sys.executable, str(launcher), '--no-browser', '--port', str(port)],
                                       cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding=encoding,
                                       env={**os.environ, 'PYTHONIOENCODING': encoding})
            base = f'http://127.0.0.1:{port}'
            def get(path):
                with urlopen(base + path, timeout=2) as response:
                    return response.read()
            def post(path, body, token):
                request = Request(base + path, data=json.dumps(body).encode(), headers={
                    'Content-Type': 'application/json', 'X-App-Token': token}, method='POST')
                with urlopen(request, timeout=2) as response:
                    return json.load(response)
            try:
                deadline = time.monotonic() + 8
                while True:
                    try:
                        html = get('/').decode()
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail('CLI server did not start')
                        time.sleep(.05)
                token = re.search(r'name="app-token" content="([^"]+)"', html)[1]
                batch = post('/api/jobs', {'url': 'BV1bK411W797'}, token)
                while True:
                    jobs = json.loads(get('/api/jobs'))
                    if jobs[0]['status'] == 'done':
                        break
                    if time.monotonic() > deadline:
                        self.fail('test extraction did not finish')
                    time.sleep(.01)
                data = get(f"/api/batches/{batch['batch_id']}/zip")
                saved = root / 'browser-saved.zip'
                saved.write_bytes(data)
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    self.assertEqual(archive.namelist(), ['P001_sample.m4a'])
                    self.assertEqual(archive.read(archive.namelist()[0]), b'sample audio')
                post('/api/shutdown', {'confirmed': True}, token)
                output, _ = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 0, output)
                self.assertEqual((old / 'audio.m4a').read_bytes(), b'old audio')
                self.assertTrue(saved.exists())
                self.assertEqual(list((root / 'downloads').iterdir()), [old])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
