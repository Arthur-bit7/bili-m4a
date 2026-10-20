"""Files and in-flight media responses owned by one application run."""
from pathlib import Path
import threading


class SessionClosing(Exception):
    pass


class RunSession:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.condition = threading.Condition()
        self.owned = set()
        self.phase = 'running'
        self.error = ''
        self.transfers = 0
        self.finished = threading.Event()

    def status(self):
        with self.condition:
            return {'phase': self.phase, 'transfers': self.transfers, 'error': self.error}

    def acquire(self):
        with self.condition:
            if self.phase != 'running':
                raise SessionClosing('程序正在退出，不再接受提取或下载请求。')
            self.transfers += 1
        released = False
        def release():
            nonlocal released
            with self.condition:
                if not released:
                    released = True
                    self.transfers -= 1
                    self.condition.notify_all()
        return release

    def create_folder(self, name):
        # Only exclusively created direct children are owned. Never register an
        # existing folder, even if it happens to have the same generated name.
        path = self.root / name
        if path.resolve().parent != self.root or path.is_symlink():
            raise OSError('缓存目录路径不正确。')
        self.root.mkdir(parents=True, exist_ok=True)
        path.mkdir(exist_ok=False)
        with self.condition:
            self.owned.add(path)
        return path

    def remove_folder(self, path):
        import shutil
        path = Path(path)
        with self.condition:
            if path not in self.owned:
                return
        # Verify the absolute resolved target before every recursive deletion.
        # Refuse a directory replaced with a symlink/junction since registration.
        if path.resolve().parent != self.root or path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise OSError('缓存目录发生变化，已保留文件，请检查启动窗口。')
        if path.exists():
            shutil.rmtree(path)
        with self.condition:
            self.owned.discard(path)

    def begin_shutdown(self):
        with self.condition:
            if self.phase not in ('running', 'error'):
                return False
            self.phase = 'stopping'
            self.error = ''
            self.finished.clear()
            return True

    def cleanup(self):
        with self.condition:
            while self.transfers:
                self.condition.wait()
            self.phase = 'cleaning'
            paths = list(self.owned)
        for path in paths:
            self.remove_folder(path)
        with self.condition:
            self.phase = 'closed'
            self.finished.set()

    def fail(self, error):
        with self.condition:
            self.phase = 'error'
            self.error = str(error)
            self.finished.set()


class MediaResponse:
    """Release the file lease when a WSGI response ends or disconnects."""
    def __init__(self, iterable, release):
        self.iterable = iterable
        self.iterator = iter(iterable)
        self.release = release
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.iterator)
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                if hasattr(self.iterable, 'close'):
                    self.iterable.close()
            finally:
                self.release()
