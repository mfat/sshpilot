"""Pure-Python SFTP v3 wire-protocol codec (no I/O, no paramiko).

This implements just the SFTP protocol layer (RFC draft-ietf-secsh-filexfer-02,
the version OpenSSH speaks as v3). It is transport-agnostic: callers feed/drain
bytes (typically the stdin/stdout pipes of an ``ssh host -s sftp`` subprocess).

Everything here is pure and unit-testable: packet build/parse and attribute
encode/decode. The live client (subprocess + threads) lives in
``openssh_backend.py``.
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Dict, List, Optional, Tuple

PROTOCOL_VERSION = 3

# -- packet types -----------------------------------------------------------
FXP_INIT = 1
FXP_VERSION = 2
FXP_OPEN = 3
FXP_CLOSE = 4
FXP_READ = 5
FXP_WRITE = 6
FXP_LSTAT = 7
FXP_FSTAT = 8
FXP_SETSTAT = 9
FXP_FSETSTAT = 10
FXP_OPENDIR = 11
FXP_READDIR = 12
FXP_REMOVE = 13
FXP_MKDIR = 14
FXP_RMDIR = 15
FXP_REALPATH = 16
FXP_STAT = 17
FXP_RENAME = 18
FXP_READLINK = 19
FXP_SYMLINK = 20
FXP_STATUS = 101
FXP_HANDLE = 102
FXP_DATA = 103
FXP_NAME = 104
FXP_ATTRS = 105
FXP_EXTENDED = 200
FXP_EXTENDED_REPLY = 201

# -- status codes -----------------------------------------------------------
FX_OK = 0
FX_EOF = 1
FX_NO_SUCH_FILE = 2
FX_PERMISSION_DENIED = 3
FX_FAILURE = 4
FX_BAD_MESSAGE = 5
FX_NO_CONNECTION = 6
FX_CONNECTION_LOST = 7
FX_OP_UNSUPPORTED = 8

_FX_NAMES = {
    FX_OK: "OK",
    FX_EOF: "EOF",
    FX_NO_SUCH_FILE: "No such file",
    FX_PERMISSION_DENIED: "Permission denied",
    FX_FAILURE: "Failure",
    FX_BAD_MESSAGE: "Bad message",
    FX_NO_CONNECTION: "No connection",
    FX_CONNECTION_LOST: "Connection lost",
    FX_OP_UNSUPPORTED: "Operation unsupported",
}

# -- open flags (pflags) ----------------------------------------------------
FXF_READ = 0x01
FXF_WRITE = 0x02
FXF_APPEND = 0x04
FXF_CREAT = 0x08
FXF_TRUNC = 0x10
FXF_EXCL = 0x20

# -- attribute flags --------------------------------------------------------
ATTR_SIZE = 0x01
ATTR_UIDGID = 0x02
ATTR_PERMISSIONS = 0x04
ATTR_ACMODTIME = 0x08
ATTR_EXTENDED = 0x80000000

S_IFMT = 0o170000
S_IFDIR = 0o040000
S_IFLNK = 0o120000


class SFTPError(IOError):
    """An SFTP STATUS response with a non-OK code.

    Subclasses ``IOError`` and sets ``errno`` so existing helpers that inspect
    ``exc.errno`` (e.g. for ENOENT) keep working.
    """

    def __init__(self, code: int, message: str = "") -> None:
        self.code = code
        text = message or _FX_NAMES.get(code, f"SFTP error {code}")
        super().__init__(text)
        import errno as _errno

        if code == FX_NO_SUCH_FILE:
            self.errno = _errno.ENOENT
        elif code == FX_PERMISSION_DENIED:
            self.errno = _errno.EACCES
        elif code in (FX_NO_CONNECTION, FX_CONNECTION_LOST):
            self.errno = _errno.EPIPE
        else:
            self.errno = _errno.EIO


@dataclasses.dataclass
class SFTPAttributes:
    """Decoded file attributes. Field names mirror paramiko's SFTPAttributes
    (``st_size``/``st_mode``/``st_mtime``/…) so existing helpers can be reused."""

    st_size: int = 0
    st_uid: Optional[int] = None
    st_gid: Optional[int] = None
    st_mode: int = 0
    st_atime: Optional[int] = None
    st_mtime: Optional[int] = None
    filename: Optional[str] = None  # set for FXP_NAME entries
    longname: Optional[str] = None

    def is_dir(self) -> bool:
        return (self.st_mode & S_IFMT) == S_IFDIR

    def is_symlink(self) -> bool:
        return (self.st_mode & S_IFMT) == S_IFLNK


# -- low level field helpers ------------------------------------------------


def pack_uint32(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def pack_uint64(value: int) -> bytes:
    return struct.pack(">Q", value & 0xFFFFFFFFFFFFFFFF)


def pack_string(value) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return pack_uint32(len(value)) + value


class _Reader:
    """Cursor over a bytes payload."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def uint32(self) -> int:
        v = struct.unpack_from(">I", self._data, self._pos)[0]
        self._pos += 4
        return v

    def uint64(self) -> int:
        v = struct.unpack_from(">Q", self._data, self._pos)[0]
        self._pos += 8
        return v

    def string(self) -> bytes:
        length = self.uint32()
        v = self._data[self._pos : self._pos + length]
        self._pos += length
        return v

    def text(self) -> str:
        return self.string().decode("utf-8", "replace")

    def remaining(self) -> int:
        return len(self._data) - self._pos


