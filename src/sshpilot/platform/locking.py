"""Process and cross-process locks for shared daemon settings files.

This module is deliberately GTK-free so the frontend configuration cache and
daemon services use the same transaction boundary without making the core
settings model depend on a frontend adapter.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict

from .fs import makedirs_private

try:  # pragma: no cover - exercised by cross-process tests on POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:  # pragma: no cover - Windows only
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

_CAN_LOCK = fcntl is not None or msvcrt is not None


def _lock_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    # msvcrt locks a byte range; LK_LOCK gives up after ~10 s, so keep
    # retrying to match flock's blocking semantics.
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            return
        except OSError:
            continue


def _unlock_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class CrossProcessRLock:
    """A re-entrant thread lock with an advisory lock-file boundary."""

    def __init__(self, path: Path) -> None:
        self._local = threading.RLock()
        self._lock_path = path.with_name(path.name + ".lock")
        self._state = threading.local()

    def acquire(self, *args, **kwargs):
        acquired = self._local.acquire(*args, **kwargs)
        if not acquired:
            return False
        depth = getattr(self._state, "depth", 0)
        if depth == 0 and _CAN_LOCK:
            makedirs_private(self._lock_path.parent)
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                _lock_fd(fd)
            except Exception:
                os.close(fd)
                self._local.release()
                raise
            self._state.fd = fd
        self._state.depth = depth + 1
        return True

    def release(self) -> None:
        depth = getattr(self._state, "depth", 0)
        if depth <= 0:
            raise RuntimeError("settings transaction lock is not held")
        self._state.depth = depth - 1
        if depth == 1 and _CAN_LOCK:
            fd = self._state.fd
            try:
                _unlock_fd(fd)
            finally:
                os.close(fd)
                del self._state.fd
        self._local.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False


_LOCKS: Dict[str, CrossProcessRLock] = {}
_LOCKS_GUARD = threading.Lock()


def settings_transaction_lock(path: Path | str) -> CrossProcessRLock:
    """Return the shared re-entrant transaction lock for *path*."""
    resolved = Path(path).resolve()
    key = str(resolved)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = CrossProcessRLock(resolved)
            _LOCKS[key] = lock
        return lock
