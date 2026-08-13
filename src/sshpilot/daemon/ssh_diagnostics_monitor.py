"""Event-driven file monitor for daemon SSH diagnostic files (inotify).

A single daemon-scoped thread owns one inotify descriptor plus a wakeup pipe
registered with a selector.  Sessions register their diagnostic file, the
monitor reads appended bytes to EOF on every write event (coalescing bursts),
and delivers raw chunks to a per-session callback.  On unregister the file is
drained to EOF *before* the watch is removed, so a write that races process
exit is never lost.  Non-Linux platforms have no inotify: the monitor reports
``available() == False`` and the daemon degrades to the PTY evidence path —
never a polling replacement.

The monitor is intentionally dumb (bytes in, bytes out); the trusted parser
and per-session bookkeeping live in ``ssh_readiness.SshReadinessManager``.
"""

from __future__ import annotations

import ctypes
import os
import queue
import selectors
import stat
import struct
import threading
from typing import Callable, Dict, Optional

_INOTIFY_LIBC = ctypes.CDLL(None, use_errno=True)
for _name, _restype, _argtypes in (
    ("inotify_init1", ctypes.c_int, [ctypes.c_int]),
    ("inotify_add_watch", ctypes.c_int, [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]),
    ("inotify_rm_watch", ctypes.c_int, [ctypes.c_int, ctypes.c_int]),
):
    _fn = getattr(_INOTIFY_LIBC, _name)
    _fn.restype = _restype
    _fn.argtypes = _argtypes

IN_NONBLOCK = getattr(os, "O_NONBLOCK", 0x800)
IN_CLOEXEC = getattr(os, "O_CLOEXEC", 0x80000)
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_IGNORED = 0x00008000

_WATCH_MASK = IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_DELETE_SELF | IN_MOVE_SELF

_READ_CHUNK = 64 * 1024


def _inotify_init() -> int:
    fd = _INOTIFY_LIBC.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 failed")
    return fd


def inotify_available() -> bool:
    """Return whether an inotify descriptor can be created on this platform."""
    if not hasattr(_INOTIFY_LIBC, "inotify_init1"):
        return False
    try:
        fd = _inotify_init()
    except OSError:
        return False
    os.close(fd)
    return True


class _WatchState:
    __slots__ = ("path", "fd", "offset", "removed", "on_data", "identity")

    def __init__(self, path: str, fd: int, on_data) -> None:
        self.path = path
        self.fd = fd
        self.offset = 0
        self.removed = False
        self.on_data = on_data
        try:
            info = os.fstat(fd)
            self.identity = (info.st_dev, info.st_ino)
        except OSError:
            self.identity = None

    def drain(self) -> None:
        """Read everything appended since the last read and deliver it."""
        while True:
            try:
                info = os.fstat(self.fd)
            except OSError:
                self.removed = True
                return
            if info.st_size < self.offset:
                # File was truncated/replaced underneath us: start over.
                self.offset = 0
            if info.st_size == self.offset:
                return
            try:
                data = os.read(self.fd, _READ_CHUNK)
            except OSError:
                self.removed = True
                return
            if not data:
                return
            self.offset += len(data)
            try:
                self.on_data(data)
            except Exception:
                continue


