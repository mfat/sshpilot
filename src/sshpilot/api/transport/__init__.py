"""Transport-neutral protocol envelopes and codecs for local IPC."""

from .codec import (
    decode_envelope,
    encode_envelope,
    error_from_wire,
    error_to_wire,
)
from .envelopes import (
    ErrorData,
    ErrorResponseEnvelope,
    EventEnvelope,
    HandshakeRequest,
    HandshakeResult,
    RequestEnvelope,
    SuccessResponseEnvelope,
)
from .framing import (
    MAX_FRAME_SIZE,
    FrameDecoder,
    FramingError,
    encode_frame,
    receive_frame,
)

__all__ = [
    "MAX_FRAME_SIZE",
    "ErrorData",
    "ErrorResponseEnvelope",
    "EventEnvelope",
    "FrameDecoder",
    "FramingError",
    "HandshakeRequest",
    "HandshakeResult",
    "RequestEnvelope",
    "SuccessResponseEnvelope",
    "decode_envelope",
    "encode_envelope",
    "encode_frame",
    "error_from_wire",
    "error_to_wire",
    "receive_frame",
]