def encode_attrs(attr: Optional[SFTPAttributes]) -> bytes:
    """Encode attributes for an outgoing request. ``None`` → empty (flags=0)."""

    if attr is None:
        return pack_uint32(0)
    flags = 0
    body = b""
    if attr.st_size is not None and attr.st_size:
        flags |= ATTR_SIZE
        body += pack_uint64(attr.st_size)
    if attr.st_uid is not None and attr.st_gid is not None:
        flags |= ATTR_UIDGID
        body += pack_uint32(attr.st_uid) + pack_uint32(attr.st_gid)
    if attr.st_mode:
        flags |= ATTR_PERMISSIONS
        body += pack_uint32(attr.st_mode)
    if attr.st_atime is not None and attr.st_mtime is not None:
        flags |= ATTR_ACMODTIME
        body += pack_uint32(int(attr.st_atime)) + pack_uint32(int(attr.st_mtime))
    return pack_uint32(flags) + body


def decode_attrs(reader: _Reader) -> SFTPAttributes:
    """Decode an ATTRS structure from *reader* (positioned at the flags word)."""

    attr = SFTPAttributes()
    flags = reader.uint32()
    if flags & ATTR_SIZE:
        attr.st_size = reader.uint64()
    if flags & ATTR_UIDGID:
        attr.st_uid = reader.uint32()
        attr.st_gid = reader.uint32()
    if flags & ATTR_PERMISSIONS:
        attr.st_mode = reader.uint32()
    if flags & ATTR_ACMODTIME:
        attr.st_atime = reader.uint32()
        attr.st_mtime = reader.uint32()
    if flags & ATTR_EXTENDED:
        count = reader.uint32()
        for _ in range(count):
            reader.string()  # type
            reader.string()  # value
    return attr


# -- packet framing ---------------------------------------------------------


def build_packet(ptype: int, payload: bytes) -> bytes:
    """Frame a packet: uint32 length (type+payload), byte type, payload."""

    return pack_uint32(len(payload) + 1) + bytes([ptype]) + payload


def build_init() -> bytes:
    return build_packet(FXP_INIT, pack_uint32(PROTOCOL_VERSION))


def build_request(ptype: int, request_id: int, payload: bytes) -> bytes:
    return build_packet(ptype, pack_uint32(request_id) + payload)


def parse_version(payload: bytes) -> Tuple[int, Dict[str, bytes]]:
    """Parse an FXP_VERSION payload → (version, extensions)."""

    reader = _Reader(payload)
    version = reader.uint32()
    extensions: Dict[str, bytes] = {}
    while reader.remaining() >= 4:
        name = reader.text()
        value = reader.string()
        extensions[name] = value
    return version, extensions


def parse_status(payload: bytes) -> Tuple[int, int, str]:
    """Parse a STATUS payload (after the type byte) → (request_id, code, msg)."""

    reader = _Reader(payload)
    request_id = reader.uint32()
    code = reader.uint32()
    message = ""
    if reader.remaining() >= 4:
        message = reader.text()
    return request_id, code, message


def parse_name(payload: bytes) -> Tuple[int, List[SFTPAttributes]]:
    """Parse a NAME payload → (request_id, [SFTPAttributes with filename])."""

    reader = _Reader(payload)
    request_id = reader.uint32()
    count = reader.uint32()
    entries: List[SFTPAttributes] = []
    for _ in range(count):
        filename = reader.text()
        longname = reader.text()
        attr = decode_attrs(reader)
        attr.filename = filename
        attr.longname = longname
        entries.append(attr)
    return request_id, entries


def parse_handle(payload: bytes) -> Tuple[int, bytes]:
    reader = _Reader(payload)
    request_id = reader.uint32()
    return request_id, reader.string()


def parse_data(payload: bytes) -> Tuple[int, bytes]:
    reader = _Reader(payload)
    request_id = reader.uint32()
    return request_id, reader.string()


def parse_attrs(payload: bytes) -> Tuple[int, SFTPAttributes]:
    reader = _Reader(payload)
    request_id = reader.uint32()
    return request_id, decode_attrs(reader)


def response_request_id(ptype: int, payload: bytes) -> int:
    """Every post-INIT response begins with the request id."""

    return struct.unpack_from(">I", payload, 0)[0]
