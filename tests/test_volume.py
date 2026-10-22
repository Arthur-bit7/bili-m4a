"""Volume changes must reach the downloaded samples, not just the player."""
from array import array
import math
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from mutagen.mp4 import MP4

from app import create_app
from core import convert_audio, extract_audio, ffmpeg


def samples(path):
    decoded = ffmpeg(['-v', 'error', '-i', str(path), '-map', '0:a:0',
                      '-ac', '1', '-ar', '48000', '-f', 'f32le', '-']).stdout
    values = array('f')
    values.frombytes(decoded)
    # Ignore encoder padding and limiter startup at the edges.
    return values[4800:43200]


def rms(values):
    return math.sqrt(sum(value * value for value in values) / len(values))


class VolumeMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.m4a'
        ffmpeg(['-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1:sample_rate=48000',
                '-c:a', 'aac', '-b:a', '192k', str(self.source)])

    def tearDown(self):
        self.temp.cleanup()

    def test_aac_volume_changes_samples_and_preserves_metadata_and_duration(self):
        original = rms(samples(self.source))
        for volume in (25, 50, 200, 400):
            with self.subTest(volume=volume):
                target = self.root / f'{volume}.m4a'
                result = convert_audio(self.source, target, 'mp4a.40.2', 'title', 'artist', volume)
                self.assertAlmostEqual(rms(samples(target)) / original, volume / 100, delta=.08)
                media = MP4(target)
                self.assertEqual(media.info.codec, 'mp4a.40.2')
                self.assertEqual(media['\xa9nam'], ['title'])
                self.assertEqual(media['\xa9ART'], ['artist'])
                self.assertAlmostEqual(result['duration'], 1, delta=.06)
                # Inspect the encoded output for accidental video tracks.
                inspection = ffmpeg(['-hide_banner', '-i', str(target), '-f', 'null', '-'])
                self.assertNotIn(b'Video:', inspection.stderr)

    def test_boost_limits_peaks_instead_of_hard_clipping(self):
        loud = self.root / 'loud.wav'
        ffmpeg(['-v', 'error', '-f', 'lavfi', '-i',
                'sine=frequency=440:duration=1:sample_rate=48000', '-af', 'volume=6', str(loud)])
        target = self.root / 'limited.m4a'
        convert_audio(loud, target, 'pcm_s16le', volume_percent=400)
        values = samples(target)
        self.assertGreater(rms(values), .5)
        self.assertLess(max(abs(value) for value in values), 1.02)
        self.assertLess(sum(abs(value) >= .99 for value in values) / len(values), .01)

    def test_extraction_applies_volume_before_returning_download(self):
        downloader = Mock()
        downloader.extract_info.return_value = {'title': 'sample', 'acodec': 'mp4a.40.2'}
        downloader.prepare_filename.return_value = str(self.source)
        update = Mock()
        folder = self.root / 'job'
        with patch('core.YoutubeDL') as factory:
            factory.return_value.__enter__.return_value = downloader
            result = extract_audio('BV1bK411W797', folder, update, 200)
        self.assertEqual(result['filename'], 'sample.m4a')
        self.assertAlmostEqual(rms(samples(folder / 'audio.m4a')) / rms(samples(self.source)), 2, delta=.08)
        self.assertTrue(any('200%' in call.kwargs.get('message', '') for call in update.call_args_list))
        self.assertEqual([p.name for p in folder.iterdir()], ['audio.m4a'])

    def test_invalid_volume_rejected_before_ffmpeg_or_download(self):
        for volume in (None, True, '200', 0, 24, 401, 100.5, float('nan'), float('inf'), [], {}):
            with self.subTest(volume=volume), patch('core.ffmpeg') as process, patch('core.YoutubeDL') as download:
                with self.assertRaises(ValueError):
                    convert_audio(self.source, self.root / 'invalid.m4a', 'aac', volume_percent=volume)
                with self.assertRaises(ValueError):
                    extract_audio('BV1bK411W797', self.root / 'invalid', Mock(), volume)
                process.assert_not_called()
                download.assert_not_called()


class VolumeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.received = []
        def runner(url, folder, update, volume_percent):
            self.received.append(volume_percent)
            (folder / 'audio.m4a').write_bytes(b'audio')
            return {'title': 'sample', 'filename': 'sample.m4a', 'size': 5, 'duration': 1}
        self.app = create_app(self.temp.name, runner=runner)
        self.client = self.app.test_client()
        token = re.search(r'name="app-token" content="([^"]+)"', self.client.get('/').data.decode())[1]
        self.headers = {'X-App-Token': token}

    def tearDown(self):
        self.app.extensions['audio_pool'].shutdown(wait=True)
        self.temp.cleanup()

    def test_defaults_and_per_batch_volume_are_saved_and_forwarded(self):
        for body in ({'url': 'BV1bK411W797'}, {'urls': [
                'https://www.bilibili.com/video/BV1bK411W797?p=1',
                'https://www.bilibili.com/video/BV1bK411W797?p=2'], 'volume_percent': 200},
                {'url': 'BV1bK411W797', 'volume_percent': 50}):
            self.assertEqual(self.client.post('/api/jobs', json=body, headers=self.headers).status_code, 202)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            batches = self.client.get('/api/batches').json
            jobs = [job for batch in batches for job in batch['jobs']]
            if len(jobs) == 4 and all(job['status'] == 'done' for job in jobs):
                break
            time.sleep(.01)
        self.assertEqual(self.received, [100, 200, 200, 50])
        self.assertEqual([job['volume_percent'] for job in jobs], [50, 200, 200, 100])
        self.assertTrue(all(job['status'] == 'done' for job in jobs))

    def test_invalid_volume_creates_no_jobs_or_files(self):
        for volume in (None, True, '200', 0, 24, 401, 100.5, [], {}):
            response = self.client.post('/api/jobs', json={'url': 'BV1bK411W797', 'volume_percent': volume}, headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertIn('25', response.json['error'])
        self.assertEqual(self.client.get('/api/jobs').json, [])
        self.assertEqual(self.received, [])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
