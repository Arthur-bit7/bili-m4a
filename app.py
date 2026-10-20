"""Local-only web UI. Run: python app.py"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import argparse
import secrets
import threading
import time
import zipfile
import uuid
import webbrowser

from flask import Flask, abort, jsonify, render_template, request, send_file
from waitress import create_server

from core import AudioError, extract_audio, inspect_video, normalize_url, safe_filename
from session import MediaResponse, RunSession, SessionClosing

ROOT = Path(__file__).resolve().parent
TERMINAL = {"done", "error", "cancelled"}
MAX_BATCH = 50
MAX_PENDING = 100
MAX_HISTORY = 500


def console_status(message):
    """Console diagnostics must never prevent startup or graceful shutdown."""
    try:
        print(message, flush=True)
    except (UnicodeError, OSError, ValueError):
        # Redirected Windows output may reject Chinese text; a pipe or stream
        # may also be closed. Cleanup and the stop signal must still run.
        pass


def create_app(data_dir=None, runner=extract_audio, inspector=inspect_video):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16384
    app.config["MAX_HISTORY"] = MAX_HISTORY
    token = secrets.token_urlsafe(32)
    session = RunSession(data_dir or ROOT / "downloads")
    data_dir = session.root
    app.extensions["audio_session"] = session
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
        if session.status()["phase"] != "running":
            return jsonify(error="程序正在退出，无法解析新链接。"), 503
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

    def finish_shutdown():
        try:
            pool.shutdown(wait=True, cancel_futures=True)
            session.cleanup()
            console_status("本次运行缓存已清理，旧文件保留。")
            callback = app.extensions.get("audio_stop_server")
            if callback:
                callback()
        except Exception as exc:
            session.fail("清理未完成，未删除的缓存仍保留。请检查磁盘或文件占用后重试。")
            app.logger.exception("Session cleanup failed: %s", exc)

    def start_shutdown():
        with lock:
            if not session.begin_shutdown():
                return
            for job in jobs.values():
                if job["status"] == "queued":
                    job.update(status="cancelled", progress=None, message="退出程序，已取消等待")
        threading.Thread(target=finish_shutdown, name="cleanup", daemon=True).start()

    app.extensions["audio_shutdown"] = start_shutdown

    @app.get("/api/session")
    def session_status():
        with lock:
            result = session.status()
            result["pending"] = sum(job["status"] not in TERMINAL for job in jobs.values())
            return jsonify(result)

    @app.post("/api/shutdown")
    def shutdown():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("confirmed") is not True:
            return jsonify(error="请确认已保存需要保留的音频，再退出并清理本次缓存。"), 400
        start_shutdown()
        return jsonify(phase=session.status()["phase"]), 202

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
            session.create_folder(job_id)
            result = runner(url, folder, update)
            update(**result, status="done", progress=100, message="音频已就绪", download=f"/audio/{job_id}")
        except Exception as exc:
            # Finish cleanup before publishing the terminal state.
            try:
                session.remove_folder(folder)
            except OSError:
                app.logger.exception("Failed to remove task cache")
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
            if session.status()["phase"] != "running":
                return jsonify(error="程序正在退出，无法提交新任务。"), 503
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

    def media_response(path, release, **options):
        try:
            response = send_file(path, **options)
            if request.method == "HEAD" or response.status_code == 304:
                response.close()
                release()
            else:
                response.response = MediaResponse(response.response, release)
            return response
        except BaseException:
            release()
            raise

    @app.errorhandler(SessionClosing)
    def closing_error(error):
        return jsonify(error=str(error)), 503

    @app.get("/api/batches/<batch_id>/zip")
    def batch_zip(batch_id):
        release = session.acquire()
        try:
            with lock:
                batch = batches.get(batch_id)
                if not batch:
                    abort(404)
                snapshot = [dict(jobs[job_id]) for job_id in batch["ids"] if jobs[job_id]["status"] == "done"]
                title = batch["title"]
            if not snapshot:
                release()
                return jsonify(error="当前批次还没有已完成的音频。"), 409
            folder = session.create_folder("zip-" + uuid.uuid4().hex)
            archive = folder / "audio.zip"
            # AAC is already compressed; store entries without another costly
            # compression pass. The snapshot excludes queued/failed/cancelled jobs.
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
                for job in sorted(snapshot, key=lambda entry: entry["page"]):
                    source = data_dir / job["id"] / "audio.m4a"
                    output.write(source, f"P{job['page']:03d}_{safe_filename(job['title'])}")
            name = safe_filename(title)[:-4] + f"_{len(snapshot)}个音频.zip"
        except (OSError, zipfile.BadZipFile):
            release()
            return jsonify(error="打包失败，请检查磁盘空间后重试。"), 500
        except BaseException:
            release()
            raise
        # An empty snapshot returns above without a file iterator to close.
        return media_response(archive, release, mimetype="application/zip", as_attachment=True,
                              download_name=name, conditional=True)

    @app.get("/audio/<job_id>")
    def audio(job_id):
        release = session.acquire()
        try:
            with lock:
                job = dict(jobs.get(job_id, {}))
            if job.get("status") != "done":
                abort(404)
            path = data_dir / job_id / "audio.m4a"
            if not path.is_file():
                abort(404)
        except BaseException:
            release()
            raise
        return media_response(path, release, mimetype="audio/mp4", as_attachment=request.args.get("download") == "1",
                              download_name=job["filename"], conditional=True)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bilibili to M4A — 本地音频提取工具")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    app = create_app()
    url = f"http://127.0.0.1:{args.port}"
    console_status(f"Bili M4A: {url}\nPress Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    stop_server = threading.Event()
    app.extensions["audio_stop_server"] = stop_server.set
    server = create_server(app, host="127.0.0.1", port=args.port, threads=4)
    server_thread = threading.Thread(target=server.run, name="http", daemon=True)
    server_thread.start()
    while not stop_server.is_set():
        try:
            stop_server.wait(.2)
        except KeyboardInterrupt:
            console_status("正在退出：取消等待任务，等待当前提取和传输结束后清理本次缓存。")
            app.extensions["audio_shutdown"]()
    # Allow the page to read the final cleanup status before closing HTTP.
    time.sleep(1)
    server.accepting = False
    for channel in list(server.active_channels.values()):
        channel.close_when_flushed = True
    # WSGI iterators can finish before their buffered bytes reach the socket.
    # Keep the HTTP loop alive until those existing channels have drained.
    while server.active_channels:
        time.sleep(.1)
    server.task_dispatcher.shutdown()
    server.asyncore.close_all(server._map)
    server_thread.join(timeout=2)
