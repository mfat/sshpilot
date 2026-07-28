from collections import deque

from sshpilot.api import EventType
from sshpilot.api.events import CoreEvent
from sshpilot.api.models import ConnectionId, ConnectionSummary
from sshpilot.api.models.common import RequestId
from sshpilot.api.transport.envelopes import SuccessResponseEnvelope
from sshpilot.api.version import PROTOCOL_VERSION
from sshpilot.daemon.server import (
    DaemonServer,
    _ClientConnection,
    _OutboundFrame,
)


class _FakeSocket:
    def __init__(self, file_descriptor, *, send_limit=None):
        self._file_descriptor = file_descriptor
        self.send_limit = send_limit
        self.sent = bytearray()
        self.closed = False

    def fileno(self):
        return -1 if self.closed else self._file_descriptor

    def send(self, data):
        size = len(data) if self.send_limit is None else min(
            len(data),
            self.send_limit,
        )
        self.sent.extend(data[:size])
        return size

    def close(self):
        self.closed = True


def _event(sequence):
    summary = ConnectionSummary(
        id=ConnectionId("connection:v1:demo"),
        nickname="demo",
        host="demo",
        hostname="example.test",
        username="alice",
        port=22,
    )
    return CoreEvent(
        type=EventType.CONNECTION_UPDATED,
        payload=summary,
        sequence=sequence,
    )


def _handshaken_state(file_descriptor, *, send_limit=None):
    state = _ClientConnection(
        _FakeSocket(file_descriptor, send_limit=send_limit)
    )
    state.protocol.handshake_completed = True
    return state


def test_event_queue_overflow_disconnects_only_the_slow_peer(tmp_path):
    server = DaemonServer(
        lambda: None,
        socket_path=tmp_path / "sshpilotd.sock",
        client_event_queue_limit=2,
    )
    slow = _handshaken_state(10)
    healthy = _handshaken_state(11)
    server._clients = {10: slow, 11: healthy}
    server._accepting_core_events = True

    for sequence in range(3):
        server._on_core_event(_event(sequence))
        server._write_client(healthy)

    assert slow.continuity_lost is True
    assert slow.queued_event_count == 0
    assert len(slow.output) == 0
    assert healthy.continuity_lost is False
    assert healthy.queued_event_count == 0
    assert len(healthy.sock.sent) > 0
    assert server._next_event_sequence == 3

    server._drain_wakeup()

    assert slow.closed is True
    assert healthy.closed is False
    assert tuple(server._clients.values()) == (healthy,)


def test_event_is_encoded_once_and_shared_by_healthy_peers(tmp_path):
    server = DaemonServer(
        lambda: None,
        socket_path=tmp_path / "sshpilotd.sock",
    )
    first = _handshaken_state(10)
    second = _handshaken_state(11)
    incomplete = _ClientConnection(_FakeSocket(12))
    server._clients = {10: first, 11: second, 12: incomplete}
    server._accepting_core_events = True

    server._on_core_event(_event(99))

    assert first.output[0].data is second.output[0].data
    assert incomplete.output == deque()
    assert first.queued_event_count == second.queued_event_count == 1


def test_partial_writes_resume_without_interleaving_frames(tmp_path):
    server = DaemonServer(
        lambda: None,
        socket_path=tmp_path / "sshpilotd.sock",
    )
    state = _handshaken_state(10, send_limit=3)
    state.output.extend(
        [
            _OutboundFrame(b"first", is_event=True),
            _OutboundFrame(b"second", is_event=False),
        ]
    )
    state.queued_event_count = 1
    state.queued_outbound_bytes = len(b"firstsecond")

    server._write_client(state)
    assert bytes(state.sock.sent) == b"fir"
    assert state.output_offset == 3
    assert state.queued_event_count == 1
    assert state.queued_outbound_bytes == len(b"stsecond")

    server._write_client(state)
    assert bytes(state.sock.sent) == b"first"
    assert state.output_offset == 0
    assert state.queued_event_count == 0
    assert state.queued_outbound_bytes == len(b"second")

    server._write_client(state)
    server._write_client(state)

    assert bytes(state.sock.sent) == b"firstsecond"
    assert not state.output
    assert state.queued_outbound_bytes == 0


def test_total_outbound_byte_limit_isolates_only_affected_peer(tmp_path):
    server = DaemonServer(
        lambda: None,
        socket_path=tmp_path / "sshpilotd.sock",
        max_client_outbound_bytes=4096,
    )
    slow = _handshaken_state(10)
    healthy = _handshaken_state(11)
    slow.output.append(_OutboundFrame(b"x" * 4000))
    slow.queued_outbound_bytes = 4000
    server._clients = {10: slow, 11: healthy}
    server._accepting_core_events = True

    server._on_core_event(_event(0))
    server._write_client(healthy)
    server._drain_wakeup()

    assert slow.closed is True
    assert slow.queued_outbound_bytes == 0
    assert healthy.closed is False
    assert healthy.queued_outbound_bytes == 0
    assert bytes(healthy.sock.sent)


def test_response_frames_count_toward_total_outbound_limit(tmp_path):
    server = DaemonServer(
        lambda: None,
        socket_path=tmp_path / "sshpilotd.sock",
        max_client_outbound_bytes=64,
    )
    state = _handshaken_state(10)
    server._clients = {10: state}

    server._queue_response(
        state,
        SuccessResponseEnvelope(
            protocol_version=PROTOCOL_VERSION,
            request_id=RequestId("request:test"),
            result={"value": "x" * 200},
        ),
    )

    assert state.closed is True
    assert state.queued_outbound_bytes == 0
