"""Capability-gated binary terminal frames for Protocol v1."""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from enum import IntEnum, IntFlag

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.common import AttachmentId, SessionId
from sshpilot.api.session_identity import session_id_from_uuid, session_uuid_from_id

from .framing import FramingError, MAX_FRAME_SIZE

TERMINAL_STREAM_VERSION = 1
MAX_TERMINAL_PAYLOAD_SIZE = 64 * 1024
_MAGIC = b"SPTB"
_HEADER = struct.Struct(">4sBBH16sQ16s")
_ZERO_UUID = b"\0" * 16


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


@dataclass(frozen=True)
class TerminalFrame:
    kind: TerminalFrameKind
    session_id: SessionId
    sequence: int
    data: bytes = b""
    attachment_id: AttachmentId | None = None
    flags: TerminalFrameFlags = TerminalFrameFlags.NONE

    def __post_init__(self) -> None:
        session_uuid_from_id(self.session_id)
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
            _attachment_uuid(self.attachment_id)
        elif self.attachment_id is not None:
            raise ValueError("only terminal input/status frames carry an attachment")
        if self.kind is not TerminalFrameKind.OUTPUT and (
            self.flags & (TerminalFrameFlags.REPLAY | TerminalFrameFlags.EOF)
        ):
            raise ValueError("terminal flags do not match the frame kind")


def is_terminal_payload(payload: bytes) -> bool:
    return len(payload) >= len(_MAGIC) and payload[: len(_MAGIC)] == _MAGIC


def encode_terminal_payload(frame: TerminalFrame) -> bytes:
    if type(frame) is not TerminalFrame:
        raise TypeError("a TerminalFrame is required")
    session_uuid = uuid.UUID(session_uuid_from_id(frame.session_id)).bytes
    attachment_uuid = (
        _attachment_uuid(frame.attachment_id).bytes
        if frame.attachment_id is not None
        else _ZERO_UUID
    )
    payload = _HEADER.pack(
        _MAGIC,
        TERMINAL_STREAM_VERSION,
        int(frame.kind),
        int(frame.flags),
        session_uuid,
        frame.sequence,
        attachment_uuid,
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
        session_id = SessionId(session_id_from_uuid(str(uuid.UUID(bytes=session_bytes))))
        attachment_id = None
        if attachment_bytes != _ZERO_UUID:
            attachment_id = AttachmentId(
                f"attachment:{uuid.UUID(bytes=attachment_bytes)!s}"
            )
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


def _attachment_uuid(attachment_id: AttachmentId | None) -> uuid.UUID:
    if not isinstance(attachment_id, str):
        raise ValueError("terminal input requires an attachment identifier")
    prefix = "attachment:"
    if not attachment_id.startswith(prefix) or len(attachment_id) > 64:
        raise ValueError("attachment identifier is malformed")
    parsed = uuid.UUID(attachment_id[len(prefix) :])
    if parsed.int == 0:
        raise ValueError("attachment identifier must not be nil")
    return parsed
