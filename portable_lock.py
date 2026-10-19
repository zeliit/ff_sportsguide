"""Process-wide POSIX lock, with a per-path thread lock fallback on Windows.

Thread fallback synchronizes threads in the same Python process. It is NOT
an inter-process lock; Windows multi-process deployments need a real file lock.
"""
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

_guard = threading.Lock()
_locks = {}


@contextmanager
def locked_file(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        with path.open("a+") as handle:
            mode = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), mode)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    key = str(path.resolve())
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=blocking):
        raise BlockingIOError("SportsGuide lock busy: " + path.name)
    try:
        yield
    finally:
        lock.release()
