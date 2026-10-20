"""Bilibili extraction and verified, audio-only M4A output."""
from pathlib import Path
import json
import re
import subprocess
import tempfile
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from urllib.error import HTTPError, URLError

import imageio_ffmpeg
from mutagen.mp4 import MP4
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


class AudioError(Exception):
    pass


def normalize_url(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ValueError("请粘贴 B 站视频链接或 BV 号。")
    text = text.strip()
    if re.fullmatch(r"BV[0-9A-Za-z]{10}|av[0-9]+", text):
        return "https://www.bilibili.com/video/" + text + "?p=1"
    match = re.search(r"https?://[^\s<>\"，。]+", text)
    if not match:
        raise ValueError("没有找到有效链接，请复制完整的 B 站分享链接。")
    parsed = urlsplit(match.group().rstrip(")]）】"))
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError("链接格式不正确。")
    if parsed.hostname == "b23.tv" and re.fullmatch(r"/[A-Za-z0-9]+/?", parsed.path):
        return "https://b23.tv" + parsed.path
    if parsed.hostname not in ("bilibili.com", "www.bilibili.com", "m.bilibili.com"):
        raise ValueError("仅支持 bilibili.com 视频链接和 b23.tv 分享链接。")
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[0-9]+)/?", parsed.path)
    if not match:
        raise ValueError("请使用普通投稿视频链接（/video/BV…），暂不支持直播和番剧。")
    page = parse_qs(parsed.query).get("p", ["1"])[0]
    if not page.isdigit() or not 1 <= int(page) <= 10000:
        raise ValueError("分 P 参数必须是正整数。")
    return f"https://www.bilibili.com/video/{match.group(1)}?p={int(page)}"


def ffmpeg(args, timeout=1800):
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", *args],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise AudioError("音频处理失败：源文件不完整或编码不受支持，请重试。")
    return result


def verify_m4a(path):
    try:
        audio = MP4(path)
        if audio.info.length <= 0 or audio.info.channels < 1:
            raise AudioError("音频文件为空。")
        # Decode all streams: a successful remux alone cannot prove playability.
        ffmpeg(["-v", "error", "-xerror", "-i", str(path), "-map", "0", "-f", "null", "-"])
        return {"duration": round(audio.info.length, 2), "size": Path(path).stat().st_size}
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError("生成的文件未通过 M4A 校验，请重试。") from exc


def convert_audio(source, destination, codec, title="", uploader=""):
    # Explicit mapping excludes video, subtitles and artwork tracks.
    codec_args = ["-c:a", "copy"] if codec.startswith(("mp4a", "aac")) else ["-c:a", "aac", "-b:a", "192k"]
    ffmpeg(["-v", "error", "-y", "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-map_metadata", "-1", *codec_args, "-metadata", f"title={title}",
            "-metadata", f"artist={uploader}", "-movflags", "+faststart", "-f", "ipod", str(destination)])
    return verify_m4a(destination)


def safe_filename(title):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:100]
    return (cleaned or "B站音频") + ".m4a"


class QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


class VideoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Reject redirects outside supported video/share URLs before following.
        normalize_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def inspect_video(text):
    """Read the video's complete page list without downloading media."""
    url = normalize_url(text)
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    try:
        if urlsplit(url).hostname == "b23.tv":
            with build_opener(VideoRedirectHandler()).open(Request(url, headers=headers), timeout=20) as response:
                url = normalize_url(response.geturl())
            if urlsplit(url).hostname == "b23.tv":
                raise AudioError("短链未跳转到普通视频，请复制完整视频链接重试。")
        parsed = urlsplit(url)
        video_id = parsed.path.rstrip("/").split("/")[-1]
        query = {"bvid": video_id} if video_id.startswith("BV") else {"aid": video_id[2:]}
        endpoint = "https://api.bilibili.com/x/web-interface/view?" + urlencode(query)
        with urlopen(Request(endpoint, headers=headers), timeout=20) as response:
            payload = json.load(response)
        if payload.get("code") != 0:
            if payload.get("code") in (-352, -412, -799, -509):
                raise AudioError("B 站暂时限制了请求，请稍后再试。")
            raise AudioError("无法读取视频信息，视频可能不可用或需要额外权限。")
        data = payload.get("data") or {}
        if (data.get("rights") or {}).get("is_stein_gate"):
            raise AudioError("暂不支持互动视频，请使用普通投稿视频链接。")
        bvid = data.get("bvid", "")
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            raise AudioError("没有获取到有效的视频信息，请重试。")
        base = "https://www.bilibili.com/video/" + bvid
        parts = [{"page": page["page"], "title": page.get("part") or f"第 {page['page']} P",
                  "url": f"{base}?p={page['page']}"} for page in data.get("pages", [])]
        if not parts:
            raise AudioError("没有获取到分 P 信息，请重试。")
        selected = int(parse_qs(parsed.query).get("p", ["1"])[0])
        if selected not in [part["page"] for part in parts]:
            raise AudioError("链接指定的分 P 不存在，请修改链接后重试。")
        return {"title": data.get("title") or "B 站视频", "parts": parts, "selected_page": selected}
    except HTTPError as exc:
        raise AudioError("读取视频信息失败，B 站可能限制了请求，请稍后重试。") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AudioError("无法连接 B 站，请检查网络后点击获取重试。") from exc
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AudioError("视频信息格式异常，请稍后重试。") from exc


def extract_audio(url, folder, update):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / "audio.m4a"
    url = normalize_url(url)

    def hook(progress):
        if progress.get("status") == "downloading":
            total = progress.get("total_bytes") or progress.get("total_bytes_estimate")
            percent = min(99, round(progress.get("downloaded_bytes", 0) / total * 100)) if total else None
            update(status="downloading", progress=percent, message="正在下载音频…")

    options = {
        "format": "bestaudio[acodec^=mp4a]/bestaudio[acodec=aac]/bestaudio/best",
        "noplaylist": True, "playlist_items": "1", "quiet": True, "no_warnings": True,
        "logger": QuietLogger(), "progress_hooks": [hook], "socket_timeout": 20,
        "retries": 3, "fragment_retries": 3, "extractor_retries": 2,
        "concurrent_fragment_downloads": 1, "sleep_interval_requests": 1,
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "windowsfilenames": True, "overwrites": False,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="source-", dir=folder) as temporary:
            options["outtmpl"] = str(Path(temporary) / "source.%(ext)s")
            update(status="extracting", progress=None, message="正在解析视频信息…")
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
                if not info:
                    raise AudioError("没有获取到视频信息。")
                # Playlist handling is restricted to a single item, including short links.
                if "entries" in info:
                    entries = [entry for entry in info["entries"] if entry]
                    if len(entries) != 1:
                        raise AudioError("请选择具体分 P 的视频链接。")
                    info = entries[0]
                source = Path(downloader.prepare_filename(info))
            if not source.is_file():
                raise AudioError("未找到下载文件，视频可能不可用。")
            title = info.get("title") or info.get("id") or "B站音频"
            update(status="converting", title=title, progress=None, message="正在生成 M4A 并检查可播放性…")
            details = convert_audio(source, destination, info.get("acodec") or "", title, info.get("uploader") or "")
            return {**details, "title": title, "filename": safe_filename(title)}
    except DownloadError as exc:
        message = str(exc).lower()
        if any(word in message for word in ("412", "429", "rate limit", "403")):
            raise AudioError("B 站暂时限制了请求，请稍后再试；不要连续重复提交。") from exc
        if any(word in message for word in ("login", "premium", "supporter", "会员")):
            raise AudioError("该视频需要登录或额外权限；当前版本支持无需登录的公开视频。") from exc
        raise AudioError("解析或下载失败。请确认视频可正常打开，检查网络；也可以更新 yt-dlp 后重试。") from exc
