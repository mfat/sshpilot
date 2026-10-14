"""Windows ConPTY session runner (the counterpart of ``pty_runner`` on POSIX).

Each session gets its own pseudo console through pywinpty, which prefers the
native ConPTY API. One reader thread per session forwards console output; a
shared reaper thread notices process exit. The handle implements the same
surface :class:`~sshpilot.daemon.session_runtime.SessionRuntime` uses on the
POSIX runner: ``write``, ``resize``, ``terminate``, ``kill``, ``wait`` and
``poll_and_notify``.

pywinpty 3.x speaks ``str`` (ConPTY emits UTF-8 VT sequences) while 2.x,
which MSYS2 packages, speaks UTF-8 ``bytes``. Both are bridged here so the
rest of the daemon keeps handling bytes.
"""

from __future__ import annotations

import codecs
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Mapping, Optional, Sequence

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.sessions import SessionExitInfo
from sshpilot.api.models.terminal import TerminalDimensions

from .process_registry import (
    KIND_SESSION,
    OwnershipNotRecorded,
    forget_owned_process,
    record_owned_process,
)
from .session_runtime import (
    SessionExitCallback,
    SessionLaunchSpec,
    SessionProcessHandle,
)

logger = logging.getLogger(__name__)

DEFAULT_TERMINAL_INPUT_BYTES = 256 * 1024
_REAP_INTERVAL = 0.05
# After the process exits, how long the reaper waits for the reader to drain
# the console's final output before declaring EOF itself.
_EOF_DRAIN_TIMEOUT = 1.0

TerminalOutputCallback = Callable[[bytes], None]
TerminalEofCallback = Callable[[], None]
LaunchBuilder = Callable[
    [SessionLaunchSpec],
    tuple[Sequence[str], Mapping[str, str]],
]


def conpty_available() -> bool:
    try:
        import winpty  # noqa: F401
    except ImportError:
        return False
    return True


def _resolve_executable(command: str, environment: Mapping[str, str]) -> str:
    resolved = shutil.which(command, path=environment.get("PATH") or os.defpath)
    if resolved is None:
        raise FileNotFoundError(command)
    return resolved


def _environment_block(environment: Mapping[str, str]) -> str:
    return "\0".join(f"{key}={value}" for key, value in environment.items()) + "\0"


def _terminate_tree(pid: int) -> None:
    """Kill ``pid`` and its descendants (ProxyCommand/ProxyJump helpers)."""

    try:
        import psutil
    except ImportError:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        return
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    except psutil.Error:
        return
    for process in (*children, parent):
        try:
            process.kill()
        except psutil.Error:
            continue


