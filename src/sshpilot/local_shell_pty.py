"""A local shell on its own pseudo terminal, for the built-in terminal backend.

The built-in backend (pyte or libvterm drawn by GTK) has no PTY of its own,
unlike VTE. Local shell tabs are owned by the frontend (see
docs/architecture/frontend-closure-audit.md), so this module is the
frontend's equivalent of the daemon's session runners:

* Windows: a pseudo console from :mod:`sshpilot.platform.windows.conpty`,
  with Microsoft's bundled ``conpty.dll`` when it is present;
* POSIX: :func:`pty.fork`, so the child is a session leader with the PTY as
  its controlling terminal.

Output is read on a thread and handed to ``on_output`` there; the caller
moves it to its main loop. ``on_exit(status)`` runs once, after the last
output. The emulator only parses bytes; this class owns the process, as in
emacs-libvterm and Neovim.

Nothing here imports GTK.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from typing import Callable, Mapping, Optional, Sequence

from .platform.process import kill_process_tree

logger = logging.getLogger(__name__)

OutputCallback = Callable[[bytes], None]
ExitCallback = Callable[[int], None]

_READ_SIZE = 64 * 1024


class LocalShellProcess:
    """One local shell. Not reusable: create a new one per spawn."""

    def __init__(self, on_output: OutputCallback, on_exit: ExitCallback) -> None:
        self._on_output = on_output
        self._on_exit = on_exit
        self._lock = threading.Lock()
        self._exited = False
        self.pid: Optional[int] = None
        self._impl: Optional[_Impl] = None

    # -- lifecycle -----------------------------------------------------------

    def start(
        self,
        argv: Sequence[str],
        *,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        columns: int = 80,
        rows: int = 24,
    ) -> int:
        if not argv:
            raise ValueError("a local shell needs a command")
        impl_type = _ConPtyImpl if sys.platform == "win32" else _PosixPtyImpl
        impl = impl_type()
        self.pid = impl.start(list(argv), dict(env) if env is not None else None, cwd,
                              max(1, int(columns)), max(1, int(rows)))
        self._impl = impl
        threading.Thread(
            target=self._read_loop, name=f"sshpilot-local-shell-{self.pid}", daemon=True
        ).start()
        return self.pid

    @property
    def pgid(self) -> Optional[int]:
        impl = self._impl
        return impl.pgid if impl is not None else None

    def write(self, data: bytes) -> None:
        impl = self._impl
        if impl is not None and data and not self._exited:
            try:
                impl.write(bytes(data))
            except OSError:
                logger.debug("Local shell write failed", exc_info=True)

    def resize(self, columns: int, rows: int) -> None:
        impl = self._impl
        if impl is not None and not self._exited:
            try:
                impl.resize(max(1, int(columns)), max(1, int(rows)))
            except OSError:
                logger.debug("Local shell resize failed", exc_info=True)

    def is_running(self) -> bool:
        return self._impl is not None and not self._exited

    def terminate(self) -> None:
        impl = self._impl
        if impl is not None and not self._exited:
            impl.terminate()

    def close(self) -> None:
        """End the shell (if still running) and release the terminal."""
        impl = self._impl
        if impl is not None:
            impl.close()

    # -- internals -----------------------------------------------------------

    def _read_loop(self) -> None:
        impl = self._impl
        assert impl is not None
        try:
            while True:
                data = impl.read(_READ_SIZE)
                if not data:
                    break
                try:
                    self._on_output(data)
                except Exception:
                    logger.exception("Local shell output handler failed")
        finally:
            self._finish(impl.wait())

    def _finish(self, status: int) -> None:
        with self._lock:
            if self._exited:
                return
            self._exited = True
        try:
            self._on_exit(status)
        except Exception:
            logger.exception("Local shell exit handler failed")


def windows_command_line(arguments: Sequence[str]) -> str:
    """Quote arguments the way the Microsoft C runtime parses them back.

    The same rules as ``subprocess.list2cmdline`` (kept local so the
    frontend does not import ``subprocess``): an empty argument or one with a
    space or tab is wrapped in quotes; a quote is escaped, and the
    backslashes before it doubled.
    """

    parts = []
    for argument in arguments:
        out = []
        backslashes = 0
        quote = not argument or " " in argument or "\t" in argument
        if quote:
            out.append('"')
        for char in argument:
            if char == "\\":
                backslashes += 1
            elif char == '"':
                out.append("\\" * (backslashes * 2) + '\\"')
                backslashes = 0
            else:
                out.append("\\" * backslashes + char)
                backslashes = 0
        if quote:
            out.append("\\" * (backslashes * 2) + '"')
        else:
            out.append("\\" * backslashes)
        parts.append("".join(out))
    return " ".join(parts)


class _Impl:
    pgid: Optional[int] = None

    def start(self, argv, env, cwd, columns, rows) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self, size: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError


class _PosixPtyImpl(_Impl):
    def __init__(self) -> None:
        self._fd = -1
        self._pid = 0
        self._status: Optional[int] = None

    def start(self, argv, env, cwd, columns, rows) -> int:
        import pty

        environment = dict(os.environ if env is None else env)
        pid, fd = pty.fork()
        if pid == 0:  # child: exec at once, nothing else in a forked GTK process
            try:
                if cwd:
                    os.chdir(cwd)
                os.execvpe(argv[0], argv, environment)
            finally:
                os._exit(127)
        self._pid, self._fd = pid, fd
        self.pgid = pid  # pty.fork made the child a session (and group) leader
        self.resize(columns, rows)
        return pid

    def read(self, size: int) -> bytes:
        try:
            return os.read(self._fd, size)
        except OSError:  # EIO once the child side closes on Linux
            return b""

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]

    def resize(self, columns: int, rows: int) -> None:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def wait(self) -> int:
        if self._status is None:
            try:
                _pid, status = os.waitpid(self._pid, 0)
                self._status = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                self._status = 0
        return self._status

    def terminate(self) -> None:
        try:
            os.killpg(self._pid, signal.SIGHUP)
        except OSError:
            pass

    def close(self) -> None:
        self.terminate()
        fd, self._fd = self._fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


class _ConPtyImpl(_Impl):
    def __init__(self) -> None:
        self._pty = None

    def start(self, argv, env, cwd, columns, rows) -> int:
        import shutil

        from sshpilot.platform.windows.conpty import CtypesPty

        environment = dict(os.environ if env is None else env)
        path = environment.get("PATH") or environment.get("Path") or os.defpath
        executable = shutil.which(argv[0], path=path) or argv[0]
        executable = executable.replace("/", "\\")
        command_line = windows_command_line(argv[1:])
        block = "\0".join(f"{key}={value}" for key, value in environment.items()) + "\0"
        pty = CtypesPty(columns, rows)
        pty.spawn(executable, (" " + command_line) if command_line else None, cwd=cwd, env=block)
        self._pty = pty
        return int(pty.pid)

    def read(self, size: int) -> bytes:
        try:
            return self._pty.read(size, blocking=True) or b""
        except EOFError:
            return b""

    def write(self, data: bytes) -> None:
        self._pty.write(data)

    def resize(self, columns: int, rows: int) -> None:
        self._pty.set_size(columns, rows)

    def wait(self) -> int:
        pty = self._pty
        while pty.isalive():
            threading.Event().wait(0.05)
        status = pty.get_exitstatus()
        return int(status) if status is not None else 0

    def terminate(self) -> None:
        try:
            if not kill_process_tree(int(self._pty.pid)):
                logger.debug("Local shell terminate could not inspect pid %s", self._pty.pid)
        except Exception:
            logger.debug("Local shell terminate failed", exc_info=True)

    def close(self) -> None:
        try:
            self._pty.close()
        except Exception:
            logger.debug("Local shell console close failed", exc_info=True)
