from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sshpilot.api import EventType
from sshpilot.api.events import CoreEvent
from sshpilot.api.models import ConnectionId, ConnectionSummary
from sshpilot.api.models.common import RequestId, SessionId
from sshpilot.api.models.interactions import (
    InteractionId,
    InteractionState,
    InteractionSummary,
    InteractionType,
    PasswordPrompt,
)
from sshpilot.api.models.common import ClientId
from sshpilot.api.transport.envelopes import SuccessResponseEnvelope
from sshpilot.api.transport.terminal_frames import TerminalFrame, TerminalFrameKind
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
        id=ConnectionId("demo"),
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


def _interaction_event(sequence):
    created_at = datetime.now(timezone.utc)
    summary = InteractionSummary(
        id=InteractionId("interaction-1"),
        session_id=SessionId("session-1"),
        connection_id=ConnectionId("demo"),
        type=InteractionType.PASSWORD,
        state=InteractionState.PENDING,
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=30),
        attempt=1,
        prompt=PasswordPrompt(
            username="alice",
            hostname="example.test",
            port=22,
            attempt=1,
            can_remember=False,
            stored_secret_available=False,
        ),
    )
    return CoreEvent(
        type=EventType.INTERACTION_CREATED,
        payload=summary,
        sequence=sequence,
        session_id=SessionId("session-1"),
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


def test_event_is_encoded_per_peer_with_peer_scoped_sequence(tmp_path):
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

    # Event sequences are scoped to each peer because interaction events may
    # be filtered by ownership. Healthy peers with the same visibility still
    # receive byte-equivalent envelopes, but they do not share mutable frame
    # state or rely on one daemon-global continuity stream.
    assert first.output[0].data == second.output[0].data
    assert incomplete.output == deque()
    assert first.queued_event_count == second.queued_event_count == 1


def test_filtered_interaction_does_not_create_peer_continuity_gap(tmp_path):
    server = DaemonServer(
        lambda: None,
        socket_path=tmp_path / "sshpilotd.sock",
    )
    owner = _handshaken_state(10)
    observer = _handshaken_state(11)
    owner.protocol.client_id = ClientId("owner")
    observer.protocol.client_id = ClientId("observer")
    server._clients = {10: owner, 11: observer}
    server._accepting_core_events = True
    server._client_can_interact = lambda _session, client_id: str(client_id) == "owner"

    server._on_core_event(_interaction_event(0))
    server._on_core_event(_event(1))

    # The observer did not receive the ownership-filtered interaction event,
    # so its next visible event must still be contiguous at sequence zero.
    assert owner.protocol.next_event_sequence == 2
    assert observer.protocol.next_event_sequence == 1
    assert len(owner.output) == 2
    assert len(observer.output) == 1


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


def test_terminal_overflow_preserves_control_and_healthy_peer(tmp_path):
    server = DaemonServer(
        lambda: None,
        socket_path=tmp_path / "sshpilotd.sock",
        max_client_outbound_bytes=4096,
        max_client_terminal_bytes=256,
    )
    slow = _handshaken_state(10)
    healthy = _handshaken_state(11)
    session_id = SessionId(
        "session-1"
    )
    frame = TerminalFrame(
        kind=TerminalFrameKind.OUTPUT,
        session_id=session_id,
        sequence=0,
        data=b"x" * 96,
    )
    slow.output.append(
        _OutboundFrame(
            b"old" * 50,
            is_terminal=True,
            session_id=session_id,
        )
    )
    slow.queued_terminal_bytes = 150
    slow.queued_outbound_bytes = 150

    server._queue_terminal_frame(slow, frame)
    server._queue_terminal_frame(healthy, frame)
    server._write_client(healthy)
    server._queue_response(
        slow,
        SuccessResponseEnvelope(
            protocol_version=PROTOCOL_VERSION,
            request_id=RequestId("request:still-responsive"),
            result={"ok": True},
        ),
    )

    assert slow.closed is False
    assert slow.queued_terminal_bytes == 0
    assert slow.queued_outbound_bytes <= server.max_client_outbound_bytes
    assert any(not queued.is_terminal for queued in slow.output)
    assert session_id in slow.terminal_continuity_lost
    queued_after_loss = len(slow.output)
    server._queue_terminal_frame(
        slow,
        TerminalFrame(
            kind=TerminalFrameKind.OUTPUT,
            session_id=session_id,
            sequence=96,
            data=b"ignored-until-replay",
        ),
    )
    assert len(slow.output) == queued_after_loss
    server._queue_replay(
        slow,
        session_id,
        SimpleNamespace(
            truncated=False,
            chunks=((0, b"recovered"),),
            eof=False,
            returned_end=9,
        ),
    )
    assert session_id not in slow.terminal_continuity_lost
    assert any(frame.is_terminal for frame in slow.output)
    assert healthy.closed is False
    assert healthy.queued_terminal_bytes == 0
    assert bytes(healthy.sock.sent)


def test_control_room_drop_reports_terminal_continuity_loss(tmp_path):
    server = DaemonServer(
        lambda: None,
        socket_path=tmp_path / "sshpilotd.sock",
        max_client_outbound_bytes=600,
        max_client_terminal_bytes=600,
    )
    state = _handshaken_state(10)
    session_id = SessionId(
        "session-1"
    )
    state.output.append(
        _OutboundFrame(
            b"x" * 580,
            is_terminal=True,
            session_id=session_id,
            terminal_sequence=37,
        )
    )
    state.queued_terminal_bytes = 580
    state.queued_outbound_bytes = 580

    server._queue_response(
        state,
        SuccessResponseEnvelope(
            protocol_version=PROTOCOL_VERSION,
            request_id=RequestId("request:needs-control-room"),
            result={"ok": True},
        ),
    )

    assert state.closed is False
    assert state.queued_terminal_bytes == 0
    assert any(
        frame.session_id == session_id and not frame.is_terminal
        for frame in state.output
    )
    assert any(frame.session_id is None for frame in state.output)
    assert state.queued_outbound_bytes <= server.max_client_outbound_bytes


def test_control_and_lifecycle_frames_overtake_terminal_without_reordering_events(
    tmp_path,
):
    server = DaemonServer(
        lambda: None,
        socket_path=tmp_path / "sshpilotd.sock",
    )
    state = _handshaken_state(10)
    terminal = _OutboundFrame(
        b"terminal",
        is_terminal=True,
        session_id=SessionId(
            "session-1"
        ),
    )
    with server._event_lock:
        server._enqueue_frame_locked(state, terminal)
        server._enqueue_frame_locked(
            state,
            _OutboundFrame(b"event-one", is_event=True),
            priority=True,
        )
        server._enqueue_frame_locked(
            state,
            _OutboundFrame(b"event-two", is_event=True),
            priority=True,
        )
        server._enqueue_frame_locked(
            state,
            _OutboundFrame(b"response"),
            priority=True,
        )

    assert [frame.data for frame in state.output] == [
        b"response",
        b"event-one",
        b"event-two",
        b"terminal",
    ]
