"""Unix PTY process ownership and one shared non-blocking I/O loop."""

from __future__ import annotations

import errno
import fcntl
import os
import queue
import select
import selectors
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Mapping, Optional, Sequence

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.sessions import SessionExitInfo
from sshpilot.api.models.terminal import TerminalDimensions

from .session_runtime import (
    SessionExitCallback,
    SessionLaunchSpec,
    SessionProcessHandle,
)
from .process_registry import KIND_SESSION, forget_owned_process, record_owned_process

DEFAULT_TERMINAL_INPUT_BYTES = 256 * 1024
DEFAULT_PTY_READ_CHUNK = 32 * 1024

TerminalOutputCallback = Callable[[bytes], None]
TerminalEofCallback = Callable[[], None]
LaunchBuilder = Callable[
    [SessionLaunchSpec],
    tuple[Sequence[str], Mapping[str, str]],
]


@dataclass
class _PtyState:
    handle: "PtyProcessHandle"
    input_chunks: Deque[memoryview] = field(default_factory=deque)
    queued_input_bytes: int = 0
    registered: bool = False


class PtyIoManager:
    """Own all PTY master reads/writes on one daemon-scoped thread."""

    def __init__(
        self,
        *,
        input_limit: int = DEFAULT_TERMINAL_INPUT_BYTES,
        read_chunk: int = DEFAULT_PTY_READ_CHUNK,
    ) -> None:
        if input_limit < 1 or read_chunk < 1:
            raise ValueError("PTY I/O bounds must be positive")
        self._input_limit = input_limit
        self._read_chunk = read_chunk
        self._selector = selectors.DefaultSelector()
        self._commands: queue.Queue[tuple] = queue.Queue(maxsize=1024)
        self._state_lock = threading.Lock()
        self._wakeup_read, self._wakeup_write = os.pipe()
        os.set_blocking(self._wakeup_read, False)
        os.set_blocking(self._wakeup_write, False)
        self._selector.register(self._wakeup_read, selectors.EVENT_READ, None)
        self._states: dict[int, _PtyState] = {}
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="sshpilot-pty-io",
            daemon=True,
        )
        self._thread.start()

    def register(self, handle: "PtyProcessHandle") -> None:
        ready = threading.Event()
        self._submit(("register", handle, ready))
        if not ready.wait(1.0):
            raise RuntimeError("PTY I/O registration timed out")

    def write(self, handle: "PtyProcessHandle", data: bytes) -> bool:
        if not data:
            return True
        with self._state_lock:
            state = self._states.get(handle.master_fd)
            if (
                state is None
                or state.queued_input_bytes + len(data) > self._input_limit
            ):
                return False
            needs_interest = not state.input_chunks
            state.input_chunks.append(memoryview(data))
            state.queued_input_bytes += len(data)
            if not needs_interest:
                return True
            try:
                self._submit(("interest", handle))
            except RuntimeError:
                state.input_chunks.pop()
                state.queued_input_bytes -= len(data)
                return False
        return True

    def resize(self, handle: "PtyProcessHandle", dimensions: TerminalDimensions) -> None:
        handle.last_dimensions = dimensions
        self._submit(("resize", handle, dimensions))

    def unregister(self, handle: "PtyProcessHandle") -> None:
        try:
            self._submit(("unregister", handle))
        except RuntimeError:
            self._close_master(handle)

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

    def _submit(self, command: tuple, *, allow_closed: bool = False) -> None:
        if self._closed and not allow_closed:
            raise RuntimeError("PTY I/O manager is closed")
        try:
            self._commands.put_nowait(command)
        except queue.Full:
            raise RuntimeError("PTY I/O command queue is full") from None
        try:
            os.write(self._wakeup_write, b"x")
        except OSError:
            if not self._closed:
                raise

    def _run(self) -> None:
        while True:
            try:
                ready = self._selector.select(0.25)
            except OSError:
                return
            for key, mask in ready:
                if key.data is None:
                    try:
                        while os.read(self._wakeup_read, 1024):
                            pass
                    except (BlockingIOError, OSError):
                        pass
                    if self._drain_commands():
                        return
                    if self._closed:
                        for state in tuple(self._states.values()):
                            self._remove_state(state)
                        self._selector.close()
                        return
                    continue
                state: _PtyState = key.data
                if mask & selectors.EVENT_READ:
                    self._read_pty(state)
                if mask & selectors.EVENT_WRITE:
                    self._write_pty(state)

    def _drain_commands(self) -> bool:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return False
            kind = command[0]
            if kind == "close":
                for state in tuple(self._states.values()):
                    self._remove_state(state)
                self._selector.close()
                return True
            handle: PtyProcessHandle = command[1]
            if kind == "register":
                state = _PtyState(handle)
                with self._state_lock:
                    self._states[handle.master_fd] = state
                self._selector.register(
                    handle.master_fd,
                    selectors.EVENT_READ,
                    state,
                )
                state.registered = True
                command[2].set()
            elif kind == "unregister":
                state = self._states.get(handle.master_fd)
                if state is not None:
                    self._remove_state(state)
            elif kind == "resize":
                self._apply_resize(handle, command[2])
            elif kind == "interest":
                with self._state_lock:
                    state = self._states.get(handle.master_fd)
                if state is not None:
                    self._set_interest(state)

    def _read_pty(self, state: _PtyState) -> None:
        try:
            data = os.read(state.handle.master_fd, self._read_chunk)
        except BlockingIOError:
            return
        except OSError as error:
            if error.errno not in {errno.EIO, errno.EBADF}:
                state.handle.notify_io_error()
            data = b""
        if data:
            state.handle.notify_output(data)
            return
        self._remove_state(state)
        state.handle.notify_eof()

    def _write_pty(self, state: _PtyState) -> None:
        while True:
            with self._state_lock:
                if not state.input_chunks:
                    break
                chunk = state.input_chunks[0]
            try:
                written = os.write(state.handle.master_fd, chunk)
            except BlockingIOError:
                break
            except OSError:
                self._remove_state(state)
                state.handle.notify_eof()
                return
            with self._state_lock:
                state.queued_input_bytes -= written
                if written == len(chunk):
                    state.input_chunks.popleft()
                else:
                    state.input_chunks[0] = chunk[written:]
                    break
        self._set_interest(state)

    def _set_interest(self, state: _PtyState) -> None:
        if not state.registered:
            return
        events = selectors.EVENT_READ
        with self._state_lock:
            has_input = bool(state.input_chunks)
        if has_input:
            events |= selectors.EVENT_WRITE
        try:
            self._selector.modify(state.handle.master_fd, events, state)
        except (KeyError, OSError, ValueError):
            pass

    def _remove_state(self, state: _PtyState) -> None:
        with self._state_lock:
            self._states.pop(state.handle.master_fd, None)
        if state.registered:
            try:
                self._selector.unregister(state.handle.master_fd)
            except (KeyError, OSError, ValueError):
                pass
            state.registered = False
        self._close_master(state.handle)

    @staticmethod
    def _close_master(handle: "PtyProcessHandle") -> None:
        handle.close_master()

    @staticmethod
    def _apply_resize(
        handle: "PtyProcessHandle",
        dimensions: TerminalDimensions,
    ) -> None:
        packed = struct.pack("HHHH", dimensions.rows, dimensions.columns, 0, 0)
        try:
            fcntl.ioctl(handle.master_fd, termios.TIOCSWINSZ, packed)
        except OSError:
            return


