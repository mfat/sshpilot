"""Pure OpenSSH SFTP backend for the file manager.

Drives a single persistent ``ssh -F <config> <host> -s sftp`` subprocess (built
via the app's native command/auth path) and speaks the SFTP v3 wire protocol
over its pipes. No paramiko: OpenSSH owns transport, ``~/.ssh/config``,
ProxyJump, host keys, and askpass/sshpass auth.

``OpenSSHSFTPManager`` matches the public contract of
``AsyncSFTPManager`` (same GObject signals, constructor, and methods) so the
file-manager window can use either backend interchangeably.
"""

from __future__ import annotations

import errno
import logging
import os
import pathlib
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from gi.repository import GObject

from . import sftp_protocol as proto
from .common import FileEntry, _MainThreadDispatcher
from .exceptions import TransferCancelledException

logger = logging.getLogger(__name__)

_CHUNK = 32768  # 32 KiB — within the SFTP max packet for reads/writes.


# ---------------------------------------------------------------------------
# Low-level SFTP-over-subprocess client
# ---------------------------------------------------------------------------


class _Pending:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional[Tuple[int, bytes]] = None


class OpenSSHSFTPClient:
    """Synchronous SFTP v3 client over a pair of byte streams (the subprocess
    stdin/stdout). A background thread reads responses and wakes the matching
    request by id, so requests can pipeline and never block the reader."""

    def __init__(self, stdin, stdout, on_close=None) -> None:
        self._stdin = stdin
        self._stdout = stdout
        # Optional transport teardown that makes the read side EOF so the reader
        # thread unblocks (e.g. terminate the ssh subprocess, or close the test
        # socketpair). Set by the owner of the transport.
        self._on_close = on_close
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 0
        self._pending: Dict[int, _Pending] = {}
        self._reader: Optional[threading.Thread] = None
        self._closed = False
        self.version: Optional[int] = None

    # -- framing ----------------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining > 0:
            buf = self._stdout.read(remaining)
            if not buf:
                raise EOFError("SFTP stream closed")
            chunks.append(buf)
            remaining -= len(buf)
        return b"".join(chunks)

    def _read_packet(self) -> Tuple[int, bytes]:
        length = int.from_bytes(self._read_exact(4), "big")
        if length == 0:
            raise EOFError("SFTP zero-length packet")
        body = self._read_exact(length)
        return body[0], body[1:]

    def _write_packet(self, data: bytes) -> None:
        with self._write_lock:
            self._stdin.write(data)
            self._stdin.flush()

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        """Perform the INIT/VERSION handshake, then start the reader thread."""
        self._write_packet(proto.build_init())
        ptype, payload = self._read_packet()
        if ptype != proto.FXP_VERSION:
            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected SFTP VERSION")
        self.version, _ = proto.parse_version(payload)
        self._reader = threading.Thread(
            target=self._reader_loop, name="sftp-reader", daemon=True
        )
        self._reader.start()

    def _reader_loop(self) -> None:
        try:
            while not self._closed:
                ptype, payload = self._read_packet()
                rid = proto.response_request_id(ptype, payload)
                slot = self._pending.pop(rid, None)
                if slot is not None:
                    slot.response = (ptype, payload)
                    slot.event.set()
        except Exception:  # EOF or stream error — fail everything pending.
            pass
        finally:
            self._closed = True
            for slot in list(self._pending.values()):
                slot.event.set()
            self._pending.clear()

    def close(self) -> None:
        self._closed = True
        # Tear down the transport first: this EOFs our read side so the reader
        # thread returns from its blocked readinto() instead of us yanking the
        # fd out from under the file objects (which caused EBADF on finalize).
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:  # pragma: no cover - best effort
                pass
        # Close the write side (also signals EOF to the peer).
        try:
            self._stdin.close()
        except Exception:  # pragma: no cover - best effort
            pass
        # Wake any in-flight requests so callers don't hang on a reply that will
        # never come.
        for slot in list(self._pending.values()):
            slot.event.set()
        self._pending.clear()
        # The reader (a daemon) exits once the read side EOFs. We do NOT close
        # ``self._stdout`` here — if the reader were still mid-read,
        # BufferedReader.close() would deadlock on the buffer lock; its owner
        # closes it after the reader has stopped.
        if self._reader is not None:
            self._reader.join(timeout=1.0)

    # -- request/response -------------------------------------------------
    def _request(self, ptype: int, payload: bytes) -> Tuple[int, bytes]:
        if self._closed:
            raise proto.SFTPError(proto.FX_CONNECTION_LOST, "SFTP session closed")
        with self._id_lock:
            self._next_id = (self._next_id + 1) & 0xFFFFFFFF
            rid = self._next_id
            slot = _Pending()
            self._pending[rid] = slot
        self._write_packet(proto.build_request(ptype, rid, payload))
        slot.event.wait()
        if slot.response is None:
            raise proto.SFTPError(proto.FX_CONNECTION_LOST, "SFTP session lost")
        return slot.response

    @staticmethod
    def _expect_ok(resp: Tuple[int, bytes]) -> None:
        ptype, payload = resp
        if ptype != proto.FXP_STATUS:
            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected STATUS")
        _, code, message = proto.parse_status(payload)
        if code != proto.FX_OK:
            raise proto.SFTPError(code, message)

    # -- high level operations -------------------------------------------
    def realpath(self, path: str) -> str:
        resp = self._request(proto.FXP_REALPATH, proto.pack_string(path))
        ptype, payload = resp
        if ptype == proto.FXP_NAME:
            _, entries = proto.parse_name(payload)
            if entries:
                return entries[0].filename or path
        self._expect_ok(resp)  # raises if STATUS error
        return path

    def stat(self, path: str) -> proto.SFTPAttributes:
        return self._attrs(self._request(proto.FXP_STAT, proto.pack_string(path)))

    def lstat(self, path: str) -> proto.SFTPAttributes:
        return self._attrs(self._request(proto.FXP_LSTAT, proto.pack_string(path)))

    @staticmethod
    def _attrs(resp: Tuple[int, bytes]) -> proto.SFTPAttributes:
        ptype, payload = resp
        if ptype == proto.FXP_ATTRS:
            _, attr = proto.parse_attrs(payload)
            return attr
        if ptype == proto.FXP_STATUS:
            _, code, message = proto.parse_status(payload)
            raise proto.SFTPError(code, message)
        raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected ATTRS")

    def listdir_attr(self, path: str) -> List[proto.SFTPAttributes]:
        resp = self._request(proto.FXP_OPENDIR, proto.pack_string(path))
        handle = self._handle(resp)
        entries: List[proto.SFTPAttributes] = []
        try:
            while True:
                rd = self._request(proto.FXP_READDIR, proto.pack_string(handle))
                ptype, payload = rd
                if ptype == proto.FXP_NAME:
                    _, names = proto.parse_name(payload)
                    for attr in names:
                        if attr.filename in (".", ".."):
                            continue
                        entries.append(attr)
                elif ptype == proto.FXP_STATUS:
                    _, code, message = proto.parse_status(payload)
                    if code == proto.FX_EOF:
                        break
                    raise proto.SFTPError(code, message)
                else:
                    raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected NAME")
        finally:
            self.close_handle(handle)
        return entries

    @staticmethod
    def _handle(resp: Tuple[int, bytes]) -> bytes:
        ptype, payload = resp
        if ptype == proto.FXP_HANDLE:
            _, handle = proto.parse_handle(payload)
            return handle
        if ptype == proto.FXP_STATUS:
            _, code, message = proto.parse_status(payload)
            raise proto.SFTPError(code, message)
        raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected HANDLE")

    def mkdir(self, path: str, mode: Optional[int] = None) -> None:
        attr = None
        if mode is not None:
            attr = proto.SFTPAttributes(st_mode=int(mode) & 0o7777)
        self._expect_ok(
            self._request(proto.FXP_MKDIR, proto.pack_string(path) + proto.encode_attrs(attr))
        )

    def rmdir(self, path: str) -> None:
        self._expect_ok(self._request(proto.FXP_RMDIR, proto.pack_string(path)))

    def remove(self, path: str) -> None:
        self._expect_ok(self._request(proto.FXP_REMOVE, proto.pack_string(path)))

    unlink = remove  # paramiko alias

    def rename(self, old: str, new: str) -> None:
        self._expect_ok(
            self._request(
                proto.FXP_RENAME, proto.pack_string(old) + proto.pack_string(new)
            )
        )

    def posix_rename(self, old: str, new: str) -> None:
        """Atomic rename that overwrites the target (OpenSSH extension).

        Regular SFTP RENAME fails if the destination exists; ``posix-rename``
        replaces it, which is what callers (e.g. authorized_keys install) need.
        """
        payload = (
            proto.pack_string("posix-rename@openssh.com")
            + proto.pack_string(old)
            + proto.pack_string(new)
        )
        self._expect_ok(self._request(proto.FXP_EXTENDED, payload))

    def chmod(self, path: str, mode: int) -> None:
        attr = proto.SFTPAttributes(st_mode=int(mode) & 0o7777)
        self._expect_ok(
            self._request(proto.FXP_SETSTAT, proto.pack_string(path) + proto.encode_attrs(attr))
        )

    def normalize(self, path: str) -> str:
        """paramiko alias for realpath."""
        return self.realpath(path)

    def open_handle(
        self, path: str, pflags: int, attr: Optional[proto.SFTPAttributes] = None
    ) -> bytes:
        """Low-level OPEN → returns an SFTP handle (bytes)."""
        payload = proto.pack_string(path) + proto.pack_uint32(pflags) + proto.encode_attrs(attr)
        return self._handle(self._request(proto.FXP_OPEN, payload))

    def open(self, path: str, mode: str = "r", bufsize: int = -1) -> "OpenSSHSFTPFile":
        """Paramiko-compatible ``open`` returning a seek-tracking file object, so
        code written against paramiko's ``SFTPClient.open(path, mode)`` (e.g. the
        window's remote copy/paste) works against this client unchanged."""
        m = mode.replace("b", "")
        if m in ("w", "x"):
            pflags = proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_TRUNC
        elif m == "a":
            pflags = proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_APPEND
        elif m in ("r+", "w+"):
            pflags = proto.FXF_READ | proto.FXF_WRITE | proto.FXF_CREAT
        else:  # "r"
            pflags = proto.FXF_READ
        handle = self.open_handle(path, pflags)
        return OpenSSHSFTPFile(self, handle)

    # paramiko's SFTPClient exposes both ``open`` and ``file`` (an alias).
    def file(self, path: str, mode: str = "r", bufsize: int = -1) -> "OpenSSHSFTPFile":
        return self.open(path, mode, bufsize)

    def read(self, handle: bytes, offset: int, length: int) -> bytes:
        payload = proto.pack_string(handle) + proto.pack_uint64(offset) + proto.pack_uint32(length)
        resp = self._request(proto.FXP_READ, payload)
        ptype, body = resp
        if ptype == proto.FXP_DATA:
            _, data = proto.parse_data(body)
            return data
        if ptype == proto.FXP_STATUS:
            _, code, message = proto.parse_status(body)
            if code == proto.FX_EOF:
                return b""
            raise proto.SFTPError(code, message)
        raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected DATA")

    def write(self, handle: bytes, offset: int, data: bytes) -> None:
        payload = proto.pack_string(handle) + proto.pack_uint64(offset) + proto.pack_string(data)
        self._expect_ok(self._request(proto.FXP_WRITE, payload))

    def close_handle(self, handle: bytes) -> None:
        try:
            self._expect_ok(self._request(proto.FXP_CLOSE, proto.pack_string(handle)))
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("SFTP close handle failed: %s", exc)