class ConPtyProcessHandle:
    """One ssh.exe (or other client) attached to its own pseudo console."""

    def __init__(
        self,
        pty,
        on_exit: SessionExitCallback,
        on_output: TerminalOutputCallback,
        on_eof: TerminalEofCallback,
        unregister: Callable[["ConPtyProcessHandle"], None],
        *,
        input_limit: int = DEFAULT_TERMINAL_INPUT_BYTES,
    ) -> None:
        self._pty = pty
        self.pid: Optional[int] = pty.pid
        self._on_exit = on_exit
        self._on_output = on_output
        self._on_eof = on_eof
        self._unregister = unregister
        self._input_limit = input_limit
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._input_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._exit_info: Optional[SessionExitInfo] = None
        self._exit_notified = False
        self._eof_notified = False
        self._eof_event = threading.Event()
        self._exited = threading.Event()
        self.last_dimensions: Optional[TerminalDimensions] = None
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"sshpilot-conpty-read-{self.pid}",
            daemon=True,
        )

    def start_reading(self) -> None:
        self._reader.start()

    # -- SessionProcessHandle -------------------------------------------------

    def terminate(self) -> None:
        # Windows has no SIGTERM for console programs; closing the pseudo
        # console delivers CTRL_CLOSE_EVENT, which ssh.exe treats as hangup.
        if self._exited.is_set():
            return
        if self.pid is not None:
            _terminate_tree(self.pid)

    def kill(self) -> None:
        if not self._exited.is_set() and self.pid is not None:
            _terminate_tree(self.pid)
        if self.pid is not None:
            forget_owned_process(self.pid)

    def wait(self, timeout: float) -> Optional[SessionExitInfo]:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.poll_and_notify():
                return self._exit_info
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(_REAP_INTERVAL, remaining))

    def poll_and_notify(self) -> bool:
        if self._exit_info is None:
            try:
                alive = self._pty.isalive()
            except Exception:
                alive = False
            if alive:
                return False
            try:
                code = self._pty.get_exitstatus()
            except Exception:
                code = None
            self._exit_info = SessionExitInfo(
                exit_code=int(code) if code is not None else 0,
                reason="process_exit",
            )
            self._exited.set()
        # Let the reader deliver the console's last frame before EOF/exit.
        if not self._eof_event.wait(_EOF_DRAIN_TIMEOUT):
            self.notify_eof()
        self._notify_exit(self._exit_info)
        return True

    # -- terminal I/O ---------------------------------------------------------

    def write(self, data: bytes) -> bool:
        if not data:
            return True
        if len(data) > self._input_limit or self._exited.is_set():
            return False
        with self._write_lock:
            text = self._input_decoder.decode(bytes(data))
            if not text:
                return True
            try:
                _write_text(self._pty, text)
            except Exception:
                logger.debug("ConPTY write failed", exc_info=True)
                return False
        return True

    def resize(self, dimensions: TerminalDimensions) -> None:
        self.last_dimensions = dimensions
        try:
            self._pty.set_size(int(dimensions.columns), int(dimensions.rows))
        except Exception:
            logger.debug("ConPTY resize failed", exc_info=True)

    def notify_eof(self) -> None:
        with self._lock:
            if self._eof_notified:
                return
            self._eof_notified = True
        self._eof_event.set()
        self._on_eof()

    def close(self) -> None:
        # Dropping the PTY object closes the pseudo console and its pipes.
        self._pty = _ClosedPty()

    # -- internals ------------------------------------------------------------

    def _read_loop(self) -> None:
        pty = self._pty
        while True:
            try:
                text = pty.read(blocking=True)
            except Exception:
                break
            if text:
                if isinstance(text, str):
                    text = text.encode("utf-8", "replace")
                try:
                    self._on_output(bytes(text))
                except Exception:
                    logger.exception("ConPTY output callback failed")
                continue
            try:
                if pty.iseof() or not pty.isalive():
                    break
            except Exception:
                break
            time.sleep(0.01)
        self.notify_eof()

    def _notify_exit(self, info: SessionExitInfo) -> None:
        with self._lock:
            if self._exit_notified:
                return
            self._exit_notified = True
        if self.pid is not None:
            forget_owned_process(self.pid)
        self._unregister(self)
        self._on_exit(info)


def _write_text(pty, text: str) -> None:
    try:
        pty.write(text)
    except TypeError:  # pywinpty 2.x takes bytes
        pty.write(text.encode("utf-8"))


def _spawn(pty, executable: str, cmdline, cwd: str, env: str) -> bool:
    try:
        return pty.spawn(executable, cmdline=cmdline, cwd=cwd, env=env)
    except TypeError:  # pywinpty 2.x takes bytes
        encode = lambda value: None if value is None else value.encode("utf-8")
        return pty.spawn(
            encode(executable), cmdline=encode(cmdline), cwd=encode(cwd), env=encode(env)
        )


class _ClosedPty:
    def isalive(self) -> bool:
        return False

    def get_exitstatus(self):
        return None

    def iseof(self) -> bool:
        return True

    def read(self, blocking: bool = False) -> str:
        raise EOFError

    def write(self, text: str) -> int:
        raise OSError("pseudo console closed")

    def set_size(self, cols: int, rows: int) -> None:
        return None


