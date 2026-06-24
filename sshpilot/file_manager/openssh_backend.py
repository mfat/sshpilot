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

    def __init__(self, stdin, stdout) -> None:
        self._stdin = stdin
        self._stdout = stdout
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
        # Close the write side (unblocks a peer blocked on read).
        try:
            self._stdin.close()
        except Exception:  # pragma: no cover - best effort
            pass
        # Interrupt the reader thread blocked in readinto() by closing its
        # underlying fd directly. Calling ``self._stdout.close()`` instead would
        # deadlock: BufferedReader.close() needs the same buffer lock the blocked
        # readinto() holds.
        try:
            os.close(self._stdout.fileno())
        except Exception:  # pragma: no cover - best effort
            pass
        # Wake any in-flight requests so callers don't hang waiting on a reply
        # that will never come.
        for slot in list(self._pending.values()):
            slot.event.set()
        self._pending.clear()
        # The reader is a daemon thread; don't block teardown waiting for a read
        # that may not unblock until the transport (subprocess) is torn down.
        if self._reader is not None:
            self._reader.join(timeout=0.2)

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

    def mkdir(self, path: str) -> None:
        self._expect_ok(
            self._request(proto.FXP_MKDIR, proto.pack_string(path) + proto.encode_attrs(None))
        )

    def rmdir(self, path: str) -> None:
        self._expect_ok(self._request(proto.FXP_RMDIR, proto.pack_string(path)))

    def remove(self, path: str) -> None:
        self._expect_ok(self._request(proto.FXP_REMOVE, proto.pack_string(path)))

    def rename(self, old: str, new: str) -> None:
        self._expect_ok(
            self._request(
                proto.FXP_RENAME, proto.pack_string(old) + proto.pack_string(new)
            )
        )

    def open(self, path: str, pflags: int, attr: Optional[proto.SFTPAttributes] = None) -> bytes:
        payload = proto.pack_string(path) + proto.pack_uint32(pflags) + proto.encode_attrs(attr)
        return self._handle(self._request(proto.FXP_OPEN, payload))

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
        self._client: Optional[OpenSSHSFTPClient] = None
        self._proc: Optional[subprocess.Popen] = None
        self._sshpass_cleanup: Optional[Callable[[], None]] = None
        self._home: Optional[str] = None

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

    def _build_argv(self) -> Tuple[List[str], Dict[str, str], Optional[Callable[[], None]]]:
        from ..ssh_connection_builder import ConnectionContext, build_ssh_connection

        app_config = None
        try:
            from ..config import Config

            app_config = Config()
        except Exception:  # pragma: no cover - defensive
            app_config = None

        ctx = ConnectionContext(
            connection=self._connection,
            connection_manager=self._connection_manager,
            config=app_config,
            command_type="ssh",
            native_mode=True,
            extra_args=["-s"],            # request a subsystem...
            remote_command="sftp",        # ...named "sftp" (after the host)
        )
        prepared = build_ssh_connection(ctx)
        argv = list(prepared.command)
        env = {**os.environ, **(prepared.env or {})}
        cleanup: Optional[Callable[[], None]] = None
        if prepared.use_sshpass and prepared.password:
            from ..ssh_password_exec import wrap_argv_with_sshpass

            argv, cleanup = wrap_argv_with_sshpass(argv, prepared.password, env=env)
        return argv, env, cleanup

    def _connect_impl(self) -> None:
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
        client = OpenSSHSFTPClient(proc.stdin, proc.stdout)
        try:
            client.start()
        except Exception as exc:
            # Handshake never completed — the ssh process likely failed auth or
            # the host has no sftp subsystem. Classify from stderr.
            stderr = b""
            try:
                proc.wait(timeout=self._connect_timeout())
                stderr = proc.stderr.read() or b""
            except Exception:  # pragma: no cover - defensive
                proc.kill()
            if cleanup is not None:
                cleanup()
            raise self._classify_handshake_failure(stderr, exc)

        with self._lock:
            self._proc = proc
            self._client = client
            self._sshpass_cleanup = cleanup
        try:
            self._home = client.realpath(".")
        except Exception:  # pragma: no cover - home is best-effort
            self._home = None

    def _classify_handshake_failure(self, stderr: bytes, exc: Exception) -> Exception:
        text = (stderr or b"").decode("utf-8", "replace").strip()
        lowered = text.lower()
        if "permission denied" in lowered or "password" in lowered or "publickey" in lowered:
            return PermissionError(text or "Authentication failed")
        if text:
            from ..scp_utils import classify_sftp_error

            friendly = classify_sftp_error(text)
            return IOError(friendly or text)
        return exc

    def close(self) -> None:
        logger.info("OpenSSHSFTPManager.close() called")
        with self._lock:
            client = self._client
            proc = self._proc
            cleanup = self._sshpass_cleanup
            self._client = None
            self._proc = None
            self._sshpass_cleanup = None
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("Error closing SFTP client: %s", exc)
        if proc is not None:
            try:
                proc.terminate()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("Error terminating ssh subprocess: %s", exc)
        if cleanup is not None:
            try:
                cleanup()
            except Exception:  # pragma: no cover - best effort
                pass
        try:
            self._executor.shutdown(wait=False)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Error shutting down executor: %s", exc)

    # -- path helpers -----------------------------------------------------
    def _client_or_raise(self) -> OpenSSHSFTPClient:
        if self._client is None:
            raise IOError("SFTP connection is not available")
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
        def _impl() -> Tuple[str, List[FileEntry]]:
            with self._lock:
                client = self._client_or_raise()
                target = self._expand(path)
                entries: List[FileEntry] = []
                for attr in client.listdir_attr(target):
                    is_dir = _is_dir(attr)
                    item_count = None
                    if is_dir:
                        try:
                            child = target.rstrip("/") + "/" + attr.filename
                            item_count = len(client.listdir_attr(child))
                        except Exception:
                            item_count = None
                    entries.append(
                        FileEntry(
                            name=attr.filename,
                            is_dir=is_dir,
                            size=int(attr.st_size or 0),
                            modified=float(attr.st_mtime or 0),
                            item_count=item_count,
                        )
                    )
            return target, entries

        self._submit(
            _impl,
            on_success=lambda result: self.emit("directory-loaded", *result),
            on_error=lambda exc: self.emit("operation-error", str(exc)),
        )

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
                handle = client.open(
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
        handle = client.open(source, proto.FXF_READ)
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
        handle = client.open(
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
