"""A run lock that works across threads in this process and across processes."""

from __future__ import annotations

import fcntl
import threading
from pathlib import Path

_thread_lock = threading.Lock()


class RunLock:
    def __init__(self, handle):
        self._handle = handle

    def release(self) -> None:
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            _thread_lock.release()


def acquire_run_lock(path: Path) -> RunLock | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _thread_lock.acquire(blocking=False):
        return None
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        _thread_lock.release()
        return None
    return RunLock(handle)