class ConPtySessionProcessRunner:
    """Start daemon SSH sessions under ConPTY and reap them on one thread."""

    terminal_capable = True

    def __init__(
        self,
        launch_builder: LaunchBuilder,
        *,
        input_limit: int = DEFAULT_TERMINAL_INPUT_BYTES,
        pty_factory=None,
    ) -> None:
        self._launch_builder = launch_builder
        self._input_limit = input_limit
        if pty_factory is None:
            from winpty import PTY

            pty_factory = PTY
        self._pty_factory = pty_factory
        self._condition = threading.Condition()
        self._handles: set[ConPtyProcessHandle] = set()
        self._closed = False
        self._reaper = threading.Thread(
            target=self._reap,
            name="sshpilot-conpty-reaper",
            daemon=True,
        )
        self._reaper.start()

    def start(
        self,
        spec: SessionLaunchSpec,
        on_exit: SessionExitCallback,
        on_output: TerminalOutputCallback,
        on_eof: TerminalEofCallback,
    ) -> SessionProcessHandle:
        argv, environment = self._launch_builder(spec)
        argv = tuple(argv)
        env = dict(environment)
        if not argv or any(type(item) is not str or not item or "\0" in item for item in argv):
            raise ValueError("PTY command must be a safe non-empty argv")
        if any(
            type(key) is not str
            or type(value) is not str
            or "\0" in key
            or "\0" in value
            or "=" in key
            for key, value in env.items()
        ):
            raise ValueError("PTY environment is invalid")
        dimensions = spec.dimensions
        rows = int(dimensions.rows) if dimensions is not None else 24
        columns = int(dimensions.columns) if dimensions is not None else 80
        try:
            executable = _resolve_executable(argv[0], env)
            pty = self._pty_factory(max(1, columns), max(1, rows))
            cmdline = " " + subprocess.list2cmdline(argv[1:]) if len(argv) > 1 else None
            if not _spawn(
                pty,
                executable,
                cmdline,
                os.path.expanduser("~"),
                _environment_block(env),
            ):
                raise RuntimeError("ConPTY spawn returned false")
            if pty.pid:
                try:
                    record_owned_process(pty.pid, kind=KIND_SESSION)
                except OwnershipNotRecorded:
                    _terminate_tree(pty.pid)
                    raise
        except Exception:
            logger.debug("ConPTY launch failed", exc_info=True)
            raise SshPilotError(
                ErrorCode.PTY_ALLOCATION_FAILED,
                "The daemon could not allocate the terminal process",
                session_id=spec.session_id,
                connection_id=spec.connection_id,
            ) from None
        handle = ConPtyProcessHandle(
            pty,
            on_exit,
            on_output,
            on_eof,
            self._unregister,
            input_limit=self._input_limit,
        )
        handle.last_dimensions = dimensions
        with self._condition:
            if self._closed:
                handle.kill()
                raise RuntimeError("ConPTY runner is closed")
            self._handles.add(handle)
            self._condition.notify_all()
        handle.start_reading()
        return handle

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            handles = tuple(self._handles)
            self._condition.notify_all()
        for handle in handles:
            try:
                handle.kill()
            except Exception:
                continue
        for handle in handles:
            try:
                handle.wait(1.0)
            except Exception:
                pass
            handle.close()
        with self._condition:
            self._handles.clear()
            self._condition.notify_all()
        if self._reaper is not threading.current_thread():
            self._reaper.join(timeout=1.0)

    def _unregister(self, handle: ConPtyProcessHandle) -> None:
        with self._condition:
            self._handles.discard(handle)
            self._condition.notify_all()

    def _reap(self) -> None:
        while True:
            with self._condition:
                if self._closed and not self._handles:
                    return
                handles = tuple(self._handles)
                if not handles:
                    self._condition.wait()
                    continue
            for handle in handles:
                try:
                    alive = handle._pty.isalive()
                except Exception:
                    alive = False
                if not alive:
                    handle.poll_and_notify()
            with self._condition:
                if self._handles and not self._closed:
                    self._condition.wait(_REAP_INTERVAL)
