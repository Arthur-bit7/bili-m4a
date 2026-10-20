import io
import re
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from app import create_app
from core import AudioError
from session import RunSession


class DownloadExitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'old-task').mkdir()
        (self.root / 'old-task' / 'audio.m4a').write_bytes(b'old audio')
        (self.root / 'old.zip').write_bytes(b'old zip')
        self.release = threading.Event()
        self.started = threading.Event()
        self.responses = []
        self.app = None

    def make_app(self, runner=None):
        self.app = create_app(self.root, runner=runner or self.runner)
        self.client = self.app.test_client()
        self.session = self.app.extensions['audio_session']
        token = re.search(r'name="app-token" content="([^"]+)"', self.client.get('/').data.decode())[1]
        self.headers = {'X-App-Token': token}

    def tearDown(self):
        self.release.set()
        for response in self.responses:
            response.close()
        if self.app:
            self.app.extensions['audio_pool'].shutdown(wait=True)
            if self.session.status()['phase'] in ('stopping', 'cleaning'):
                self.session.finished.wait(3)
        self.temp.cleanup()

    def runner(self, url, folder, update):
        page = int(url.split('?p=')[1])
        (folder / 'audio.m4a').write_bytes(f'audio-{page}'.encode())
        return {'title': '../相同\\标题:测试', 'filename': 'test.m4a', 'duration': 1, 'size': 7}

    def submit(self, pages=(1, 2, 3)):
        response = self.client.post('/api/jobs', json={'title': '课程', 'urls': [
            f'https://www.bilibili.com/video/BV1bK411W797?p={page}' for page in pages]}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        return response.json

    def wait_jobs(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            jobs = self.client.get('/api/jobs').json
            if predicate(jobs):
                return jobs
            time.sleep(.01)
        self.fail('worker timed out')

    def shutdown(self):
        response = self.client.post('/api/shutdown', json={'confirmed': True}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        return response

    def test_zip_snapshot_sorts_and_excludes_failures_and_waiting(self):
        def runner(url, folder, update):
            if url.endswith('?p=2'):
                raise AudioError('failed')
            if url.endswith('?p=4'):
                self.started.set()
                self.release.wait(5)
            return self.runner(url, folder, update)
        self.make_app(runner)
        batch = self.submit((4, 3, 2, 1))
        self.assertTrue(self.started.wait(2))
        response = self.client.get(f"/api/batches/{batch['batch_id']}/zip")
        self.responses.append(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/zip')
        self.assertIn('attachment', response.headers['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), 2)
            self.assertTrue(names[0].startswith('P001_'))
            self.assertTrue(names[1].startswith('P003_'))
            self.assertTrue(all('/' not in name and '\\' not in name for name in names))
            self.assertEqual(archive.read(names[0]), b'audio-1')
        response.close()
        self.assertEqual(self.session.status()['transfers'], 0)

    def test_head_conditional_and_invalid_range_do_not_block_exit(self):
        self.make_app()
        batch = self.submit((1,))
        self.wait_jobs(lambda jobs: jobs and jobs[0]['status'] == 'done')
        path = f"/audio/{batch['id']}"
        response = self.client.head(path)
        self.assertEqual(response.status_code, 200)
        etag = response.headers['ETag']
        response.close()
        response = self.client.get(path, headers={'If-None-Match': etag})
        self.assertEqual(response.status_code, 304)
        response.close()
        self.assertEqual(self.client.get(path, headers={'Range': 'bytes=1000-2000'}).status_code, 416)
        self.assertEqual(self.session.status()['transfers'], 0)
        self.shutdown()
        self.assertTrue(self.session.finished.wait(2))

    def test_empty_and_missing_zip_requests_release_leases(self):
        def blocked(url, folder, update):
            self.started.set()
            self.release.wait(5)
            return self.runner(url, folder, update)
        self.make_app(blocked)
        batch = self.submit((1,))
        self.assertTrue(self.started.wait(2))
        self.assertEqual(self.client.get(f"/api/batches/{batch['batch_id']}/zip").status_code, 409)
        self.assertEqual(self.client.get('/api/batches/missing/zip').status_code, 404)
        self.assertEqual(self.session.status()['transfers'], 0)

    def test_cleanup_waits_for_existing_audio_response_and_keeps_old_files(self):
        self.make_app()
        self.app.config['MAX_HISTORY'] = 1
        first = self.submit((1,))
        self.wait_jobs(lambda jobs: jobs and jobs[0]['status'] == 'done')
        second = self.submit((2,))
        self.wait_jobs(lambda jobs: len(jobs) == 1 and jobs[0]['id'] == second['id'] and jobs[0]['status'] == 'done')
        response = self.client.get(f"/audio/{second['id']}", buffered=False)
        self.responses.append(response)
        self.assertEqual(self.session.status()['transfers'], 1)
        self.shutdown()
        self.assertFalse(self.session.finished.wait(.05))
        self.assertTrue((self.root / first['id'] / 'audio.m4a').exists())
        self.assertTrue((self.root / second['id'] / 'audio.m4a').exists())
        self.assertEqual(self.client.get(f"/audio/{second['id']}").status_code, 503)
        response.close()
        self.assertTrue(self.session.finished.wait(2))
        self.assertEqual(self.session.status()['phase'], 'closed')
        self.assertFalse((self.root / first['id']).exists())
        self.assertFalse((self.root / second['id']).exists())
        self.assertEqual((self.root / 'old-task' / 'audio.m4a').read_bytes(), b'old audio')
        self.assertEqual((self.root / 'old.zip').read_bytes(), b'old zip')

    def test_cleanup_waits_for_zip_and_keeps_browser_copy(self):
        self.make_app()
        batch = self.submit((1,))
        self.wait_jobs(lambda jobs: jobs and jobs[0]['status'] == 'done')
        response = self.client.get(f"/api/batches/{batch['batch_id']}/zip", buffered=False)
        self.responses.append(response)
        owned = list(self.session.owned)
        # A browser copy outside the owned cache directories must survive.
        saved = self.root / 'browser-saved.zip'
        saved.write_bytes(response.data)
        response.close()
        # Hold an independent Range transfer while requesting exit.
        transfer = self.client.get(f"/audio/{batch['id']}", headers={'Range': 'bytes=0-3'}, buffered=False)
        self.responses.append(transfer)
        self.assertEqual(transfer.status_code, 206)
        self.shutdown()
        self.assertFalse(self.session.finished.wait(.05))
        transfer.close()
        self.assertTrue(self.session.finished.wait(2))
        self.assertTrue(saved.exists())
        self.assertTrue(all(not path.exists() for path in owned))

    def test_exit_cancels_queued_waits_for_active_and_rejects_new_tasks(self):
        def blocked(url, folder, update):
            self.started.set()
            self.release.wait(5)
            return self.runner(url, folder, update)
        self.make_app(blocked)
        batch = self.submit((1, 2, 3))
        self.assertTrue(self.started.wait(2))
        self.assertEqual(self.client.post('/api/shutdown', json={'confirmed': True}).status_code, 403)
        self.assertEqual(self.client.post('/api/shutdown', json={}, headers=self.headers).status_code, 400)
        self.assertEqual(self.session.status()['phase'], 'running')
        self.shutdown()
        statuses = {j['page']: j['status'] for j in self.client.get('/api/jobs').json}
        self.assertEqual(statuses, {1: 'extracting', 2: 'cancelled', 3: 'cancelled'})
        self.assertFalse(self.session.finished.wait(.05))
        self.assertEqual(self.client.post('/api/jobs', json={'url': 'BV1bK411W797'}, headers=self.headers).status_code, 503)
        self.assertEqual(self.client.post('/api/video', json={'url': 'BV1bK411W797'}, headers=self.headers).status_code, 503)
        self.release.set()
        self.assertTrue(self.session.finished.wait(2))
        self.assertFalse((self.root / batch['id']).exists())

    def test_cleanup_failure_is_reported_and_can_be_retried(self):
        self.make_app()
        stopped = threading.Event()
        self.app.extensions['audio_stop_server'] = stopped.set
        batch = self.submit((1,))
        self.wait_jobs(lambda jobs: jobs and jobs[0]['status'] == 'done')
        with patch.object(self.session, 'remove_folder', side_effect=PermissionError('locked')):
            with self.assertLogs(self.app.logger, level='ERROR'):
                self.shutdown()
                self.assertTrue(self.session.finished.wait(2))
        self.assertEqual(self.session.status()['phase'], 'error')
        self.assertFalse(stopped.is_set())
        self.assertTrue((self.root / batch['id']).exists())
        self.shutdown()
        self.assertTrue(stopped.wait(2))
        self.assertEqual(self.session.status()['phase'], 'closed')

    def test_broken_console_does_not_prevent_cleanup_or_server_stop(self):
        self.make_app()
        batch = self.submit((1,))
        self.wait_jobs(lambda jobs: jobs and jobs[0]['status'] == 'done')
        stopped = threading.Event()
        self.app.extensions['audio_stop_server'] = stopped.set
        with patch('app.print', side_effect=BrokenPipeError('closed pipe'), create=True):
            self.shutdown()
            self.assertTrue(stopped.wait(2))
        self.assertEqual(self.session.status()['phase'], 'closed')
        self.assertFalse((self.root / batch['id']).exists())
        self.assertEqual((self.root / 'old-task' / 'audio.m4a').read_bytes(), b'old audio')

    def test_existing_directory_collision_is_never_owned_or_deleted(self):
        self.make_app()
        with patch('app.uuid.uuid4', return_value=Mock(hex='old-task')):
            self.submit((1,))
        self.wait_jobs(lambda jobs: jobs and jobs[0]['status'] == 'error')
        self.shutdown()
        self.assertTrue(self.session.finished.wait(2))
        self.assertEqual((self.root / 'old-task' / 'audio.m4a').read_bytes(), b'old audio')

    def test_unregistered_files_and_new_session_are_not_cleaned(self):
        self.make_app()
        added = self.root / 'unregistered'
        added.mkdir()
        (added / 'keep.txt').write_text('keep')
        self.shutdown()
        self.assertTrue(self.session.finished.wait(2))
        self.assertTrue((added / 'keep.txt').exists())
        another = RunSession(self.root)
        another.begin_shutdown()
        another.cleanup()
        self.assertTrue((self.root / 'old-task' / 'audio.m4a').exists())
        with self.assertRaises(OSError):
            another.create_folder('../outside')


if __name__ == '__main__':
    unittest.main()