class SshDiagnosticsMonitor:
    """Own one inotify descriptor and all diagnostic-file reads on one thread."""

    def __init__(self) -> None:
        if not inotify_available():
            raise RuntimeError("inotify is unavailable on this platform")
        self._fd = _inotify_init()
        self._wakeup_read, self._wakeup_write = os.pipe()
        os.set_blocking(self._wakeup_read, False)
        os.set_blocking(self._wakeup_write, False)
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._wakeup_read, selectors.EVENT_READ, None)
        self._selector.register(self._fd, selectors.EVENT_READ, "inotify")
        self._watches: Dict[int, _WatchState] = {}
        self._by_key: Dict[object, _WatchState] = {}
        self._commands: queue.Queue[tuple] = queue.Queue(maxsize=1024)
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="sshpilot-diagnostics-monitor",
            daemon=True,
        )
        self._thread.start()

    def register(
        self,
        key: object,
        path: str,
        on_data: Callable[[bytes], None],
    ) -> None:
        """Watch *path* and deliver appended bytes (existing content included)."""
        self._submit(("register", key, path, on_data))

    def unregister(self, key: object, *, unlink_path: Optional[str] = None) -> None:
        """Drain the file to EOF, remove the watch, and optionally unlink it."""
        self._submit(("unregister", key, unlink_path))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._submit(("close",), allow_closed=True)
        except RuntimeError:
            try:
                os.write(self._wakeup_write, b"x")
            except OSError:
                pass
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)
        for descriptor in (self._wakeup_read, self._wakeup_write):
            try:
                os.close(descriptor)
            except OSError:
                pass

    # -- internals ----------------------------------------------------------

    def _submit(self, command: tuple, *, allow_closed: bool = False) -> None:
        if self._closed and not allow_closed:
            raise RuntimeError("diagnostics monitor is closed")
        try:
            self._commands.put_nowait(command)
        except queue.Full:
            raise RuntimeError("diagnostics monitor command queue is full") from None
        try:
            os.write(self._wakeup_write, b"x")
        except OSError:
            if not self._closed:
                raise

    def _run(self) -> None:
        while True:
            try:
                ready = self._selector.select(0.5)
            except OSError:
                return
            for key, _mask in ready:
                if key.data is None:
                    try:
                        while os.read(self._wakeup_read, 1024):
                            pass
                    except (BlockingIOError, OSError):
                        pass
                    if self._drain_commands():
                        return
                    continue
                if key.data == "inotify":
                    self._consume_inotify()

    def _drain_commands(self) -> bool:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return False
            kind = command[0]
            if kind == "close":
                for watch in tuple(self._watches.values()):
                    self._drop_watch(watch, unlink_path=watch.path)
                self._selector.close()
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                return True
            if kind == "register":
                self._handle_register(command[1], command[2], command[3])
            elif kind == "unregister":
                self._handle_unregister(command[1], command[2])

    def _handle_register(self, key: object, path: str, on_data) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            return
        watch = _WatchState(path, fd, on_data)
        encoded = os.fsencode(path)
        wd = _INOTIFY_LIBC.inotify_add_watch(self._fd, encoded, _WATCH_MASK)
        if wd < 0:
            os.close(fd)
            return
        self._watches[wd] = watch
        self._by_key[key] = watch
        # A marker may already have been written before the watch was armed:
        # read whatever exists now so no event is lost.
        watch.drain()

    def _handle_unregister(self, key: object, unlink_path: Optional[str]) -> None:
        watch = self._by_key.pop(key, None)
        if watch is None:
            return
        watch.drain()
        self._drop_watch(watch, unlink_path=unlink_path)

    def _consume_inotify(self) -> None:
        try:
            buf = os.read(self._fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            self._selector.close()
            return
        offset = 0
        while offset + 16 <= len(buf):
            wd, mask, _cookie, name_len = struct.unpack_from("@iiiI", buf, offset)
            offset += 16 + name_len
            watch = self._watches.get(wd)
            if watch is None:
                continue
            if mask & (IN_DELETE_SELF | IN_MOVE_SELF):
                watch.drain()
                watch.removed = True
                self._drop_watch(watch, unlink_path=None)
                continue
            if mask & (IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE):
                watch.drain()

    def _drop_watch(self, watch: _WatchState, *, unlink_path: Optional[str]) -> None:
        for wd, candidate in tuple(self._watches.items()):
            if candidate is watch:
                _INOTIFY_LIBC.inotify_rm_watch(self._fd, wd)
                self._watches.pop(wd, None)
                break
        try:
            os.close(watch.fd)
        except OSError:
            pass
        if unlink_path is not None and not watch.removed:
            self._unlink_verified(unlink_path, watch.identity)

    @staticmethod
    def _unlink_verified(path: str, identity) -> None:
        try:
            info = os.lstat(path)
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode):
            return
        if identity is not None and (info.st_dev, info.st_ino) != identity:
            return
        try:
            os.unlink(path)
        except OSError:
            return
