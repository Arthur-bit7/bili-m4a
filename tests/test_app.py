import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import imageio_ffmpeg
from mutagen.mp4 import MP4

from app import create_app
from core import AudioError, convert_audio, ffmpeg, normalize_url, verify_m4a


class UrlTests(unittest.TestCase):
    def test_normal_and_share(self):
        self.assertEqual(normalize_url("BV1bK411W797"), "https://www.bilibili.com/video/BV1bK411W797?p=1")
        self.assertEqual(normalize_url("分享 https://www.bilibili.com/video/BV1bK411W797/?p=2&share=x"), "https://www.bilibili.com/video/BV1bK411W797?p=2")
        self.assertEqual(normalize_url("看看 https://b23.tv/Ab1234。"), "https://b23.tv/Ab1234")

    def test_reject_non_video_inputs(self):
        for value in (None, "", "https://example.com/video/BV1bK411W797", "https://www.bilibili.com.evil.com/video/BV1bK411W797", "https://www.bilibili.com/video/BV1bK411W797?p=0", "https://user@www.bilibili.com/video/BV1bK411W797", "https://www.bilibili.com/bangumi/play/ep1", "file:///etc/passwd"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_url(value)


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_aac_copy_removes_video_and_decodes(self):
        source, target = self.root / "source.mp4", self.root / "audio.m4a"
        ffmpeg(["-v", "error", "-f", "lavfi", "-i", "color=c=black:s=32x32:d=1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(source)])
        result = convert_audio(source, target, "mp4a.40.2", "测试标题")
        self.assertGreater(result["duration"], 0.9)
        self.assertEqual(MP4(target)["\xa9nam"], ["测试标题"])
        inspected = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(target)], capture_output=True)
        self.assertNotIn(b"Video:", inspected.stderr)
        self.assertIn(b"Audio: aac", inspected.stderr)
        # Compare decoded PCM bytes: remuxing must preserve the audio samples.
        a = ffmpeg(["-v", "error", "-i", str(source), "-map", "0:a:0", "-f", "s16le", "-"]).stdout
        b = ffmpeg(["-v", "error", "-i", str(target), "-map", "0:a:0", "-f", "s16le", "-"]).stdout
        self.assertEqual(a, b)

    def test_wav_transcodes_to_aac(self):
        source, target = self.root / "source.wav", self.root / "audio.m4a"
        ffmpeg(["-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(source)])
        convert_audio(source, target, "pcm_s16le")
        self.assertEqual(MP4(target).info.codec, "mp4a.40.2")

    def test_bad_media_rejected(self):
        target = self.root / "bad.m4a"
        target.write_bytes(b"not an audio file")
        with self.assertRaises(AudioError):
            verify_m4a(target)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.app.extensions["audio_pool"].shutdown(wait=True)
        self.temp.cleanup()

    def setup_app(self, runner):
        self.app = create_app(self.temp.name, runner=runner)
        self.client = self.app.test_client()
        page = self.client.get("/").data.decode()
        self.token = re.search(r'name="app-token" content="([^"]+)"', page)[1]

    def submit(self):
        return self.client.post("/api/jobs", json={"url": "BV1bK411W797"}, headers={"X-App-Token": self.token})

    def wait(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            jobs = self.client.get("/api/jobs").json
            if jobs and jobs[0]["status"] in ("done", "error"):
                return jobs[0]
            time.sleep(.02)
        self.fail("worker timed out")

    def test_local_protection(self):
        self.setup_app(lambda *args: {})
        self.assertEqual(self.client.post("/api/jobs", json={"url":"BV1bK411W797"}).status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host":"evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/jobs", json={}, headers={"X-App-Token":self.token}).status_code, 400)
        self.assertEqual(self.client.get("/audio/unknown").status_code, 404)

    def test_download_and_range(self):
        def runner(url, folder, update):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "audio.m4a").write_bytes(b"0123456789")
            return {"title":"测试", "filename":"测试.m4a", "duration":1, "size":10}
        self.setup_app(runner)
        self.assertEqual(self.submit().status_code, 202)
        job = self.wait()
        self.assertEqual(job["status"], "done")
        response = self.client.get(job["download"] + "?download=1")
        self.assertEqual(response.mimetype, "audio/mp4")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        response.close()
        response = self.client.get(job["download"], headers={"Range":"bytes=0-3"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"0123")
        response.close()

    def test_error_and_cleanup(self):
        def runner(url, folder, update):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "partial.m4a").write_bytes(b"bad")
            raise AudioError("测试错误")
        self.setup_app(runner)
        self.submit()
        job = self.wait()
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["message"], "测试错误")
        self.app.extensions["audio_pool"].shutdown(wait=True)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