class OpenSSHSFTPFile:
    """A minimal paramiko-``SFTPFile``-compatible wrapper over a handle.

    Tracks its own offset so ``read()``/``write()`` behave like a stream, which
    is what the window's remote copy/paste expects.
    """

    def __init__(self, client: "OpenSSHSFTPClient", handle: bytes) -> None:
        self._client = client
        self._handle = handle
        self._offset = 0
        self._closed = False

    def read(self, size: Optional[int] = None) -> bytes:
        if size is not None:
            data = self._client.read(self._handle, self._offset, size)
            self._offset += len(data)
            return data
        # Read to EOF.
        chunks = []
        while True:
            chunk = self._client.read(self._handle, self._offset, _CHUNK)
            if not chunk:
                break
            self._offset += len(chunk)
            chunks.append(chunk)
        return b"".join(chunks)

    def write(self, data: bytes) -> None:
        self._client.write(self._handle, self._offset, data)
        self._offset += len(data)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._client.close_handle(self._handle)

    def __enter__(self) -> "OpenSSHSFTPFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _is_dir(attr: proto.SFTPAttributes) -> bool:
    return attr.is_dir()


# ---------------------------------------------------------------------------
# Manager (mirrors AsyncSFTPManager's public contract)
# ---------------------------------------------------------------------------


