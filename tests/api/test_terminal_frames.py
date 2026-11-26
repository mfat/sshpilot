import struct
import threading
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.daemon_client import DaemonClient
from sshpilot.api.models.common import AttachmentId, SessionId
from sshpilot.api.models.terminal import TerminalInput
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


def test_client_correlates_input_error_with_unique_input_sequence():
    sent = bytearray()
    client = object.__new__(DaemonClient)
    client._require_capability = lambda _capability: None
    client._state_lock = threading.RLock()
    client._send_lock = threading.Lock()
    client._terminal_input_sequences = {}
    client._closed = False
    client._socket = SimpleNamespace(sendall=sent.extend)
    client._terminal_sequences = {}
    client._terminal_overflow = set()
    terminal_items = []
    client._queue_terminal_item = terminal_items.append

    for data in (b"first", b"second"):
        client.send_terminal_input(
            TerminalInput(
                session_id=_session_id(),
                attachment_id=_attachment_id(),
                data=data,
            )
        )

    decoded = MultiplexedFrameDecoder().feed(bytes(sent))
    assert [(frame.sequence, frame.data) for frame in decoded] == [
        (0, b"first"),
        (1, b"second"),
    ]

    assert client._receive_terminal(
        TerminalFrame(
            kind=TerminalFrameKind.INPUT_ERROR,
            session_id=_session_id(),
            attachment_id=_attachment_id(),
            sequence=1,
            data=ErrorCode.TERMINAL_INPUT_BACKPRESSURE.value.encode("ascii"),
        )
    )
    assert terminal_items[0].details["input_sequence"] == 1


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


def test_terminal_ack_frame_carries_an_attachment_and_no_data():
    frame = TerminalFrame(
        kind=TerminalFrameKind.ACK,
        session_id=_session_id(),
        attachment_id=_attachment_id(),
        sequence=1_048_576,
    )
    assert decode_terminal_payload(encode_terminal_payload(frame)) == frame

    with pytest.raises(ValueError):
        TerminalFrame(
            kind=TerminalFrameKind.ACK,
            session_id=_session_id(),
            sequence=1,
        )
    with pytest.raises(ValueError):
        TerminalFrame(
            kind=TerminalFrameKind.ACK,
            session_id=_session_id(),
            attachment_id=_attachment_id(),
            sequence=1,
            data=b"x",
        )
    with pytest.raises(ValueError):
        TerminalFrame(
            kind=TerminalFrameKind.ACK,
            session_id=_session_id(),
            attachment_id=_attachment_id(),
            sequence=1,
            flags=TerminalFrameFlags.REPLAY,
        )


def test_client_sends_terminal_output_ack_as_a_binary_frame():
    from sshpilot.api.models.terminal import TerminalOutputAck

    sent = bytearray()
    client = object.__new__(DaemonClient)
    client._require_capability = lambda _capability: None
    client._state_lock = threading.RLock()
    client._send_lock = threading.Lock()
    client._closed = False
    client._socket = SimpleNamespace(sendall=sent.extend)

    client.acknowledge_terminal_output(
        TerminalOutputAck(
            session_id=_session_id(),
            attachment_id=_attachment_id(),
            sequence=4096,
        )
    )

    (frame,) = MultiplexedFrameDecoder().feed(bytes(sent))
    assert frame.kind is TerminalFrameKind.ACK
    assert frame.attachment_id == _attachment_id()
    assert frame.sequence == 4096
    assert frame.data == b""


def test_client_terminal_queue_is_bounded_by_bytes_not_frames():
    """256 small frames used to overflow under 1 MiB during a burst (#1426)."""

    import queue
    from datetime import datetime, timezone

    from sshpilot.api.daemon_client import DEFAULT_CLIENT_TERMINAL_QUEUE_BYTES
    from sshpilot.api.models.terminal import TerminalOutput

    client = object.__new__(DaemonClient)
    client._state_lock = threading.RLock()
    client._terminal_queue = queue.Queue(maxsize=65536)
    client._terminal_queue_bytes = 0
    client._terminal_queue_byte_limit = DEFAULT_CLIENT_TERMINAL_QUEUE_BYTES
    client._terminal_overflow = set()

    def output(sequence, size):
        return TerminalOutput(
            session_id=_session_id(),
            sequence=sequence,
            data=b"x" * size,
            created_at=datetime.now(timezone.utc),
        )

    frames = 1024  # 4 MiB in 4 KiB PTY reads: far past the old 256-item bound
    for index in range(frames):
        client._queue_terminal_item(output(index * 4096, 4096))
    assert client._terminal_queue.qsize() == frames
    assert client._terminal_overflow == set()

    big = DEFAULT_CLIENT_TERMINAL_QUEUE_BYTES - client._terminal_queue_bytes
    client._queue_terminal_item(output(frames * 4096, big + 1))
    assert client._terminal_overflow == {_session_id()}