class PtyProcessHandle(SessionProcessHandle):
    def __init__(
        self,
        process: subprocess.Popen,
        master_fd: int,
        manager: PtyIoManager,
        on_exit: SessionExitCallback,
        on_output: TerminalOutputCallback,
        on_eof: TerminalEofCallback,
        unregister: Callable[["PtyProcessHandle"], None],
    ) -> None:
        self._process = process
        self.master_fd = master_fd
        self._manager = manager
        self._on_exit = on_exit
        self._on_output = on_output
        self._on_eof = on_eof
        self._unregister = unregister
        self._lock = threading.Lock()
        self._exit_notified = False
        self._eof_notified = False
        self._master_closed = False
        self.last_dimensions: Optional[TerminalDimensions] = None

    def terminate(self) -> None:
        with self._lock:
            if self._process.poll() is None:
                os.killpg(self._process.pid, signal.SIGTERM)

    def kill(self) -> None:
        with self._lock:
            if self._process.poll() is None:
                os.killpg(self._process.pid, signal.SIGKILL)
        forget_owned_process(self._process.pid)

    def wait(self, timeout: float) -> Optional[SessionExitInfo]:
        try:
            code = self._process.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            return None
        info = self._exit_info(code)
        self._notify_exit(info)
        return info

    def poll_and_notify(self) -> bool:
        code = self._process.poll()
        if code is None:
            return False
        self._notify_exit(self._exit_info(code))
        return True

    def write(self, data: bytes) -> bool:
        return self._manager.write(self, data)

    def resize(self, dimensions: TerminalDimensions) -> None:
        self.last_dimensions = dimensions
        self._manager.resize(self, dimensions)

    def notify_output(self, data: bytes) -> None:
        self._on_output(data)

    def notify_eof(self) -> None:
        with self._lock:
            if self._eof_notified:
                return
            self._eof_notified = True
        self._on_eof()

    def notify_io_error(self) -> None:
        self.notify_eof()

    def close_master(self) -> None:
        with self._lock:
            if self._master_closed:
                return
            self._master_closed = True
            descriptor = self.master_fd
        try:
            os.close(descriptor)
        except OSError:
            pass

    def _notify_exit(self, info: SessionExitInfo) -> None:
        with self._lock:
            if self._exit_notified:
                return
            self._exit_notified = True
        self._unregister(self)
        self._on_exit(info)

    @staticmethod
    def _exit_info(code: int) -> SessionExitInfo:
        if code < 0:
            return SessionExitInfo(signal=-code, reason="signal")
        return SessionExitInfo(exit_code=code, reason="process_exit")


