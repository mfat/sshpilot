import struct

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.common import AttachmentId, SessionId
from sshpilot.api.transport.framing import (
    FramingError,
    MultiplexedFrameDecoder,
    encode_binary_frame,
)
from sshpilot.api.transport.terminal_frames import (
    MAX_TERMINAL_PAYLOAD_SIZE,
    TerminalFrame,
    TerminalFrameFlags,
    TerminalFrameKind,
    decode_terminal_payload,
    encode_terminal_payload,
)


def _session_id():
    return SessionId("session-test")


def _attachment_id():
    return AttachmentId("attachment-test")


def test_terminal_output_frame_preserves_arbitrary_bytes_and_metadata():
    frame = TerminalFrame(
        kind=TerminalFrameKind.OUTPUT,
        session_id=_session_id(),
        sequence=42,
        data=b"\x00\xffsplit\r\n",
        flags=TerminalFrameFlags.REPLAY,
    )

    decoded = decode_terminal_payload(encode_terminal_payload(frame))

    assert decoded == frame


def test_terminal_frame_ids_round_trip_without_shared_process_state():
    """Daemon encodes; UI must decode the same id with no shared map."""
    payload = encode_terminal_payload(
        TerminalFrame(
            kind=TerminalFrameKind.OUTPUT,
            session_id=SessionId("session-1"),
            sequence=1,
            data=b"hi",
        )
    )
    # Fresh decode path (no encode in this "process") must recover session-1.
    decoded = decode_terminal_payload(payload)
    assert decoded.session_id == "session-1"
    assert decoded.data == b"hi"


def test_terminal_input_frame_requires_a_strict_attachment_uuid():
    with pytest.raises(ValueError):
        TerminalFrame(
            kind=TerminalFrameKind.INPUT,
            session_id=_session_id(),
            sequence=0,
            data=b"x",
        )

    frame = TerminalFrame(
        kind=TerminalFrameKind.INPUT,
        session_id=_session_id(),
        attachment_id=_attachment_id(),
        sequence=0,
        data=b"\0",
    )
    assert decode_terminal_payload(encode_terminal_payload(frame)) == frame


def test_terminal_input_error_frame_carries_only_stable_error_code():
    frame = TerminalFrame(
        kind=TerminalFrameKind.INPUT_ERROR,
        session_id=_session_id(),
        attachment_id=_attachment_id(),
        sequence=7,
        data=ErrorCode.TERMINAL_INPUT_OWNER_REQUIRED.value.encode("ascii"),
    )

    assert decode_terminal_payload(encode_terminal_payload(frame)) == frame


def test_multiplexed_decoder_handles_fragmented_binary_and_json_frames():
    terminal = TerminalFrame(
        kind=TerminalFrameKind.OUTPUT,
        session_id=_session_id(),
        sequence=0,
        data=b"hello",
    )
    binary = encode_binary_frame(encode_terminal_payload(terminal))
    json_payload = b'{"type":"probe"}'
    json_frame = struct.pack(">I", len(json_payload)) + json_payload
    decoder = MultiplexedFrameDecoder()

    decoded = []
    for byte in binary + json_frame:
        decoded.extend(decoder.feed(bytes((byte,))))

    assert decoded == [terminal, {"type": "probe"}]


def test_terminal_frame_limits_and_malformed_payload_are_rejected():
    with pytest.raises(ValueError):
        TerminalFrame(
            kind=TerminalFrameKind.OUTPUT,
            session_id=_session_id(),
            sequence=0,
            data=b"x" * (MAX_TERMINAL_PAYLOAD_SIZE + 1),
        )
    with pytest.raises(FramingError) as raised:
        decode_terminal_payload(b"SPTB")
    assert raised.value.code is ErrorCode.INVALID_FRAME
