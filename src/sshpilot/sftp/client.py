"""GTK-free synchronous SFTP v3 client over a pair of byte streams.

The daemon SFTP runtime drives an ``ssh … -s sftp`` subprocess without pulling
in GObject/GTK.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Tuple

from . import protocol as proto

logger = logging.getLogger(__name__)

_CHUNK = 32768  # 32 KiB — within the SFTP max packet for reads/writes.


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

    def symlink(self, target_path: str, link_path: str) -> None:
        """Create ``link_path`` as a symlink pointing at ``target_path``.

        The SFTP v3 draft specifies ``SSH_FXP_SYMLINK`` as ``linkpath`` then
        ``targetpath``, but OpenSSH's sftp-server famously implemented the
        arguments swapped and kept that behaviour for compatibility (see
        OpenSSH's ``PROTOCOL`` §4.1). Since this client only ever talks to an
        OpenSSH subsystem, send ``targetpath`` first to match the server.
        """
        self._expect_ok(
            self._request(
                proto.FXP_SYMLINK,
                proto.pack_string(target_path) + proto.pack_string(link_path),
            )
        )

    def readlink(self, path: str) -> str:
        resp = self._request(proto.FXP_READLINK, proto.pack_string(path))
        ptype, payload = resp
        if ptype == proto.FXP_NAME:
            _, entries = proto.parse_name(payload)
            if entries:
                return entries[0].filename or ""
            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "empty READLINK reply")
        self._expect_ok(resp)  # raises if STATUS error
        raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected NAME")

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
