"""Capability-gated binary terminal frames for Protocol v1."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.common import AttachmentId, SessionId, require_identifier

from .framing import FramingError, MAX_FRAME_SIZE
from .wire_ids import id_to_wire_bytes, wire_bytes_to_id

TERMINAL_STREAM_VERSION = 2
MAX_TERMINAL_PAYLOAD_SIZE = 64 * 1024
_MAGIC = b"SPTB"
_HEADER = struct.Struct(">4sBBH32sQ32s")
_ZERO_BYTES = b"\0" * 32


class TerminalFrameKind(IntEnum):
    OUTPUT = 1
    INPUT = 2
    CONTINUITY_LOST = 3
    INPUT_ERROR = 4


class TerminalFrameFlags(IntFlag):
    NONE = 0
    REPLAY = 1
    EOF = 2
    TRUNCATED = 4


_FLAGS_BY_KIND = {
    TerminalFrameKind.OUTPUT: (
        TerminalFrameFlags.REPLAY
        | TerminalFrameFlags.EOF
        | TerminalFrameFlags.TRUNCATED
    ),
    TerminalFrameKind.INPUT: TerminalFrameFlags.NONE,
    TerminalFrameKind.CONTINUITY_LOST: TerminalFrameFlags.TRUNCATED,
    TerminalFrameKind.INPUT_ERROR: TerminalFrameFlags.NONE,
}


@dataclass(frozen=True)
class TerminalFrame:
    kind: TerminalFrameKind
    session_id: SessionId
    sequence: int
    data: bytes = b""
    attachment_id: AttachmentId | None = None
    flags: TerminalFrameFlags = TerminalFrameFlags.NONE

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("terminal sequence must be a non-negative integer")
        if not isinstance(self.data, bytes):
            raise TypeError("terminal frame data must be bytes")
        if len(self.data) > MAX_TERMINAL_PAYLOAD_SIZE:
            raise ValueError("terminal frame payload exceeds the maximum size")
        if self.kind in {
            TerminalFrameKind.INPUT,
            TerminalFrameKind.INPUT_ERROR,
        }:
            _validate_attachment_id(self.attachment_id)
        elif self.attachment_id is not None:
            raise ValueError("only terminal input/status frames carry an attachment")
        if not isinstance(self.kind, TerminalFrameKind):
            raise TypeError("terminal frame kind is invalid")
        if not isinstance(self.flags, TerminalFrameFlags):
            raise TypeError("terminal frame flags are invalid")
        if self.flags & ~_FLAGS_BY_KIND[self.kind]:
            raise ValueError("terminal flags do not match the frame kind")


def is_terminal_payload(payload: bytes) -> bool:
    return len(payload) >= len(_MAGIC) and payload[: len(_MAGIC)] == _MAGIC


def encode_terminal_payload(frame: TerminalFrame) -> bytes:
    if type(frame) is not TerminalFrame:
        raise TypeError("a TerminalFrame is required")
    session_bytes = id_to_wire_bytes(frame.session_id)
    attachment_bytes = (
        id_to_wire_bytes(frame.attachment_id)
        if frame.attachment_id is not None
        else _ZERO_BYTES
    )
    payload = _HEADER.pack(
        _MAGIC,
        TERMINAL_STREAM_VERSION,
        int(frame.kind),
        int(frame.flags),
        session_bytes,
        frame.sequence,
        attachment_bytes,
    ) + frame.data
    if len(payload) > MAX_FRAME_SIZE:
        raise FramingError(
            ErrorCode.FRAME_TOO_LARGE,
            "The terminal frame exceeds the maximum size",
        )
    return payload


def decode_terminal_payload(payload: bytes) -> TerminalFrame:
    if not isinstance(payload, bytes) or len(payload) < _HEADER.size:
        raise FramingError(ErrorCode.INVALID_FRAME, "The terminal frame is incomplete")
    try:
        (
            magic,
            version,
            kind_value,
            flags_value,
            session_bytes,
            sequence,
            attachment_bytes,
        ) = _HEADER.unpack(payload[: _HEADER.size])
        if magic != _MAGIC or version != TERMINAL_STREAM_VERSION:
            raise ValueError
        kind = TerminalFrameKind(kind_value)
        flags = TerminalFrameFlags(flags_value)
        known_flags = int(
            TerminalFrameFlags.REPLAY
            | TerminalFrameFlags.EOF
            | TerminalFrameFlags.TRUNCATED
        )
        if int(flags) & ~known_flags:
            raise ValueError
        session_id = SessionId(wire_bytes_to_id(session_bytes))
        attachment_id = None
        if attachment_bytes != _ZERO_BYTES:
            attachment_id = AttachmentId(wire_bytes_to_id(attachment_bytes))
        return TerminalFrame(
            kind=kind,
            session_id=session_id,
            sequence=sequence,
            data=payload[_HEADER.size :],
            attachment_id=attachment_id,
            flags=flags,
        )
    except (TypeError, ValueError):
        raise FramingError(
            ErrorCode.INVALID_FRAME,
            "The terminal frame is malformed",
        ) from None


def _validate_attachment_id(attachment_id: AttachmentId | None) -> None:
    if not isinstance(attachment_id, str) or not attachment_id.strip():
        raise ValueError("terminal input requires an attachment identifier")
