"""One-use client-to-daemon secret response frames."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.common import InteractionId, require_identifier

from .framing import FramingError, MAX_FRAME_SIZE
from .wire_ids import id_to_wire_bytes, wire_bytes_to_id

SECRET_STREAM_VERSION = 2
MAX_SECRET_PAYLOAD_SIZE = 16 * 1024
_MAGIC = b"SPSB"
_HEADER = struct.Struct(">4sBBH32s16s")


class SecretFrameKind(IntEnum):
    RESPONSE = 1
    REVEAL_RESPONSE = 2
    COMMAND_INPUT = 3


@dataclass
class SecretFrame:
    kind: SecretFrameKind
    interaction_id: InteractionId
    nonce: bytes = field(repr=False)
    secret: bytearray = field(repr=False)

    def __post_init__(self) -> None:
        if self.kind not in (
            SecretFrameKind.RESPONSE,
            SecretFrameKind.REVEAL_RESPONSE,
            SecretFrameKind.COMMAND_INPUT,
        ):
            raise ValueError("secret frame kind is unsupported")
        require_identifier(self.interaction_id, "interaction id")
        if type(self.nonce) is not bytes or len(self.nonce) != 16:
            raise ValueError("secret response nonce is malformed")
        if type(self.secret) is not bytearray:
            raise TypeError("secret response payload must be a bytearray")
        if len(self.secret) > MAX_SECRET_PAYLOAD_SIZE:
            raise ValueError("secret response payload size is invalid")
        if b"\0" in self.secret:
            raise ValueError("secret response payload must not contain NUL")

    def clear(self) -> None:
        self.secret[:] = b"\0" * len(self.secret)
        self.secret.clear()


def is_secret_payload(payload: bytes) -> bool:
    return len(payload) >= len(_MAGIC) and payload[: len(_MAGIC)] == _MAGIC


def encode_secret_payload(frame: SecretFrame) -> bytes:
    if type(frame) is not SecretFrame:
        raise TypeError("a SecretFrame is required")
    interaction_bytes = id_to_wire_bytes(frame.interaction_id)
    payload = _HEADER.pack(
        _MAGIC,
        SECRET_STREAM_VERSION,
        int(frame.kind),
        0,
        interaction_bytes,
        frame.nonce,
    ) + bytes(frame.secret)
    if len(payload) > MAX_FRAME_SIZE:
        raise FramingError(
            ErrorCode.FRAME_TOO_LARGE,
            "The secret response frame exceeds the maximum size",
        )
    return payload


def decode_secret_payload(payload: bytes) -> SecretFrame:
    if not isinstance(payload, bytes) or len(payload) < _HEADER.size:
        raise FramingError(
            ErrorCode.INVALID_FRAME,
            "The secret response frame is incomplete",
        )
    try:
        magic, version, kind_value, flags, interaction_bytes, nonce = (
            _HEADER.unpack(payload[: _HEADER.size])
        )
        if magic != _MAGIC or version != SECRET_STREAM_VERSION or flags != 0:
            raise ValueError
        secret = bytearray(payload[_HEADER.size :])
        return SecretFrame(
            kind=SecretFrameKind(kind_value),
            interaction_id=InteractionId(wire_bytes_to_id(interaction_bytes)),
            nonce=nonce,
            secret=secret,
        )
    except (TypeError, ValueError):
        raise FramingError(
            ErrorCode.INVALID_FRAME,
            "The secret response frame is malformed",
        ) from None
