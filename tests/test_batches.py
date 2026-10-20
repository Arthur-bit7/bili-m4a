import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app import create_app
from core import AudioError


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.release = threading.Event()
        self.started = threading.Event()
        self.seen = []

    def make_app(self, runner=None):
        self.app = create_app(self.temp.name, runner=runner or self.runner)
        self.client = self.app.test_client()
        token = re.search(r'name="app-token" content="([^"]+)"', self.client.get('/').data.decode())[1]
        self.headers = {'X-App-Token': token}

    def tearDown(self):
        self.release.set()
        self.app.extensions['audio_pool'].shutdown(wait=True)
        self.temp.cleanup()

    def runner(self, url, folder, update):
        self.seen.append(int(url.split('?p=')[1]))
        folder.mkdir(parents=True)
        (folder / 'audio.m4a').write_bytes(b'audio')
        return {'title': '测试音频', 'filename': 'test.m4a', 'duration': 1, 'size': 5}

    def blocking_runner(self, url, folder, update):
        self.started.set()
        if not self.release.wait(5):
            raise RuntimeError('test worker timed out')
        return self.runner(url, folder, update)

    def submit(self, pages):
        return self.client.post('/api/jobs', json={'title': '课程', 'urls': [
            f'https://www.bilibili.com/video/BV1bK411W797?p={page}' for page in pages
        ]}, headers=self.headers)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            jobs = self.client.get('/api/jobs').json
            if predicate(jobs):
                return jobs
            time.sleep(.01)
        self.fail('worker timed out')

    def test_sorted_serial_execution_and_failure_continues(self):
        active = 0
        maximum = 0
        lock = threading.Lock()
        def runner(url, folder, update):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                result = self.runner(url, folder, update)
                if url.endswith('?p=2'):
                    raise AudioError('第二 P 失败')
                return result
            finally:
                with lock:
                    active -= 1
        self.make_app(runner)
        response = self.submit([3, 1, 2])
        self.assertEqual(response.status_code, 202)
        jobs = self.wait_for(lambda jobs: len(jobs) == 3 and all(j['status'] in ('done', 'error') for j in jobs))
        self.assertEqual(self.seen, [1, 2, 3])
        self.assertEqual(maximum, 1)
        self.assertEqual(sum(j['status'] == 'done' for j in jobs), 2)
        batch = self.client.get('/api/batches').json[0]
        self.assertEqual([j['page'] for j in batch['jobs']], [1, 2, 3])
        failed = next(j for j in jobs if j['status'] == 'error')
        self.assertFalse((Path(self.temp.name) / failed['id']).exists())

    def test_queue_admission_is_atomic_and_cancel_frees_capacity(self):
        self.make_app(self.blocking_runner)
        first = self.submit(range(1, 51)).json
        self.assertTrue(self.started.wait(2))
        self.assertEqual(self.submit(range(51, 101)).status_code, 202)
        rejected = self.submit([101, 102])
        self.assertEqual(rejected.status_code, 429)
        self.assertIn('0', rejected.json['error'])
        self.assertEqual(len(self.client.get('/api/jobs').json), 100)
        response = self.client.post(f"/api/batches/{first['batch_id']}/cancel", headers=self.headers)
        self.assertEqual(response.json['cancelled'], 49)
        self.assertEqual(self.submit(range(101, 131)).status_code, 202)
        self.release.set()
        self.wait_for(lambda jobs: all(j['status'] in ('done', 'cancelled') for j in jobs))
        self.assertEqual(self.seen, [1, *range(51, 131)])

    def test_single_cancel_cannot_cancel_running_job(self):
        self.make_app(self.blocking_runner)
        batch = self.submit([1, 2, 3]).json
        self.assertTrue(self.started.wait(2))
        running, queued = batch['ids'][:2]
        self.assertEqual(self.client.post(f'/api/jobs/{queued}/cancel').status_code, 403)
        self.assertEqual(self.client.post(f'/api/jobs/{running}/cancel', headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post(f'/api/jobs/{queued}/cancel', headers=self.headers).json['cancelled'], 1)
        self.assertEqual(self.client.post(f'/api/jobs/{queued}/cancel', headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post('/api/jobs/missing/cancel', headers=self.headers).status_code, 404)
        self.release.set()
        self.wait_for(lambda jobs: all(j['status'] in ('done', 'cancelled') for j in jobs))
        self.assertEqual(self.seen, [1, 3])

    def test_invalid_batch_does_not_create_partial_jobs(self):
        self.make_app()
        for pages in ([], range(1, 52), [1, 1]):
            self.assertEqual(self.submit(pages).status_code, 400)
        self.assertEqual(self.client.post('/api/jobs', json={'urls': [
            'https://www.bilibili.com/video/BV1bK411W797?p=1',
            'https://www.bilibili.com/video/BV1nx411u79K?p=2'
        ]}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get('/api/jobs').json, [])
        self.assertEqual(self.client.get('/api/batches').json, [])

    def test_finished_history_is_evicted_without_blocking_new_jobs(self):
        self.make_app()
        self.app.config['MAX_HISTORY'] = 2
        first_id = None
        for page in range(1, 6):
            response = self.submit([page])
            self.assertEqual(response.status_code, 202)
            if first_id is None:
                first_id = response.json['id']
            self.wait_for(lambda jobs: all(j['status'] == 'done' for j in jobs) and len(jobs) <= 2)
        self.assertEqual(len(self.client.get('/api/batches').json), 2)
        self.assertNotIn(first_id, [j['id'] for j in self.client.get('/api/jobs').json])
        self.assertTrue((Path(self.temp.name) / first_id / 'audio.m4a').exists())


if __name__ == '__main__':
    unittest.main()