class PtySessionProcessRunner:
    """Allocate PTYs, own exact process groups, and reap them on one thread."""

    terminal_capable = True

    def __init__(
        self,
        launch_builder: LaunchBuilder,
        *,
        input_limit: int = DEFAULT_TERMINAL_INPUT_BYTES,
        read_chunk: int = DEFAULT_PTY_READ_CHUNK,
    ) -> None:
        self._launch_builder = launch_builder
        self._io = PtyIoManager(
            input_limit=input_limit,
            read_chunk=read_chunk,
        )
        self._condition = threading.Condition()
        self._handles: set[PtyProcessHandle] = set()
        self._closed = False
        self._reaper = threading.Thread(
            target=self._reap,
            name="sshpilot-pty-reaper",
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
            for key, value in env.items()
        ):
            raise ValueError("PTY environment is invalid")
        master_fd = slave_fd = status_read = status_write = -1
        try:
            master_fd, slave_fd = os.openpty()
            os.set_blocking(master_fd, False)
            dimensions = spec.dimensions
            if dimensions is not None:
                packed = struct.pack(
                    "HHHH",
                    dimensions.rows,
                    dimensions.columns,
                    0,
                    0,
                )
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, packed)
            status_read, status_write = os.pipe()
            if getattr(sys, "frozen", False):
                # sys.executable is this application's own binary in a frozen
                # (PyInstaller) build, not a Python interpreter — running it
                # against _pty_child.py's path would just relaunch the GUI
                # (detected as a second instance, exiting almost immediately
                # with status 0 and no PTY output whatsoever, and none of
                # _pty_child.py's setsid/login_tty/exec ever runs). Re-invoke
                # this same binary with an internal flag instead; see the
                # matching dispatch in run.py and the "--daemon" precedent in
                # daemon/launcher.py, which has the identical requirement.
                command = (
                    sys.executable,
                    "--internal-pty-child",
                    str(slave_fd),
                    str(status_write),
                    "--",
                    *argv,
                )
            else:
                helper = str(Path(__file__).with_name("_pty_child.py"))
                command = (
                    sys.executable,
                    helper,
                    str(slave_fd),
                    str(status_write),
                    "--",
                    *argv,
                )
            process = subprocess.Popen(
                command,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                close_fds=True,
                pass_fds=(slave_fd, status_write),
            )
            os.close(status_write)
            status_write = -1
            # The helper calls setsid before exec, so the child is a process
            # group leader and the whole group is ours to signal.
            record_owned_process(
                process.pid, kind=KIND_SESSION, process_group=True
            )
            readable, _, _ = select.select((status_read,), (), (), 2.0)
            # A successful exec closes status_write via CLOEXEC without
            # writing anything, so os.read(...) returns b"" (EOF) here too —
            # that is the *success* case, not failure. Only a timeout
            # (never became readable) or an explicit non-empty payload (the
            # child wrote b"launch_failed" before exiting) means the exec
            # itself failed.
            status = os.read(status_read, 64) if readable else None
            if status is None or status:
                process.kill()
                process.wait(timeout=1.0)
                raise RuntimeError("PTY child did not exec")
        except Exception:
            for descriptor in (master_fd, slave_fd, status_read, status_write):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            raise SshPilotError(
                ErrorCode.PTY_ALLOCATION_FAILED,
                "The daemon could not allocate the terminal process",
                session_id=spec.session_id,
                connection_id=spec.connection_id,
            ) from None
        finally:
            for descriptor in (slave_fd, status_read, status_write):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        handle = PtyProcessHandle(
            process,
            master_fd,
            self._io,
            on_exit,
            on_output,
            on_eof,
            self._unregister,
        )
        with self._condition:
            if self._closed:
                handle.kill()
                handle.wait(0.5)
                handle.close_master()
                raise RuntimeError("PTY runner is closed")
            self._handles.add(handle)
            self._condition.notify_all()
        try:
            self._io.register(handle)
        except Exception:
            with self._condition:
                self._handles.discard(handle)
            handle.kill()
            handle.wait(0.5)
            handle.close_master()
            raise
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
        deadline = time.monotonic() + 1.0
        pending = set(handles)
        while pending and time.monotonic() < deadline:
            for handle in tuple(pending):
                try:
                    if handle.wait(0) is not None:
                        pending.discard(handle)
                except Exception:
                    pending.discard(handle)
            if pending:
                time.sleep(0.01)
        self._io.close()
        for handle in handles:
            handle.close_master()
        with self._condition:
            self._handles.clear()
            self._condition.notify_all()
        if self._reaper is not threading.current_thread():
            self._reaper.join(timeout=1.0)

    def _unregister(self, handle: PtyProcessHandle) -> None:
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
                handle.poll_and_notify()
            with self._condition:
                if self._handles and not self._closed:
                    self._condition.wait(0.02)
