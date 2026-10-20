import io
import json
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from app import create_app
from core import AudioError, VideoRedirectHandler, inspect_video


class InspectTests(unittest.TestCase):
    def response(self, pages=2, code=0):
        return io.BytesIO(json.dumps({"code": code, "data": {
            "bvid": "BV1bK411W797", "title": "视频标题",
            "pages": [{"page": n, "part": f"章节 {n}"} for n in range(1, pages + 1)]
        }}).encode())

    @patch("core.urlopen")
    def test_full_list_preserves_requested_page(self, fetch):
        fetch.return_value = self.response()
        info = inspect_video("https://www.bilibili.com/video/BV1bK411W797?p=2")
        self.assertEqual(info["selected_page"], 2)
        self.assertEqual([p["title"] for p in info["parts"]], ["章节 1", "章节 2"])
        self.assertTrue(info["parts"][1]["url"].endswith("?p=2"))
        self.assertIn("bvid=BV1bK411W797", fetch.call_args.args[0].full_url)

    @patch("core.urlopen")
    def test_single_page_and_av(self, fetch):
        fetch.return_value = self.response(pages=1)
        info = inspect_video("av123")
        self.assertEqual(len(info["parts"]), 1)
        self.assertEqual(info["selected_page"], 1)
        self.assertIn("aid=123", fetch.call_args.args[0].full_url)

    @patch("core.urlopen")
    def test_missing_page_is_not_silently_replaced(self, fetch):
        fetch.return_value = self.response()
        with self.assertRaisesRegex(AudioError, "不存在"):
            inspect_video("https://www.bilibili.com/video/BV1bK411W797?p=3")

    @patch("core.urlopen")
    def test_network_and_api_errors(self, fetch):
        fetch.side_effect = URLError("offline")
        with self.assertRaisesRegex(AudioError, "网络"):
            inspect_video("BV1bK411W797")
        fetch.side_effect = None
        fetch.return_value = self.response(code=-412)
        with self.assertRaisesRegex(AudioError, "限制"):
            inspect_video("BV1bK411W797")
        fetch.return_value = io.BytesIO(b"not json")
        with self.assertRaisesRegex(AudioError, "格式异常"):
            inspect_video("BV1bK411W797")

    @patch("core.urlopen")
    @patch("core.build_opener")
    def test_short_link_resolves_page_before_inspection(self, opener, fetch):
        response = opener.return_value.open.return_value.__enter__.return_value
        response.geturl.return_value = "https://www.bilibili.com/video/BV1bK411W797?p=2"
        fetch.return_value = self.response()
        self.assertEqual(inspect_video("https://b23.tv/Ab1234")["selected_page"], 2)

    def test_external_short_redirect_rejected(self):
        with self.assertRaises(ValueError):
            VideoRedirectHandler().redirect_request(None, None, 302, "", {}, "https://example.com/private")


class VideoApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inspector = Mock(return_value={"title": "课程", "parts": [
            {"page": 1, "title": "第一讲", "url": "https://www.bilibili.com/video/BV1bK411W797?p=1"},
            {"page": 2, "title": "第二讲", "url": "https://www.bilibili.com/video/BV1bK411W797?p=2"},
        ], "selected_page": 2})
        self.runner = Mock()
        self.app = create_app(self.temp.name, runner=self.runner, inspector=self.inspector)
        self.client = self.app.test_client()
        page = self.client.get("/").data.decode()
        self.headers = {"X-App-Token": re.search(r'name="app-token" content="([^"]+)"', page)[1]}

    def tearDown(self):
        self.app.extensions["audio_pool"].shutdown(wait=True)
        self.temp.cleanup()

    def test_preview_creates_no_job_or_download(self):
        response = self.client.post("/api/video", json={"url": "BV1bK411W797"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["parts"]), 2)
        self.assertEqual(self.client.get("/api/jobs").json, [])
        self.runner.assert_not_called()

    def test_protection_validation_and_retry(self):
        self.assertEqual(self.client.post("/api/video", json={"url": "BV1bK411W797"}).status_code, 403)
        self.assertEqual(self.client.post("/api/video", json={"url": "https://example.com"}, headers=self.headers).status_code, 400)
        self.inspector.assert_not_called()
        self.inspector.side_effect = AudioError("读取失败")
        self.assertEqual(self.client.post("/api/video", json={"url": "BV1bK411W797"}, headers=self.headers).status_code, 502)
        self.inspector.side_effect = None
        self.assertEqual(self.client.post("/api/video", json={"url": "BV1bK411W797"}, headers=self.headers).status_code, 200)


if __name__ == "__main__":
    unittest.main()
