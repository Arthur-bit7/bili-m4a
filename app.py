"""Local-only web UI. Run: python app.py"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import argparse
import secrets
import shutil
import threading
import uuid
import webbrowser

from flask import Flask, abort, jsonify, render_template, request, send_file
from waitress import serve

from core import AudioError, extract_audio, inspect_video, normalize_url

ROOT = Path(__file__).resolve().parent
TERMINAL = {"done", "error", "cancelled"}
MAX_BATCH = 50
MAX_PENDING = 100
MAX_HISTORY = 500


def create_app(data_dir=None, runner=extract_audio, inspector=inspect_video):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16384
    app.config["MAX_HISTORY"] = MAX_HISTORY
    token = secrets.token_urlsafe(32)
    data_dir = Path(data_dir or ROOT / "downloads")
    jobs = {}
    batches = {}
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio")
    app.extensions["audio_pool"] = pool

    @app.before_request
    def local_only():
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            abort(403)
        if request.method == "POST" and not secrets.compare_digest(request.headers.get("X-App-Token", ""), token):
            abort(403)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return render_template("index.html", token=token)

    @app.post("/api/video")
    def video_info():
        body = request.get_json(silent=True)
        try:
            url = normalize_url(body.get("url") if isinstance(body, dict) else None)
            return jsonify(inspector(url))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except AudioError as exc:
            return jsonify(error=str(exc)), 502
        except Exception:
            return jsonify(error="读取视频信息失败，请稍后点击获取重试。"), 502

    @app.get("/api/jobs")
    def list_jobs():
        with lock:
            return jsonify(list(reversed([dict(job) for job in jobs.values()])))

    def trim_history():
        # Called under lock. Keep active batches intact, and evict whole finished
        # batches so their totals and pagination never describe partial records.
        finished = [batch_id for batch_id, batch in batches.items()
                    if all(jobs[job_id]["status"] in TERMINAL for job_id in batch["ids"])]
        count = sum(len(batches[batch_id]["ids"]) for batch_id in finished)
        for batch_id in finished:
            if count <= app.config["MAX_HISTORY"]:
                break
            batch = batches.pop(batch_id)
            count -= len(batch["ids"])
            for job_id in batch["ids"]:
                jobs.pop(job_id)

    @app.get("/api/batches")
    def list_batches():
        with lock:
            result = []
            for batch in reversed(list(batches.values())):
                entries = [dict(jobs[job_id]) for job_id in batch["ids"]]
                result.append({"id": batch["id"], "title": batch["title"], "total": len(entries),
                               "created": batch["created"], "jobs": entries})
            return jsonify(result)

    def execute(job_id, url):
        with lock:
            if jobs.get(job_id, {}).get("status") != "queued":
                return
            # Claim the job before releasing the lock; cancellation can only win
            # while it is still queued, never after media processing has begun.
            jobs[job_id].update(status="extracting", message="正在解析视频信息…")
        folder = data_dir / job_id

        def update(**values):
            with lock:
                jobs[job_id].update(values)

        try:
            result = runner(url, folder, update)
            update(**result, status="done", progress=100, message="音频已就绪", download=f"/audio/{job_id}")
        except Exception as exc:
            # Finish cleanup before publishing the terminal state.
            shutil.rmtree(folder, ignore_errors=True)
            update(status="error", progress=None, message=str(exc) if isinstance(exc, (AudioError, ValueError)) else "处理失败，请检查磁盘空间后重试。")
        finally:
            with lock:
                trim_history()

    @app.post("/api/jobs")
    def new_job():
        body = request.get_json(silent=True)
        try:
            if not isinstance(body, dict):
                raise ValueError("请提交视频链接。")
            raw = body.get("urls") if "urls" in body else [body.get("url")]
            if not isinstance(raw, list) or not raw:
                raise ValueError("请至少选择一个分 P。")
            if len(raw) > MAX_BATCH:
                raise ValueError(f"每批最多选择 {MAX_BATCH} 个分 P，请分批提交。")
            urls = [normalize_url(value) for value in raw]
            if len(set(urls)) != len(urls):
                raise ValueError("同一批次不能包含重复的分 P。")
            if len(urls) > 1 and (len({url.split("?")[0] for url in urls}) != 1
                                  or any(urlsplit(url).hostname == "b23.tv" for url in urls)):
                raise ValueError("每批请选择同一个视频的分 P。")
            urls.sort(key=lambda url: int(parse_qs(urlsplit(url).query).get("p", ["1"])[0]))
            title = body.get("title", "B 站音频")
            if not isinstance(title, str):
                raise ValueError("视频标题格式不正确。")
            title = title.strip()[:200] or "B 站音频"
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        with lock:
            remaining = MAX_PENDING - sum(job["status"] not in TERMINAL for job in jobs.values())
            if len(urls) > remaining:
                return jsonify(error=f"队列还可容纳 {remaining} 个任务，请减少选择或等待任务完成。"), 429
            trim_history()
            batch_id = uuid.uuid4().hex
            created = datetime.now().strftime("%H:%M")
            ids = []
            for url in urls:
                job_id = uuid.uuid4().hex
                page = int(parse_qs(urlsplit(url).query).get("p", ["1"])[0])
                ids.append(job_id)
                jobs[job_id] = {"id": job_id, "batch_id": batch_id, "page": page,
                                "url": url, "title": title, "status": "queued",
                                "progress": None, "message": "等待处理…", "created": created}
            batches[batch_id] = {"id": batch_id, "title": title, "created": created, "ids": ids}
            # Submission also happens under lock to keep concurrent batches from
            # interleaving their page order in the single-worker executor.
            for job_id, url in zip(ids, urls):
                pool.submit(execute, job_id, url)
        return jsonify(id=ids[0], ids=ids, batch_id=batch_id), 202

    def cancel_queued(ids):
        count = 0
        for job_id in ids:
            if jobs[job_id]["status"] == "queued":
                jobs[job_id].update(status="cancelled", progress=None, message="已取消等待")
                count += 1
        trim_history()
        return count

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id):
        with lock:
            if job_id not in jobs:
                abort(404)
            if jobs[job_id]["status"] != "queued":
                return jsonify(error="只能取消等待中的任务。"), 409
            return jsonify(cancelled=cancel_queued([job_id]))

    @app.post("/api/batches/<batch_id>/cancel")
    def cancel_batch(batch_id):
        with lock:
            if batch_id not in batches:
                abort(404)
            return jsonify(cancelled=cancel_queued(batches[batch_id]["ids"]))

    @app.get("/audio/<job_id>")
    def audio(job_id):
        with lock:
            job = dict(jobs.get(job_id, {}))
        if job.get("status") != "done":
            abort(404)
        path = data_dir / job_id / "audio.m4a"
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="audio/mp4", as_attachment=request.args.get("download") == "1",
                         download_name=job["filename"], conditional=True)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bilibili to M4A — 本地音频提取工具")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    app = create_app()
    url = f"http://127.0.0.1:{args.port}"
    print(f"Bili M4A: {url}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        serve(app, host="127.0.0.1", port=args.port, threads=4)
    finally:
        app.extensions["audio_pool"].shutdown(wait=False, cancel_futures=True)
