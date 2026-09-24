"""GTK-free synchronous SFTP v3 client over a pair of byte streams.

The daemon SFTP runtime drives an ``ssh … -s sftp`` subprocess without pulling
in GObject/GTK.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Deque, Dict, Iterator, List, Optional, Tuple

from . import protocol as proto

logger = logging.getLogger(__name__)

_CHUNK = 32768  # 32 KiB — within the SFTP max packet for reads/writes.
# Upper bound on a read/write chunk, whatever limits@openssh.com advertises.
_MAX_CHUNK = 4 * 1024 * 1024
# Reads/writes a file keeps in flight, so a transfer costs about one round
# trip per window instead of one per chunk.
_PIPELINE_DEPTH = 16
# READDIRs a directory listing keeps in flight.
_READDIR_AHEAD = 8


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
        self.extensions: Dict[str, bytes] = {}
        # Largest READ/WRITE payload; raised from limits@openssh.com in start().
        self.max_read_length = _CHUNK
        self.max_write_length = _CHUNK

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
        self.version, self.extensions = proto.parse_version(payload)
        self._reader = threading.Thread(
            target=self._reader_loop, name="sftp-reader", daemon=True
        )
        self._reader.start()
        self._load_limits()

    def _load_limits(self) -> None:
        """Adopt the server's READ/WRITE size limits (OpenSSH extension).

        OpenSSH advertises ~255 KiB, so a 1 MiB file moves in 4-5 chunks
        instead of 32. Servers without the extension keep the 32 KiB default.
        """
        if "limits@openssh.com" not in self.extensions:
            return
        try:
            ptype, payload = self._request(
                proto.FXP_EXTENDED, proto.pack_string("limits@openssh.com")
            )
            if ptype != proto.FXP_EXTENDED_REPLY:
                return
            reader = proto._Reader(payload)
            reader.uint32()  # request id
            max_packet = reader.uint64()
            max_read = reader.uint64()
            max_write = reader.uint64()
        except Exception as exc:
            logger.debug("SFTP limits request failed: %s", exc)
            return
        # 0 means "no limit"; a WRITE also carries its handle and header, so
        # leave room for them inside the packet limit.
        if max_packet:
            max_write = min(max_write or max_packet, max_packet - 1024)
        if max_read > 0:
            self.max_read_length = min(max_read, _MAX_CHUNK)
        if max_write > 0:
            self.max_write_length = min(max_write, _MAX_CHUNK)

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
        return self._wait(self._send(ptype, payload))

    def _send(self, ptype: int, payload: bytes) -> _Pending:
        """Send a request without waiting; pass the slot to ``_wait``."""
        if self._closed:
            raise proto.SFTPError(proto.FX_CONNECTION_LOST, "SFTP session closed")
        with self._id_lock:
            self._next_id = (self._next_id + 1) & 0xFFFFFFFF
            rid = self._next_id
            slot = _Pending()
            self._pending[rid] = slot
        # The reader marks the session closed before it wakes the pending
        # slots, so a slot registered after that sweep is woken here.
        if self._closed:
            slot.event.set()
        self._write_packet(proto.build_request(ptype, rid, payload))
        return slot

    @staticmethod
    def _wait(slot: _Pending) -> Tuple[int, bytes]:
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
        """List a directory, keeping ``_READDIR_AHEAD`` READDIRs in flight.

        Each READDIR returns the next batch (OpenSSH: ~100 names) or EOF, and
        a server handles requests on one handle in order, so a large directory
        costs a round trip per ``_READDIR_AHEAD`` batches instead of per batch.
        """
        resp = self._request(proto.FXP_OPENDIR, proto.pack_string(path))
        handle = self._handle(resp)
        entries: List[proto.SFTPAttributes] = []
        request = proto.pack_string(handle)
        inflight: Deque[_Pending] = deque()
        try:
            while True:
                while len(inflight) < _READDIR_AHEAD:
                    inflight.append(self._send(proto.FXP_READDIR, request))
                ptype, payload = self._wait(inflight.popleft())
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
            # The read-aheads past EOF answer EOF; settle them before CLOSE.
            while inflight:
                self._wait(inflight.popleft())
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

    def supports_hardlink(self) -> bool:
        return "hardlink@openssh.com" in self.extensions

    def hardlink(self, old: str, new: str) -> None:
        """Create ``new`` as a hard link to ``old`` (OpenSSH extension).

        One round trip, and the link keeps the old inode's content even after
        ``old`` is later replaced by a rename.
        """
        payload = (
            proto.pack_string("hardlink@openssh.com")
            + proto.pack_string(old)
            + proto.pack_string(new)
        )
        self._expect_ok(self._request(proto.FXP_EXTENDED, payload))

    def supports_copy_data(self) -> bool:
        return "copy-data" in self.extensions

    def copy_data(self, source_handle: bytes, destination_handle: bytes) -> None:
        """Copy a whole open file into another on the server (``copy-data``).

        One round trip, and no file data crosses the connection.
        """
        payload = (
            proto.pack_string("copy-data")
            + proto.pack_string(source_handle)
            + proto.pack_uint64(0)  # read offset
            + proto.pack_uint64(0)  # length: 0 copies to EOF
            + proto.pack_string(destination_handle)
            + proto.pack_uint64(0)  # write offset
        )
        self._expect_ok(self._request(proto.FXP_EXTENDED, payload))

    def supports_statvfs(self) -> bool:
        return "statvfs@openssh.com" in self.extensions

    def statvfs(self, path: str) -> proto.SFTPStatVFS:
        """Filesystem sizes for *path* (OpenSSH extension)."""
        payload = proto.pack_string("statvfs@openssh.com") + proto.pack_string(path)
        ptype, body = self._request(proto.FXP_EXTENDED, payload)
        if ptype == proto.FXP_STATUS:
            _, code, message = proto.parse_status(body)
            raise proto.SFTPError(code, message)
        if ptype != proto.FXP_EXTENDED_REPLY:
            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected EXTENDED_REPLY")
        return proto.parse_statvfs(body)

    def fsetstat(self, handle: bytes, attr: proto.SFTPAttributes) -> None:
        """Set attributes (mode, times, …) on an open handle."""
        self._expect_ok(
            self._request(proto.FXP_FSETSTAT, proto.pack_string(handle) + proto.encode_attrs(attr))
        )

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

    def open(
        self,
        path: str,
        mode: str = "r",
        bufsize: int = -1,
        *,
        create_mode: Optional[int] = None,
    ) -> "OpenSSHSFTPFile":
        """Paramiko-compatible ``open`` returning a seek-tracking file object, so
        code written against paramiko's ``SFTPClient.open(path, mode)`` (e.g. the
        window's remote copy/paste) works against this client unchanged.

        ``create_mode`` sets the permissions a newly created file gets in the
        same OPEN request (the server still applies its umask), saving the
        separate SETSTAT round trip."""
        m = mode.replace("b", "")
        if m in ("w", "x"):
            pflags = proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_TRUNC
        elif m == "a":
            pflags = proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_APPEND
        elif m in ("r+", "w+"):
            pflags = proto.FXF_READ | proto.FXF_WRITE | proto.FXF_CREAT
        else:  # "r"
            pflags = proto.FXF_READ
        attr = None
        if create_mode is not None:
            attr = proto.SFTPAttributes(st_mode=int(create_mode) & 0o7777)
        handle = self.open_handle(path, pflags, attr)
        return OpenSSHSFTPFile(self, handle)

    # paramiko's SFTPClient exposes both ``open`` and ``file`` (an alias).
    def file(
        self,
        path: str,
        mode: str = "r",
        bufsize: int = -1,
        *,
        create_mode: Optional[int] = None,
    ) -> "OpenSSHSFTPFile":
        return self.open(path, mode, bufsize, create_mode=create_mode)

    def read(self, handle: bytes, offset: int, length: int) -> bytes:
        return self._read_data(self._wait(self._send_read(handle, offset, length)))

    def _send_read(self, handle: bytes, offset: int, length: int) -> _Pending:
        payload = proto.pack_string(handle) + proto.pack_uint64(offset) + proto.pack_uint32(length)
        return self._send(proto.FXP_READ, payload)

    @staticmethod
    def _read_data(resp: Tuple[int, bytes]) -> bytes:
        """DATA reply → bytes; EOF → ``b""``."""
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
        self._expect_ok(self._wait(self._send_write(handle, offset, data)))

    def _send_write(self, handle: bytes, offset: int, data: bytes) -> _Pending:
        payload = proto.pack_string(handle) + proto.pack_uint64(offset) + proto.pack_string(data)
        return self._send(proto.FXP_WRITE, payload)

    def iter_read(
        self, handle: bytes, offset: int = 0, length: Optional[int] = None
    ) -> Iterator[bytes]:
        """Yield a file's bytes in order from ``offset``, up to ``length`` or EOF.

        Up to ``_PIPELINE_DEPTH`` reads are in flight at once. A server may
        return fewer bytes than asked (OpenSSH's sftp-server caps a READ at
        ~255 KiB), so a short reply is followed up for the rest of its chunk;
        only an empty reply means EOF.
        """
        step = self.max_read_length
        end = None if length is None else offset + length
        inflight: Deque[Tuple[int, int, _Pending]] = deque()
        next_offset = position = offset
        while True:
            while len(inflight) < _PIPELINE_DEPTH and (end is None or next_offset < end):
                size = step if end is None else min(step, end - next_offset)
                inflight.append((next_offset, size, self._send_read(handle, next_offset, size)))
                next_offset += size
            if not inflight:
                return
            chunk_offset, size, slot = inflight.popleft()
            data = self._read_data(self._wait(slot))
            chunk_end = chunk_offset + size
            while data:
                yield data
                position += len(data)
                if position >= chunk_end:
                    break
                data = self.read(handle, position, chunk_end - position)
            if position < chunk_end:
                # EOF inside this chunk; replies still in flight are past it.
                return

    def pipelined_writer(self, handle: bytes, offset: int = 0) -> "PipelinedWriter":
        return PipelinedWriter(self, handle, offset)

    def close_handle(self, handle: bytes) -> None:
        try:
            self._expect_ok(self._request(proto.FXP_CLOSE, proto.pack_string(handle)))
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("SFTP close handle failed: %s", exc)


class PipelinedWriter:
    """Writes to an open handle with up to ``_PIPELINE_DEPTH`` WRITEs in flight.

    Data is split into chunks the server accepts: OpenSSH's sftp-server drops
    the session on a message over 256 KiB. A failed WRITE raises from a later
    ``write()`` or from ``flush()``, which must be called before the handle is
    closed.
    """

    def __init__(self, client: "OpenSSHSFTPClient", handle: bytes, offset: int = 0) -> None:
        self._client = client
        self._handle = handle
        self.offset = offset
        self._inflight: Deque[_Pending] = deque()

    def write(self, data: bytes) -> None:
        client = self._client
        step = client.max_write_length
        view = memoryview(data)
        for start in range(0, len(view), step):
            chunk = bytes(view[start:start + step])
            self._inflight.append(client._send_write(self._handle, self.offset, chunk))
            self.offset += len(chunk)
            if len(self._inflight) >= _PIPELINE_DEPTH:
                client._expect_ok(client._wait(self._inflight.popleft()))

    def flush(self) -> None:
        """Wait until every WRITE is acknowledged."""
        while self._inflight:
            self._client._expect_ok(self._client._wait(self._inflight.popleft()))


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

    @property
    def handle(self) -> bytes:
        return self._handle

    def read(self, size: Optional[int] = None) -> bytes:
        """Read ``size`` bytes, or to EOF when ``size`` is None."""
        chunks = []
        for chunk in self._client.iter_read(self._handle, self._offset, size):
            chunks.append(chunk)
            self._offset += len(chunk)
        return b"".join(chunks)

    def write(self, data: bytes) -> None:
        writer = self._client.pipelined_writer(self._handle, self._offset)
        writer.write(data)
        writer.flush()
        self._offset = writer.offset

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
