"""Terminal output flow control (#1426), end to end through a real daemon.

A real child floods its PTY. With a flow-controlled input-owner attachment the
daemon stops reading once the frontend is far enough behind, so the child
blocks instead of output being dropped; acknowledgements let it finish with
every byte delivered in order.
"""

import os
import sys
import threading
import time

from sshpilot.api import DaemonClient
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    SessionState,
)
from sshpilot.api.models.terminal import (
    TerminalDimensions,
    TerminalInput,
    TerminalOutputAck,
)
from sshpilot.daemon.pty_runner import PtySessionProcessRunner
from sshpilot.daemon.terminal_stream import FLOW_CONTROL_HIGH_BYTES

_WAIT_TIMEOUT = 20.0
_FLOOD_BYTES = 6 * 1024 * 1024
# Bytes past HIGH the daemon may still read before its pause lands: reads in
# flight on the PTY thread plus one read chunk.
_PAUSE_SLACK = 512 * 1024

# Raw mode so the byte count is exact; writes the flood, a marker, then waits.
_FLOOD_SCRIPT = (
    "import os,tty;tty.setraw(0);"
    f"block=b'x'*65536;n={_FLOOD_BYTES}//65536;"
    "[os.write(1,block) for _ in range(n)];"
    "os.write(1,b'DONE');os.read(0,1)"
)
# Cooked mode so ^C reaches the child as SIGINT; floods until interrupted.
_ENDLESS_SCRIPT = "import os\nwhile True: os.write(1, b'y' * 65536)\n"
# Writes the flood and exits without waiting for input.
_FLOOD_AND_EXIT_SCRIPT = (
    "import os,tty;tty.setraw(0);"
    f"block=b'x'*65536;n={_FLOOD_BYTES}//65536;"
    "[os.write(1,block) for _ in range(n)]"
)