class OpenSSHSFTPManager(GObject.GObject):
    """File-manager backend that talks SFTP over an OpenSSH subprocess."""

    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, tuple()),
        "connection-error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "authentication-required": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float, str)),
        "progress-bytes": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
        "operation-error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "directory-loaded": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        # Folder item-counts filled in after the listing: (path, {name: count}).
        "directory-counts": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }

    def __init__(
        self,
        host: str,
        username: str,
        port: int = 22,
        password: Optional[str] = None,
        *,
        dispatcher: Callable[[Callable, tuple, dict], None] | None = None,
        connection: Any = None,
        connection_manager: Any = None,
        ssh_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._username = username
        self._password = password
        self._port = port or 22
        self._connection = connection
        self._connection_manager = connection_manager
        self._ssh_config = dict(ssh_config) if ssh_config else None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._dispatcher = dispatcher or (
            lambda cb, args=(), kwargs=None: _MainThreadDispatcher.dispatch(
                cb, *args, **(kwargs or {})
            )
        )
        self._lock = threading.Lock()
        self._cancel_lock = threading.Lock()
        self._cancelled_operations: set = set()
        self._operation_seq = 0
        self._listdir_seq = 0  # generation guard for background count passes
        self._client: Optional[OpenSSHSFTPClient] = None
        self._proc: Optional[subprocess.Popen] = None
        self._sshpass_cleanup: Optional[Callable[[], None]] = None
        self._home: Optional[str] = None
        self._stderr_lines: "deque[str]" = deque(maxlen=200)
        self._stderr_thread: Optional[threading.Thread] = None
        # SFTP-level keepalive (mirrors the paramiko backend).
        self._keepalive_interval = 0
        self._keepalive_count_max = 0
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop: Optional[threading.Event] = None
        self._keepalive_failures = 0

    # -- shared plumbing (mirrors AsyncSFTPManager) -----------------------
    def _submit(
        self,
        func: Callable[[], object],
        *,
        on_success: Optional[Callable[[object], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> Future:
        future = self._executor.submit(func)

        def _done(fut: Future) -> None:
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - uniform error path
                if on_error:
                    self._dispatcher(on_error, (exc,), {})
                else:
                    self._dispatcher(self.emit, ("operation-error", str(exc)), {})
            else:
                if on_success:
                    self._dispatcher(on_success, (result,), {})

        future.add_done_callback(_done)
        return future

    def _mark_cancelled(self, operation_id: str) -> None:
        with self._cancel_lock:
            self._cancelled_operations.add(operation_id)

    def _discard_cancellation_flag(self, operation_id: str) -> None:
        with self._cancel_lock:
            self._cancelled_operations.discard(operation_id)

    def _is_cancelled(self, operation_id: str) -> bool:
        with self._cancel_lock:
            return operation_id in self._cancelled_operations

    def _next_operation_id(self, kind: str) -> str:
        with self._cancel_lock:
            self._operation_seq += 1
            seq = self._operation_seq
        return f"{kind}_{id(self)}_{time.time_ns()}_{seq}"

    @staticmethod
    def _format_size(num_bytes: float) -> str:
        size = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{size:.1f} TB"

    def _connect_timeout(self) -> int:
        cfg = self._ssh_config or {}
        fm = cfg.get("file_manager") if isinstance(cfg, dict) else None
        if isinstance(fm, dict):
            try:
                value = int(fm.get("sftp_connect_timeout") or 0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        return 30

    # -- connection -------------------------------------------------------
    def connect_to_server(self, password: Optional[str] = None) -> None:
        if password is not None:
            self._password = password
        self._submit(
            self._connect_impl,
            on_success=lambda *_: self.emit("connected"),
            on_error=lambda exc: self._emit_connect_error(exc),
        )

    def _emit_connect_error(self, exc: Exception) -> None:
        message = str(exc)
        lowered = message.lower()
        if "permission denied" in lowered or "authentication" in lowered or "password" in lowered:
            self.emit("authentication-required", message)
        else:
            self.emit("connection-error", message)

    def _build_argv(
        self,
        *,
        remote_command: str = "sftp",
        extra_args: Tuple[str, ...] = ("-s",),
    ) -> Tuple[List[str], Dict[str, str], Optional[Callable[[], None]]]:
        from ..ssh_connection_builder import ConnectionContext, build_ssh_connection

        app_config = None
        try:
            from ..config import Config

            app_config = Config()
        except Exception:  # pragma: no cover - defensive
            app_config = None

        # Dialog / connect_to_server(password=…) stores the secret on the manager;
        # resolve_native_auth reads connection.password (same as scp_utils).
        if self._password and self._connection is not None:
            try:
                self._connection.password = self._password
            except Exception:  # pragma: no cover - defensive
                pass

        ctx = ConnectionContext(
            connection=self._connection,
            connection_manager=self._connection_manager,
            config=app_config,
            command_type="ssh",
            native_mode=True,
            extra_args=list(extra_args),  # e.g. ["-s"] to request a subsystem...
            remote_command=remote_command,  # ...named "sftp" (after the host)
        )
        prepared = build_ssh_connection(ctx)
        argv = list(prepared.command)
        env = {**os.environ, **(prepared.env or {})}
        cleanup: Optional[Callable[[], None]] = None
        if prepared.use_sshpass and prepared.password:
            from ..ssh_password_exec import wrap_argv_with_sshpass

            argv, cleanup = wrap_argv_with_sshpass(argv, prepared.password, env=env)
        return argv, env, cleanup

    @property
    def host(self) -> str:
        """Hostname this session is connected to (keyring identity)."""
        return self._host

    @property
    def username(self) -> str:
        """Login user for this session (keyring identity)."""
        return self._username

    def run_command(
        self, command: str, *, input: Optional[bytes] = None, timeout: float = 30
    ) -> Tuple[int, bytes, str]:
        """Run a one-shot command on this host over the same SSH/auth path as the
        SFTP session and capture its output. ``input`` (bytes) is fed to stdin —
        e.g. a sudo password for ``sudo -S`` plus file content for ``tee``. Binary
        safe (no text decoding of stdout). Returns
        ``(exit_code, stdout_bytes, stderr_text)``; ``exit_code == -1`` means it
        could not be launched. **Blocking** — call via :meth:`run_command_async`."""
        argv, env, cleanup = self._build_argv(
            remote_command=command, extra_args=())
        try:
            proc = subprocess.run(
                argv, env=env, input=input, capture_output=True, timeout=timeout)
            stderr = (proc.stderr or b"").decode("utf-8", "replace")
            return proc.returncode, (proc.stdout or b""), stderr
        except subprocess.TimeoutExpired:
            return -1, b"", "Command timed out"
        except Exception as exc:  # noqa: BLE001 — surface as a failed result
            return -1, b"", str(exc)
        finally:
            if cleanup is not None:
                cleanup()

    def run_command_async(
        self, command: str, *, input: Optional[bytes] = None, timeout: float = 30
    ) -> Future:
        """Run :meth:`run_command` on the backend executor; returns a ``Future``
        resolving to ``(exit_code, stdout_bytes, stderr_text)``."""
        return self._executor.submit(
            self.run_command, command, input=input, timeout=timeout)

    def _connect_impl(self) -> None:
        self._read_keepalive_config()
        argv, env, cleanup = self._build_argv()
        logger.debug("OpenSSH SFTP backend launching: %s", " ".join(argv))
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
        )
        # Drain stderr continuously so verbose ssh output can't fill the pipe
        # buffer and block the ssh process mid-session.
        self._start_stderr_drain(proc)
        # On client.close() terminate the ssh subprocess so its stdout EOFs and
        # the reader thread unblocks cleanly (no fd surgery).
        client = OpenSSHSFTPClient(
            proc.stdin, proc.stdout, on_close=lambda: self._terminate_proc(proc)
        )
        try:
            client.start()
        except Exception as exc:
            # Handshake never completed — the ssh process likely failed auth or
            # the host has no sftp subsystem. Classify from the drained stderr.
            try:
                proc.wait(timeout=self._connect_timeout())
            except Exception:  # pragma: no cover - defensive
                proc.kill()
            if cleanup is not None:
                cleanup()
            raise self._classify_handshake_failure(self._drained_stderr(), exc) from exc

        with self._lock:
            self._proc = proc
            self._client = client
            self._sshpass_cleanup = cleanup
        try:
            self._home = client.realpath(".")
        except Exception:  # pragma: no cover - home is best-effort
            self._home = None
        self._start_keepalive_worker()

    def _start_stderr_drain(self, proc: subprocess.Popen) -> None:
        self._stderr_lines.clear()

        def _drain() -> None:
            try:
                for raw in iter(proc.stderr.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line:
                        self._stderr_lines.append(line)
                        logger.debug("ssh stderr: %s", line)
            except Exception:  # pragma: no cover - best effort
                pass

        self._stderr_thread = threading.Thread(
            target=_drain, name="sftp-stderr", daemon=True
        )
        self._stderr_thread.start()

    def _drained_stderr(self) -> str:
        # Give the drain thread a moment to flush the final lines.
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.5)
        return "\n".join(self._stderr_lines).strip()

    def _classify_handshake_failure(self, text: str, exc: Exception) -> Exception:
        text = (text or "").strip()
        lowered = text.lower()
        if "permission denied" in lowered or "password" in lowered or "publickey" in lowered:
            return PermissionError(text or "Authentication failed")
        if text:
            from ..scp_utils import classify_sftp_error

            friendly = classify_sftp_error(text)
            return OSError(friendly or text)
        return exc

    def _read_keepalive_config(self) -> None:
        cfg = self._ssh_config or {}
        fm = cfg.get("file_manager") if isinstance(cfg, dict) else None
        if not isinstance(fm, dict):
            return

        def _coerce(value):
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0

        self._keepalive_interval = _coerce(fm.get("sftp_keepalive_interval"))
        self._keepalive_count_max = _coerce(fm.get("sftp_keepalive_count_max"))

    def _start_keepalive_worker(self) -> None:
        if self._keepalive_interval <= 0:
            return
        stop = threading.Event()
        self._keepalive_stop = stop
        self._keepalive_failures = 0
        interval = self._keepalive_interval

        def _worker() -> None:
            while not stop.wait(interval):
                err = None
                with self._lock:
                    client = self._client
                    if client is None:
                        break
                    try:
                        client.realpath(".")  # cheap liveness probe
                    except Exception as exc:
                        err = exc
                if err is None:
                    self._keepalive_failures = 0
                    continue
                self._keepalive_failures += 1
                count_max = self._keepalive_count_max
                logger.debug(
                    "OpenSSH keepalive failed (%s/%s): %s",
                    self._keepalive_failures, count_max, err,
                )
                if count_max >= 0 and self._keepalive_failures > count_max:
                    self._dispatcher(
                        self.emit,
                        ("operation-error",
                         "SFTP keepalive failed too many times; connection may be down"),
                        {},
                    )
                    break

        self._keepalive_thread = threading.Thread(
            target=_worker, name="sftp-keepalive", daemon=True
        )
        self._keepalive_thread.start()

    def _stop_keepalive_worker(self) -> None:
        if self._keepalive_stop is not None:
            self._keepalive_stop.set()
        self._keepalive_thread = None
        self._keepalive_stop = None

    @staticmethod
    def _terminate_proc(proc: subprocess.Popen) -> None:
        """Stop the ssh subprocess (EOFs its pipes so the reader/stderr threads
        unblock). Idempotent and quiet."""
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:  # pragma: no cover - best effort
            pass

    def close(self) -> None:
        logger.info("OpenSSHSFTPManager.close() called")
        self._stop_keepalive_worker()
        with self._lock:
            client = self._client
            proc = self._proc
            cleanup = self._sshpass_cleanup
            self._client = None
            self._proc = None
            self._sshpass_cleanup = None
        # Terminate the subprocess first so the reader/stderr threads EOF and
        # stop, then close the client (joins the reader) — no fd surgery.
        self._terminate_proc(proc)
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("Error closing SFTP client: %s", exc)
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:  # pragma: no cover - best effort
                    pass
            # Close the proc's own pipe objects once the readers have stopped,
            # so their finalizers don't later double-close the fds.
            for attr in ("stdout", "stderr", "stdin"):
                stream = getattr(proc, attr, None)
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # pragma: no cover - best effort
                    pass
        if cleanup is not None:
            try:
                cleanup()
            except Exception:  # pragma: no cover - best effort
                pass
        try:
            self._executor.shutdown(wait=False)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Error shutting down executor: %s", exc)

    @property
    def _sftp(self) -> Optional[OpenSSHSFTPClient]:
        """Compatibility alias for the live SFTP client.

        Some window/dialog code probes ``manager._sftp`` (a paramiko detail) for
        liveness and for ``stat()``. The OpenSSH client exposes the same
        ``.stat()`` (returning attrs with ``st_mode``/``st_size``/``st_mtime``),
        so aliasing it keeps the backend a drop-in replacement.
        """
        return self._client

    def is_connected(self) -> bool:
        if self._client is None:
            return False
        proc = self._proc
        # The subprocess must still be alive; a dead ssh means a stale session.
        return proc is not None and proc.poll() is None

    # -- path helpers -----------------------------------------------------
    def _client_or_raise(self) -> OpenSSHSFTPClient:
        if self._client is None:
            raise OSError("SFTP connection is not available")
        return self._client

    def _expand(self, path: str) -> str:
        if path == "~":
            return self._home or self._client_or_raise().realpath(".")
        if path.startswith("~/"):
            home = self._home or self._client_or_raise().realpath(".")
            return home.rstrip("/") + "/" + path[2:]
        return path

    def _remote_exists(self, client: OpenSSHSFTPClient, path: str) -> bool:
        try:
            client.stat(path)
        except proto.SFTPError as exc:
            if getattr(exc, "errno", None) == errno.ENOENT:
                return False
            raise
        return True

    # -- directory listing ------------------------------------------------
    def listdir(self, path: str) -> None:
        # Bump the generation so an in-flight background count pass for a
        # previous directory abandons itself (see _start_count_pass).
        with self._cancel_lock:
            self._listdir_seq += 1
            generation = self._listdir_seq

        def _impl() -> Tuple[str, List[FileEntry]]:
            with self._lock:
                client = self._client_or_raise()
                target = self._expand(path)
                entries: List[FileEntry] = []
                # Fast path: no per-folder "N items" count here — that needs a
                # separate remote read per subfolder and is what made listing
                # slow over high-latency links. Counts are filled in afterwards
                # by a cooperative background pass.
                for attr in client.listdir_attr(target):
                    is_dir = _is_dir(attr)
                    entries.append(
                        FileEntry(
                            name=attr.filename,
                            is_dir=is_dir,
                            size=int(attr.st_size or 0),
                            modified=float(attr.st_mtime or 0),
                            item_count=None,
                        )
                    )
            return target, entries

        def _loaded(result):
            target, entries = result
            self.emit("directory-loaded", target, entries)
            self._start_count_pass(target, entries, generation)

        self._submit(
            _impl,
            on_success=_loaded,
            on_error=lambda exc: self.emit("operation-error", str(exc)),
        )

    def _start_count_pass(
        self, path: str, entries: List[FileEntry], generation: int
    ) -> None:
        """Fill folder item-counts in the background, one folder per executor
        task so user operations interleave, and abandon if the user navigates."""
        folders = [e.name for e in entries if e.is_dir]
        if not folders:
            return

        def _count_chunk(index: int) -> None:
            with self._cancel_lock:
                if self._listdir_seq != generation:
                    return  # superseded by a newer listing
            name = folders[index]
            try:
                with self._lock:
                    client = self._client_or_raise()
                    child = path.rstrip("/") + "/" + name
                    count = len(client.listdir_attr(child))
                self._dispatcher(
                    self.emit, ("directory-counts", path, {name: count}), {}
                )
            except Exception as exc:  # leave this folder's count as None
                logger.debug("Count for %s failed: %s", name, exc)
            if index + 1 < len(folders):
                self._submit(lambda: _count_chunk(index + 1))

        self._submit(lambda: _count_chunk(0))

    def directory_size(self, path: str) -> Future:
        """Future resolving to the recursive byte size of a remote directory."""

        def _sum(client: OpenSSHSFTPClient, target: str) -> int:
            total = 0
            for attr in client.listdir_attr(target):
                child = target.rstrip("/") + "/" + attr.filename
                if _is_dir(attr):
                    try:
                        total += _sum(client, child)
                    except Exception:  # unreadable subdir → skip
                        pass
                else:
                    total += int(attr.st_size or 0)
            return total

        def _impl() -> int:
            with self._lock:
                client = self._client_or_raise()
                return _sum(client, self._expand(path))

        return self._submit(_impl)

    # -- simple operations ------------------------------------------------
    def mkdir(self, path: str) -> Future:
        def _impl() -> None:
            with self._lock:
                self._client_or_raise().mkdir(path)

        return self._submit(_impl)

    def touch(self, path: str) -> Future:
        def _impl() -> None:
            with self._lock:
                client = self._client_or_raise()
                if self._remote_exists(client, path):
                    raise FileExistsError(path)
                handle = client.open_handle(
                    path, proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_EXCL
                )
                client.close_handle(handle)

        return self._submit(_impl)

    def path_exists(self, path: str) -> Future:
        def _impl() -> bool:
            with self._lock:
                return self._remote_exists(self._client_or_raise(), path)

        return self._submit(_impl)

    def rename(self, source: str, target: str) -> Future:
        def _impl() -> None:
            with self._lock:
                self._client_or_raise().rename(source, target)

        return self._submit(_impl)

    def remove(self, path: str) -> Future:
        def _remove_recursive(client: OpenSSHSFTPClient, target: str) -> None:
            try:
                attr = client.lstat(target)
            except proto.SFTPError as exc:
                if getattr(exc, "errno", None) == errno.ENOENT:
                    return
                raise
            if _is_dir(attr):
                for child in client.listdir_attr(target):
                    _remove_recursive(client, target.rstrip("/") + "/" + child.filename)
                client.rmdir(target)
            else:
                client.remove(target)

        def _impl() -> None:
            with self._lock:
                _remove_recursive(self._client_or_raise(), path)

        return self._submit(_impl)

    # -- transfers --------------------------------------------------------
    def _download_file(
        self,
        client: OpenSSHSFTPClient,
        source: str,
        destination: pathlib.Path,
        operation_id: str,
        transferred_base: int,
        grand_total: int,
    ) -> int:
        try:
            total = int(client.stat(source).st_size or 0)
        except Exception:
            total = 0
        handle = client.open_handle(source, proto.FXF_READ)
        offset = 0
        try:
            with open(destination, "wb") as fh:
                while True:
                    if self._is_cancelled(operation_id):
                        raise TransferCancelledException("Download was cancelled")
                    data = client.read(handle, offset, _CHUNK)
                    if not data:
                        break
                    fh.write(data)
                    offset += len(data)
                    done = transferred_base + offset
                    self.emit("progress-bytes", done, grand_total or total)
                    ref = grand_total or total
                    if ref > 0:
                        self.emit(
                            "progress",
                            done / ref,
                            f"Downloaded {self._format_size(done)} of {self._format_size(ref)}",
                        )
                    else:
                        self.emit("progress", 0.0, f"Downloaded {self._format_size(done)}")
        finally:
            client.close_handle(handle)
        return offset

    def download(self, source: str, destination: pathlib.Path) -> Future:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Download: prepare parent failed: %s", exc)
        operation_id = self._next_operation_id("download")

        def _impl() -> None:
            self.emit("progress", 0.0, "Starting download…")
            try:
                with self._lock:
                    client = self._client_or_raise()
                    self._download_file(client, source, destination, operation_id, 0, 0)
                if not self._is_cancelled(operation_id):
                    self.emit("progress", 1.0, "Download complete")
            except TransferCancelledException:
                self._cleanup_local(destination)
                self.emit("progress", 0.0, "Download cancelled")
                raise
            except Exception as exc:
                self.emit(
                    "operation-error",
                    f"Download failed: {os.path.basename(source)}: {exc}",
                )
                raise
            finally:
                self._discard_cancellation_flag(operation_id)

        return self._cancellable(self._submit(_impl), operation_id)

    def _upload_file(
        self,
        client: OpenSSHSFTPClient,
        source: pathlib.Path,
        destination: str,
        operation_id: str,
        transferred_base: int,
        grand_total: int,
    ) -> int:
        total = source.stat().st_size
        handle = client.open_handle(
            destination, proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_TRUNC
        )
        offset = 0
        try:
            with open(source, "rb") as fh:
                while True:
                    if self._is_cancelled(operation_id):
                        raise TransferCancelledException("Upload was cancelled")
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        break
                    client.write(handle, offset, chunk)
                    offset += len(chunk)
                    done = transferred_base + offset
                    ref = grand_total or total
                    self.emit("progress-bytes", done, ref)
                    if ref > 0:
                        self.emit(
                            "progress",
                            done / ref,
                            f"Uploaded {self._format_size(done)} of {self._format_size(ref)}",
                        )
                    else:
                        self.emit("progress", 0.0, f"Uploaded {self._format_size(done)}")
        finally:
            client.close_handle(handle)
        return offset

    def upload(self, source: pathlib.Path, destination: str) -> Future:
        operation_id = self._next_operation_id("upload")

        def _impl() -> None:
            self.emit("progress", 0.0, "Starting upload…")
            try:
                with self._lock:
                    client = self._client_or_raise()
                    self._upload_file(client, source, destination, operation_id, 0, 0)
                if not self._is_cancelled(operation_id):
                    self.emit("progress", 1.0, "Upload complete")
            except TransferCancelledException:
                self._cleanup_remote(destination)
                self.emit("progress", 0.0, "Upload cancelled")
                raise
            except Exception as exc:
                self.emit(
                    "operation-error",
                    f"Upload failed: {os.path.basename(str(source))}: {exc}",
                )
                raise
            finally:
                self._discard_cancellation_flag(operation_id)

        return self._cancellable(self._submit(_impl), operation_id)

    def download_directory(self, source: str, destination: pathlib.Path) -> Future:
        operation_id = self._next_operation_id("download_dir")

        def _impl() -> None:
            self.emit("progress", 0.0, "Scanning…")
            try:
                with self._lock:
                    client = self._client_or_raise()
                    files: List[Tuple[str, pathlib.Path, int]] = []
                    grand_total = 0

                    def _walk(remote: str, local: pathlib.Path) -> None:
                        local.mkdir(parents=True, exist_ok=True)
                        for attr in client.listdir_attr(remote):
                            rpath = remote.rstrip("/") + "/" + attr.filename
                            lpath = local / attr.filename
                            if _is_dir(attr):
                                _walk(rpath, lpath)
                            else:
                                size = int(attr.st_size or 0)
                                files.append((rpath, lpath, size))

                    _walk(source, destination)
                    grand_total = sum(size for _, _, size in files)
                    done = 0
                    for rpath, lpath, _size in files:
                        if self._is_cancelled(operation_id):
                            raise TransferCancelledException("Download was cancelled")
                        moved = self._download_file(
                            client, rpath, lpath, operation_id, done, grand_total
                        )
                        done += moved
                if not self._is_cancelled(operation_id):
                    self.emit("progress", 1.0, "Download complete")
            except TransferCancelledException:
                self.emit("progress", 0.0, "Download cancelled")
                raise
            except Exception as exc:
                self.emit("operation-error", f"Download failed: {exc}")
                raise
            finally:
                self._discard_cancellation_flag(operation_id)

        return self._cancellable(self._submit(_impl), operation_id)

    def upload_directory(self, source: pathlib.Path, destination: str) -> Future:
        operation_id = self._next_operation_id("upload_dir")

        def _impl() -> None:
            self.emit("progress", 0.0, "Scanning…")
            try:
                files: List[Tuple[pathlib.Path, str, int]] = []
                for root, _dirs, names in os.walk(source):
                    rel = os.path.relpath(root, source)
                    for name in names:
                        local = pathlib.Path(root) / name
                        remote = destination.rstrip("/") + "/" + (
                            name if rel == "." else f"{rel}/{name}"
                        ).replace(os.sep, "/")
                        files.append((local, remote, local.stat().st_size))
                grand_total = sum(size for _, _, size in files)
                with self._lock:
                    client = self._client_or_raise()
                    # Pre-create directories.
                    self._mkdirs(client, destination)
                    made = set()
                    for local, remote, _size in files:
                        parent = remote.rsplit("/", 1)[0]
                        if parent and parent not in made:
                            self._mkdirs(client, parent)
                            made.add(parent)
                    done = 0
                    for local, remote, _size in files:
                        if self._is_cancelled(operation_id):
                            raise TransferCancelledException("Upload was cancelled")
                        moved = self._upload_file(
                            client, local, remote, operation_id, done, grand_total
                        )
                        done += moved
                if not self._is_cancelled(operation_id):
                    self.emit("progress", 1.0, "Upload complete")
            except TransferCancelledException:
                self.emit("progress", 0.0, "Upload cancelled")
                raise
            except Exception as exc:
                self.emit("operation-error", f"Upload failed: {exc}")
                raise
            finally:
                self._discard_cancellation_flag(operation_id)

        return self._cancellable(self._submit(_impl), operation_id)

    @staticmethod
    def _mkdirs(client: OpenSSHSFTPClient, path: str) -> None:
        parts = [p for p in path.split("/") if p]
        cur = "/" if path.startswith("/") else ""
        for part in parts:
            cur = (cur.rstrip("/") + "/" + part) if cur else part
            try:
                client.mkdir(cur)
            except proto.SFTPError:
                pass  # already exists / permission — let the write surface it

    def _cancellable(self, future: Future, operation_id: str) -> Future:
        original_cancel = future.cancel

        def cancel_with_cleanup():
            self._mark_cancelled(operation_id)
            return original_cancel()

        future.cancel = cancel_with_cleanup
        return future

    def _cleanup_local(self, destination: pathlib.Path) -> None:
        try:
            if destination.exists():
                destination.unlink()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Partial download cleanup failed: %s", exc)

    def _cleanup_remote(self, destination: str) -> None:
        try:
            if self._client is not None:
                self._client.remove(destination)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Partial upload cleanup failed: %s", exc)
