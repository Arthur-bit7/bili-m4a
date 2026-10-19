"""Local-only web UI. Run: python app.py"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import argparse
import secrets
import shutil
import threading
import uuid
import webbrowser

from flask import Flask, abort, jsonify, render_template, request, send_file
from waitress import serve

from core import AudioError, extract_audio, normalize_url

ROOT = Path(__file__).resolve().parent


def create_app(data_dir=None, runner=extract_audio):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8192
    token = secrets.token_urlsafe(32)
    data_dir = Path(data_dir or ROOT / "downloads")
    jobs = {}
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

    @app.get("/api/jobs")
    def list_jobs():
        with lock:
            return jsonify(list(reversed([dict(job) for job in jobs.values()])))

    def execute(job_id, url):
        folder = data_dir / job_id

        def update(**values):
            with lock:
                jobs[job_id].update(values)

        try:
            result = runner(url, folder, update)
            update(**result, status="done", progress=100, message="音频已就绪", download=f"/audio/{job_id}")
        except Exception as exc:
            update(status="error", progress=None, message=str(exc) if isinstance(exc, (AudioError, ValueError)) else "处理失败，请检查磁盘空间后重试。")
            shutil.rmtree(folder, ignore_errors=True)

    @app.post("/api/jobs")
    def new_job():
        body = request.get_json(silent=True)
        try:
            url = normalize_url(body.get("url") if isinstance(body, dict) else None)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        with lock:
            if sum(job["status"] not in ("done", "error") for job in jobs.values()) >= 5:
                return jsonify(error="队列已满，请等当前任务完成。"), 429
            if len(jobs) >= 100:
                return jsonify(error="本次运行已达到 100 条记录，请重启工具后继续。已有音频保留在 downloads 文件夹。"), 429
            job_id = uuid.uuid4().hex
            jobs[job_id] = {"id": job_id, "url": url, "title": "B 站音频", "status": "queued", "progress": None,
                            "message": "等待处理…", "created": datetime.now().strftime("%H:%M")}
        pool.submit(execute, job_id, url)
        return jsonify(id=job_id), 202

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
    serve(app, host="127.0.0.1", port=args.port, threads=4)