def _wait_until(predicate, timeout=_WAIT_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _runner(script):
    return PtySessionProcessRunner(
        lambda _spec: (
            (sys.executable, "-u", "-c", script),
            {"PATH": os.environ.get("PATH", "")},
        )
    )


class _Collector:
    def __init__(self):
        self.lock = threading.Lock()
        self.chunks = []
        self.next_sequence = 0
        self.gaps = []
        self.losses = []
        self.eof = threading.Event()

    def on_output(self, output):
        with self.lock:
            if output.sequence != self.next_sequence:
                self.gaps.append((self.next_sequence, output.sequence))
            self.chunks.append(output.data)
            self.next_sequence = output.next_sequence

    def on_continuity_lost(self, *args):
        self.losses.append(args)

    def on_eof(self, *_args):
        self.eof.set()

    @property
    def received(self):
        with self.lock:
            return self.next_sequence

    def data(self):
        with self.lock:
            return b"".join(self.chunks)


def _open(client, collector, *, flow_control=True, request_input=True):
    opened = client.open_session(
        OpenSessionRequest(
            connection_id=client.list_connections()[0].id,
            dimensions=TerminalDimensions(rows=24, columns=80),
        )
    )
    subscription = client.subscribe_terminal(
        opened.id,
        collector.on_output,
        on_continuity_lost=collector.on_continuity_lost,
        on_eof=collector.on_eof,
    )
    attached = client.attach_session(
        AttachSessionRequest(
            session_id=opened.id,
            request_input=request_input,
            want_terminal_output=True,
            from_sequence=0,
            flow_control=flow_control,
        )
    )
    return opened, attached, subscription


def _stalls(collector, settle=0.5):
    """The stream stops growing: the daemon has paused the PTY."""

    before = collector.received
    time.sleep(settle)
    return collector.received == before


def _session_ended(client, session_id):
    return client.get_session(session_id).state in {
        SessionState.EXITED,
        SessionState.CLOSED,
    }


def test_daemon_advertises_terminal_flow_control(daemon_factory):
    server, _manager = daemon_factory(session_runner=_runner(_FLOOD_SCRIPT))
    client = DaemonClient(socket_path=server.socket_path)
    try:
        from sshpilot.api import Capability

        assert Capability.TERMINAL_FLOW_CONTROL in client.get_capabilities().supported
    finally:
        client.close()


def test_unacknowledged_output_pauses_the_pty_and_acks_deliver_every_byte(
    daemon_factory,
):
    server, _manager = daemon_factory(session_runner=_runner(_FLOOD_SCRIPT))
    client = DaemonClient(socket_path=server.socket_path)
    collector = _Collector()
    try:
        opened, attached, _subscription = _open(client, collector)
        assert attached.attachment.input_owner is True

        # Nothing acknowledged: the daemon reads about one window and stops.
        assert _wait_until(lambda: collector.received >= FLOW_CONTROL_HIGH_BYTES)
        assert _wait_until(lambda: _stalls(collector))
        assert collector.received <= FLOW_CONTROL_HIGH_BYTES + _PAUSE_SLACK
        assert b"DONE" not in collector.data()

        # Acknowledging what arrived lets the child run to the end.
        def ack_and_check():
            client.acknowledge_terminal_output(
                TerminalOutputAck(
                    session_id=opened.id,
                    attachment_id=attached.attachment.id,
                    sequence=collector.received,
                )
            )
            return collector.data().endswith(b"DONE")

        assert _wait_until(ack_and_check)
        data = collector.data()
        assert len(data) == _FLOOD_BYTES + len(b"DONE")
        assert data[:_FLOOD_BYTES] == b"x" * _FLOOD_BYTES
        assert collector.gaps == []
        assert collector.losses == []
    finally:
        client.close()


def test_input_still_reaches_a_paused_session(daemon_factory):
    """Ctrl+C works during a flood the frontend has not caught up with."""

    server, _manager = daemon_factory(session_runner=_runner(_ENDLESS_SCRIPT))
    client = DaemonClient(socket_path=server.socket_path)
    collector = _Collector()
    try:
        opened, attached, _subscription = _open(client, collector)
        assert _wait_until(lambda: collector.received >= FLOW_CONTROL_HIGH_BYTES)
        assert _wait_until(lambda: _stalls(collector))

        client.send_terminal_input(
            TerminalInput(
                session_id=opened.id,
                attachment_id=attached.attachment.id,
                data=b"\x03",
            )
        )

        # No acknowledgement is ever sent: the exit alone lets the PTY drain.
        assert collector.eof.wait(_WAIT_TIMEOUT)
        assert _wait_until(lambda: _session_ended(client, opened.id))
    finally:
        client.close()


def test_detaching_the_owner_resumes_reading(daemon_factory):
    server, _manager = daemon_factory(
        session_runner=_runner(_FLOOD_AND_EXIT_SCRIPT)
    )
    client = DaemonClient(socket_path=server.socket_path)
    collector = _Collector()
    try:
        opened, attached, _subscription = _open(client, collector)
        assert _wait_until(lambda: collector.received >= FLOW_CONTROL_HIGH_BYTES)
        assert _wait_until(lambda: _stalls(collector))
        assert not _session_ended(client, opened.id)

        client.detach_session(
            DetachSessionRequest(
                session_id=opened.id,
                attachment_id=attached.attachment.id,
            )
        )

        assert _wait_until(lambda: _session_ended(client, opened.id))
    finally:
        client.close()


def test_disconnecting_the_owner_resumes_reading(daemon_factory):
    """A crashed or quit frontend never leaves the remote program stuck."""

    server, _manager = daemon_factory(
        session_runner=_runner(_FLOOD_AND_EXIT_SCRIPT)
    )
    client = DaemonClient(socket_path=server.socket_path)
    observer = DaemonClient(socket_path=server.socket_path)
    collector = _Collector()
    try:
        opened, _attached, _subscription = _open(client, collector)
        assert _wait_until(lambda: collector.received >= FLOW_CONTROL_HIGH_BYTES)
        assert _wait_until(lambda: _stalls(collector))

        client.close()

        assert _wait_until(lambda: _session_ended(observer, opened.id))
    finally:
        client.close()
        observer.close()


def test_attachment_without_flow_control_never_pauses(daemon_factory):
    """Clients that do not acknowledge keep today's behaviour."""

    server, _manager = daemon_factory(
        session_runner=_runner(_FLOOD_AND_EXIT_SCRIPT)
    )
    client = DaemonClient(socket_path=server.socket_path)
    collector = _Collector()
    try:
        opened, _attached, _subscription = _open(
            client, collector, flow_control=False
        )
        assert _wait_until(lambda: _session_ended(client, opened.id))
    finally:
        client.close()


def test_view_only_flow_controlled_attachment_does_not_slow_the_session(
    daemon_factory,
):
    server, _manager = daemon_factory(
        session_runner=_runner(_FLOOD_AND_EXIT_SCRIPT)
    )
    owner = DaemonClient(socket_path=server.socket_path)
    viewer = DaemonClient(socket_path=server.socket_path)
    owner_output = _Collector()
    viewer_output = _Collector()
    try:
        # The owner attaches without flow control and before the viewer, so
        # the session's pace is the owner's; the viewer never acknowledges.
        opened = owner.open_session(
            OpenSessionRequest(
                connection_id=owner.list_connections()[0].id,
                dimensions=TerminalDimensions(rows=24, columns=80),
            )
        )
        owner.subscribe_terminal(opened.id, owner_output.on_output)
        owner.attach_session(
            AttachSessionRequest(
                session_id=opened.id,
                request_input=True,
                want_terminal_output=True,
            )
        )
        viewer.subscribe_terminal(opened.id, viewer_output.on_output)
        attached = viewer.attach_session(
            AttachSessionRequest(
                session_id=opened.id,
                request_input=True,
                want_terminal_output=True,
                flow_control=True,
            )
        )
        assert attached.attachment.input_owner is False

        assert _wait_until(lambda: _session_ended(owner, opened.id))
    finally:
        owner.close()
        viewer.close()


def test_acknowledgements_for_unknown_attachments_are_ignored(daemon_factory):
    server, _manager = daemon_factory(session_runner=_runner(_FLOOD_SCRIPT))
    client = DaemonClient(socket_path=server.socket_path)
    other = DaemonClient(socket_path=server.socket_path)
    collector = _Collector()
    try:
        opened, attached, _subscription = _open(client, collector)
        assert _wait_until(lambda: collector.received >= FLOW_CONTROL_HIGH_BYTES)
        assert _wait_until(lambda: _stalls(collector))

        # Another client cannot release a session it does not own, and a
        # stale attachment id is harmless; both keep the connection open.
        other.acknowledge_terminal_output(
            TerminalOutputAck(
                session_id=opened.id,
                attachment_id=attached.attachment.id,
                sequence=collector.received,
            )
        )
        client.acknowledge_terminal_output(
            TerminalOutputAck(
                session_id=opened.id,
                attachment_id="attachment-gone",
                sequence=collector.received,
            )
        )
        assert _stalls(collector)
        assert client.get_session(opened.id).state is SessionState.RUNNING
        assert other.get_session(opened.id).state is SessionState.RUNNING
    finally:
        client.close()
        other.close()


def test_a_replay_chunk_short_of_live_reports_no_continuity_loss(daemon_factory):
    """Chunked replay through the real client and server (#1426).

    Recovery replays 512 KiB at a time. Each chunk that stopped short of the
    live end used to arrive flagged TRUNCATED on every frame, and the client
    reported a continuity loss for each, restarting recovery forever.
    """

    from sshpilot.api.models.terminal import ReplayRequest

    script = (
        "import os,tty;tty.setraw(0);"
        "os.write(1,b'r'*(1536*1024));os.write(1,b'END');os.read(0,1)"
    )
    server, _manager = daemon_factory(session_runner=_runner(script))
    client = DaemonClient(socket_path=server.socket_path)
    collector = _Collector()
    try:
        opened, attached, _subscription = _open(client, collector, flow_control=False)
        assert _wait_until(lambda: collector.data().endswith(b"END"))
        losses_before = list(collector.losses)

        chunk = client.replay_terminal(
            ReplayRequest(
                session_id=opened.id,
                attachment_id=attached.attachment.id,
                after_sequence=0,
                max_bytes=512 * 1024,
            )
        )
        assert chunk.truncated is True  # more remains: this is one chunk
        time.sleep(0.5)
        assert collector.losses == losses_before
    finally:
        client.close()
