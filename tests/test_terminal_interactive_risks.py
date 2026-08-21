"""Focused contracts for the remaining daemon-terminal interaction risks."""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import AttachmentId, ConnectionId, SessionId
from sshpilot.api.models.sessions import (
    AttachSessionResult,
    AttachmentInfo,
    SessionState,
    SessionSummary,
)
from sshpilot.api.models.terminal import (
    ReplayBounds,
    ReplayResult,
    TerminalDimensions,
    TerminalOutput,
)
from sshpilot.api.terminal_events import TerminalSubscription
from sshpilot.api.transport.framing import MultiplexedFrameDecoder
from sshpilot.api.transport.terminal_frames import TerminalFrameKind
from sshpilot.daemon.server import DaemonServer, _ClientConnection
from sshpilot.gtk_client_bridge import GtkClientBridge
from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_session_controller import (
    DaemonTerminalSessionController,
    TerminalSessionState,
    required_daemon_terminal_capabilities,
)


_SESSION_ID = SessionId("session-interactive-risk")
_ATTACHMENT_ID = AttachmentId("attachment-interactive-risk")


class _Dispatcher:
    def __init__(self) -> None:
        self._items: queue.Queue = queue.Queue()

    def __call__(self, callback, *args):
        self._items.put((callback, args))
        return 1

    def run_one(self, timeout: float = 2.0) -> bool:
        callback, args = self._items.get(timeout=timeout)
        return bool(callback(*args))

    def drain(self) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                callback, args = self._items.get_nowait()
            except queue.Empty:
                return
            while callback(*args):
                pass
        raise AssertionError("GTK dispatcher did not drain")

    def run_until(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            remaining = max(0.001, deadline - time.monotonic())
            try:
                callback, args = self._items.get(timeout=remaining)
            except queue.Empty:
                continue
            while callback(*args):
                pass
        raise AssertionError("GTK condition was not reached")


class _TerminalClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.server_instance_id = "daemon-interactive-risk"
        self.output_callbacks = None
        self.rpc_order = []
        self.pty_input = []
        self.input_called = threading.Event()
        self.first_resize_entered = threading.Event()
        self.release_first_resize = threading.Event()
        self.block_first_resize = False
        self.reject_input = False
        self.block_input = False
        self.release_input = threading.Event()
        self.resize_rpc_count = 0
        self.replay_rpc_count = 0
        self.replay_start = None
        self.recovery_frames = []
        self.replay_truncated = False
        self.attach_called = threading.Event()
        self.release_attach = threading.Event()
        self.block_attach = False
        self.detached = []
        self.last_resize = None

    def get_capabilities(self):
        return SimpleNamespace(supported=required_daemon_terminal_capabilities())

    def subscribe_terminal(
        self,
        _session_id,
        on_output,
        *,
        on_continuity_lost=None,
        on_eof=None,
        on_error=None,
    ):
        callbacks = SimpleNamespace(
            on_output=on_output,
            on_continuity_lost=on_continuity_lost,
            on_eof=on_eof,
            on_error=on_error,
            closed=False,
        )
        self.output_callbacks = callbacks
        return TerminalSubscription(lambda: setattr(callbacks, "closed", True))

    def emit_output(self, sequence: int, data: bytes, *, replay=False) -> bool:
        callbacks = self.output_callbacks
        if callbacks is None or callbacks.closed:
            return False
        callbacks.on_output(
            TerminalOutput(
                session_id=_SESSION_ID,
                sequence=sequence,
                data=data,
                replay=replay,
            )
        )
        return True

    def emit_continuity(self, expected: int, available: int) -> None:
        callbacks = self.output_callbacks
        assert callbacks is not None and not callbacks.closed
        callbacks.on_continuity_lost(_SESSION_ID, expected, available)

    def send_terminal_input(self, request) -> None:
        self.rpc_order.append(("input", request.data))
        self.input_called.set()
        if self.block_input:
            assert self.release_input.wait(2.0)
        if self.reject_input:
            callbacks = self.output_callbacks
            assert callbacks is not None and callbacks.on_error is not None
            callbacks.on_error(
                SshPilotError(
                    ErrorCode.TERMINAL_INPUT_BACKPRESSURE,
                    "The terminal input was rejected",
                    retryable=True,
                    session_id=_SESSION_ID,
                )
            )
        else:
            self.pty_input.append(request.data)

    def resize_terminal(self, request) -> None:
        self.resize_rpc_count += 1
        call_number = self.resize_rpc_count
        self.rpc_order.append(("resize", request.dimensions))
        self.last_resize = request.dimensions
        if self.block_first_resize and call_number == 1:
            self.first_resize_entered.set()
            assert self.release_first_resize.wait(2.0)

    def replay_terminal(self, request):
        self.replay_rpc_count += 1
        self.replay_start = request.after_sequence
        replay_ends = [
            sequence + len(data)
            for sequence, data, is_replay in self.recovery_frames
            if is_replay
        ]
        live_sequence = max(replay_ends, default=request.after_sequence)
        returned_end = request.after_sequence
        remaining = request.max_bytes
        for sequence, data, replay in self.recovery_frames:
            if not replay:
                if request.after_sequence == self.replay_start:
                    assert self.emit_output(sequence, data, replay=False)
                continue
            overlap = max(0, request.after_sequence - sequence)
            if overlap >= len(data) or remaining == 0:
                continue
            chunk = data[overlap : overlap + remaining]
            chunk_sequence = sequence + overlap
            assert self.emit_output(chunk_sequence, chunk, replay=True)
            returned_end = chunk_sequence + len(chunk)
            remaining -= len(chunk)
        truncated = self.replay_truncated or returned_end < live_sequence
        first = (
            request.after_sequence + 1
            if self.replay_truncated
            else request.after_sequence
        )
        return ReplayResult(
            session_id=_SESSION_ID,
            first_sequence=first,
            next_sequence=returned_end,
            bounds=ReplayBounds(
                earliest_sequence=first,
                latest_sequence=live_sequence,
                retained_bytes=max(0, live_sequence - first),
            ),
            truncated=truncated,
        )

    def attach_session(self, request):
        self.attach_called.set()
        if self.block_attach:
            assert self.release_attach.wait(2.0)
        self.replay_start = request.from_sequence
        for sequence, data, replay in self.recovery_frames:
            assert self.emit_output(sequence, data, replay=replay)
        replay_ends = [
            sequence + len(data)
            for sequence, data, is_replay in self.recovery_frames
            if is_replay
        ]
        latest = max(replay_ends, default=request.from_sequence)
        return AttachSessionResult(
            session=SessionSummary(
                id=_SESSION_ID,
                connection_id=ConnectionId("connection-interactive-risk"),
                state=SessionState.RUNNING,
            ),
            attachment=AttachmentInfo(
                id=AttachmentId(f"attachment-{self.name}"),
                session_id=_SESSION_ID,
                client_id=f"client-{self.name}",
                input_owner=True,
            ),
            available_start=(latest if self.replay_truncated else request.from_sequence),
            live_sequence=latest,
            replay_truncated=self.replay_truncated,
        )

    def detach_session(self, request):
        self.detached.append(request.attachment_id)

    def close_session(self, _request):
        return None


class _QueuedReplayClient(_TerminalClient):
    """Drive the real server replay queue while outbound writes are stalled."""

    class _StalledSocket:
        def fileno(self):
            return -1

        def close(self):
            return None

    def __init__(self, recovery_start: int, retained: bytes, socket_path) -> None:
        super().__init__("queued-replay")
        self.recovery_start = recovery_start
        self.retained = bytearray(retained)
        self.deleted_queued_replay_bytes = 0
        self._replay_condition = threading.Condition()
        self._server = DaemonServer(lambda: None, socket_path=socket_path)
        self._server_state = _ClientConnection(self._StalledSocket())

    def append_during_recovery(self, data: bytes) -> None:
        with self._replay_condition:
            self.retained.extend(data)

    def replay_terminal(self, request):
        with self._replay_condition:
            self.replay_rpc_count += 1
            self.replay_start = request.after_sequence
            queued_before = self._queued_payload_bytes()

            live_sequence = self.recovery_start + len(self.retained)
            returned_end = min(
                live_sequence,
                request.after_sequence + request.max_bytes,
            )
            offset = request.after_sequence - self.recovery_start
            data = bytes(self.retained[offset : offset + request.max_bytes])
            truncated = returned_end < live_sequence
            self._server._queue_replay(
                self._server_state,
                _SESSION_ID,
                SimpleNamespace(
                    chunks=((request.after_sequence, data),),
                    truncated=truncated,
                    eof=False,
                    returned_end=returned_end,
                ),
            )
            if queued_before:
                self.deleted_queued_replay_bytes += queued_before
            result = ReplayResult(
                session_id=_SESSION_ID,
                first_sequence=request.after_sequence,
                next_sequence=returned_end,
                bounds=ReplayBounds(
                    earliest_sequence=self.recovery_start,
                    latest_sequence=live_sequence,
                    retained_bytes=len(self.retained),
                ),
                truncated=truncated,
            )
            self._replay_condition.notify_all()
            return result

    def flush_queued_replay(self) -> int:
        with self._replay_condition:
            frames = self._decode_queued_frames()
            self._server_state.output.clear()
            self._server_state.queued_terminal_bytes = 0
            self._server_state.queued_outbound_bytes = 0
        delivered = 0
        for frame in frames:
            assert frame.kind is TerminalFrameKind.OUTPUT
            delivered += len(frame.data)
            callbacks = self.output_callbacks
            assert callbacks is not None and not callbacks.closed
            callbacks.on_output(
                TerminalOutput(
                    session_id=frame.session_id,
                    sequence=frame.sequence,
                    data=frame.data,
                    replay=True,
                )
            )
        return delivered

    def _decode_queued_frames(self):
        decoder = MultiplexedFrameDecoder()
        frames = []
        for outbound in self._server_state.output:
            if outbound.is_terminal:
                frames.extend(decoder.feed(outbound.data))
        return frames

    def _queued_payload_bytes(self) -> int:
        return sum(len(frame.data) for frame in self._decode_queued_frames())

    def wait_for_replay_requests(self, count: int) -> None:
        deadline = time.monotonic() + 2.0
        with self._replay_condition:
            while self.replay_rpc_count < count:
                remaining = deadline - time.monotonic()
                assert remaining > 0
                self._replay_condition.wait(remaining)


def _active_controller(client, bridge, *, on_output=None, on_error=None, on_loss=None):
    controller = DaemonTerminalSessionController(
        client=client,
        bridge=bridge,
        connection_id=ConnectionId("connection-interactive-risk"),
        view_id="view-interactive-risk",
        on_output=on_output,
        on_error=on_error,
        on_continuity_lost=on_loss,
    )
    controller._tab_state.session_id = _SESSION_ID
    controller._tab_state.attachment_id = _ATTACHMENT_ID
    controller._tab_state.input_owner = True
    controller._tab_state.state = TerminalSessionState.ACTIVE
    return controller


def _bind_active_stream(controller, bridge, client):
    _generation, binding = controller._replace_output_binding(client, paused=False)
    assert binding is not None
    return binding


def test_terminal_continuity_loss_does_not_leave_view_permanently_dead():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    client = _TerminalClient("client")
    output = []
    display = []
    resets = []
    widget = SimpleNamespace(
        backend=SimpleNamespace(
            reset=lambda clear_scrollback, clear_screen: resets.append(
                (clear_scrollback, clear_screen)
            )
        ),
        _feed_display=display.append,
    )
    controller = _active_controller(
        client,
        bridge,
        on_output=output.append,
        on_loss=lambda: TerminalWidget._on_daemon_continuity_lost(widget),
    )
    binding = _bind_active_stream(controller, bridge, client)
    first = b"HEARTBEAT-0001\r\n"
    replayed = b"REPLAY-AFTER-GAP\r\n"
    later = b"HEARTBEAT-0002\r\n"
    client.recovery_frames = [
        (len(first), b"STALE-LIVE-BEFORE-REPLAY", False),
        (len(first), replayed, True),
    ]
    token = "INPUT-AFTER-CONTINUITY-LOSS"
    input_widget = SimpleNamespace(
        _daemon_controller=controller,
        has_input_ownership=True,
    )
    try:
        assert client.emit_output(0, first)
        dispatcher.drain()
        client.emit_continuity(len(first), len(first) + 64)
        dispatcher.run_until(
            lambda: controller.state is TerminalSessionState.ACTIVE
            and replayed in output
        )
        assert client.emit_output(len(first) + len(replayed), later)
        dispatcher.drain()

        TerminalWidget._on_daemon_commit(
            input_widget,
            None,
            token,
            len(token),
        )
        assert client.input_called.wait(2.0)

        assert output == [first, replayed, later]
        assert client.pty_input == [token.encode()]
        assert controller.state is TerminalSessionState.ACTIVE
        assert resets == []
        assert display == []
        assert client.replay_rpc_count == 1
        assert client.replay_start == len(first)
    finally:
        binding.close()
        controller._stream = None
        bridge.shutdown(wait=True)


def test_terminal_input_not_blocked_by_resize_backlog():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    client = _TerminalClient("client")
    client.block_first_resize = True
    controller = _active_controller(client, bridge)
    frontend_resize_events = 200
    sentinels = [
        b"INPUT-SENTINEL-RESIZE-1",
        b"INPUT-SENTINEL-RESIZE-2",
        b"INPUT-SENTINEL-RESIZE-3",
    ]
    try:
        controller.resize(TerminalDimensions(rows=25, columns=81))
        assert client.first_resize_entered.wait(2.0)
        for index in range(1, frontend_resize_events):
            controller.resize(
                TerminalDimensions(
                    rows=25 + index,
                    columns=81 + index,
                )
            )
        for sentinel in sentinels:
            controller.send_input(sentinel)
        assert client.input_called.wait(2.0)
        deadline = time.monotonic() + 2.0
        while len(client.pty_input) < len(sentinels) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert client.rpc_order == [
            ("resize", TerminalDimensions(rows=25, columns=81)),
            *[("input", sentinel) for sentinel in sentinels],
        ]
        assert client.pty_input == sentinels
        client.release_first_resize.set()
        dispatcher.run_until(lambda: client.resize_rpc_count == 2)

        assert client.resize_rpc_count == 2
        assert client.last_resize == TerminalDimensions(
            rows=25 + frontend_resize_events - 1,
            columns=81 + frontend_resize_events - 1,
        )
    finally:
        client.release_first_resize.set()
        bridge.shutdown(wait=True)


def test_unrecoverable_terminal_replay_leaves_active_and_marks_view():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    client = _TerminalClient("client")
    client.replay_truncated = True
    output = []
    errors = []
    markers = []
    controller = _active_controller(
        client,
        bridge,
        on_output=output.append,
        on_error=errors.append,
        on_loss=lambda: markers.append(True),
    )
    binding = _bind_active_stream(controller, bridge, client)
    prefix = b"SAFE-PREFIX"
    try:
        assert client.emit_output(0, prefix)
        dispatcher.drain()
        client.emit_continuity(len(prefix), len(prefix) + 100)
        dispatcher.run_until(
            lambda: controller.state is TerminalSessionState.FAILED,
        )

        assert output == [prefix]
        assert markers == [True]
        assert [error.code for error in errors] == [
            ErrorCode.TERMINAL_REPLAY_UNAVAILABLE
        ]
        assert controller._stream is None
        assert controller.input_owner is False
    finally:
        binding.close()
        bridge.shutdown(wait=True)


def test_terminal_recovery_replays_multiple_bounded_chunks_in_order():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    client = _TerminalClient("client")
    prefix = b"PREFIX"
    first_replay = b"a" * (400 * 1024)
    second_replay = b"b" * (400 * 1024)
    client.recovery_frames = [
        (len(prefix), first_replay, True),
        (len(prefix) + len(first_replay), second_replay, True),
    ]
    output = []
    controller = _active_controller(client, bridge, on_output=output.append)
    binding = _bind_active_stream(controller, bridge, client)
    try:
        assert client.emit_output(0, prefix)
        dispatcher.drain()
        client.emit_continuity(len(prefix), len(prefix) + 1)
        dispatcher.run_until(
            lambda: controller.state is TerminalSessionState.ACTIVE
            and len(b"".join(output))
            == len(prefix + first_replay + second_replay),
        )

        assert b"".join(output) == prefix + first_replay + second_replay
        assert client.replay_rpc_count == 2
        assert controller.tab_state.expected_sequence == len(
            prefix + first_replay + second_replay
        )
    finally:
        binding.close()
        bridge.shutdown(wait=True)


@pytest.mark.parametrize("replay_bytes", [700 * 1024, 1300 * 1024])
def test_replay_request_waits_until_previous_chunk_reaches_frontend(
    replay_bytes,
    tmp_path,
):
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    prefix = b"SAFE-PREFIX"
    retained = bytes((index % 251 for index in range(replay_bytes)))
    during_recovery = b"LIVE-DURING-RECOVERY" * 2048
    after_recovery = b"LIVE-AFTER-BOUNDARY"
    client = _QueuedReplayClient(
        len(prefix),
        retained,
        tmp_path / "unused-replay-race.sock",
    )
    output = []
    controller = _active_controller(client, bridge, on_output=output.append)
    binding = _bind_active_stream(controller, bridge, client)

    def _normal_worker_barrier():
        reached = threading.Event()
        bridge.submit(
            reached.set,
            on_success=lambda _result: None,
            on_error=lambda error: pytest.fail(str(error)),
        )
        assert reached.wait(2.0)

    try:
        assert client.emit_output(0, prefix)
        dispatcher.drain()
        client.emit_continuity(len(prefix), len(prefix) + 1)
        dispatcher.run_one()
        client.wait_for_replay_requests(1)
        dispatcher.run_one()

        # If response metadata chains immediately, request #2 runs ahead of
        # this barrier and deletes request #1's deliberately stalled frames.
        _normal_worker_barrier()
        assert client.replay_rpc_count == 1

        client.append_during_recovery(during_recovery)
        total_expected = len(retained) + len(during_recovery)
        delivered = 0
        request_count = 1
        while delivered < total_expected:
            delivered += client.flush_queued_replay()
            dispatcher.drain()
            if delivered < total_expected:
                request_count += 1
                client.wait_for_replay_requests(request_count)
                dispatcher.run_one()

        dispatcher.run_until(
            lambda: controller.state is TerminalSessionState.ACTIVE,
        )
        assert client.emit_output(
            len(prefix) + total_expected,
            after_recovery,
        )
        dispatcher.drain()

        expected = prefix + retained + during_recovery + after_recovery
        assert b"".join(output) == expected
        assert client.deleted_queued_replay_bytes == 0
        assert controller.tab_state.expected_sequence == len(expected)
        assert request_count == (total_expected + 512 * 1024 - 1) // (512 * 1024)
    finally:
        binding.close()
        bridge.shutdown(wait=True)


def test_terminal_input_backpressure_does_not_silently_lose_keystroke():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    client = _TerminalClient("client")
    client.reject_input = True
    visible_failures = []
    observed_errors = []
    widget = SimpleNamespace(
        _on_connection_failed=lambda message: pytest.fail(
            f"input backpressure disconnected the session: {message}"
        ),
        _show_toast=lambda message, timeout=3: visible_failures.append(message),
    )

    def _frontend_error(error):
        observed_errors.append(error)
        TerminalWidget._on_daemon_error(widget, error)

    controller = _active_controller(client, bridge, on_error=_frontend_error)
    binding = _bind_active_stream(controller, bridge, client)
    sentinel = "INPUT-SENTINEL-7f93"
    input_widget = SimpleNamespace(
        _daemon_controller=controller,
        has_input_ownership=True,
    )
    try:
        TerminalWidget._on_daemon_commit(
            input_widget,
            None,
            sentinel,
            len(sentinel),
        )
        assert client.input_called.wait(2.0)
        dispatcher.drain()

        assert [error.code for error in observed_errors] == [
            ErrorCode.TERMINAL_INPUT_BACKPRESSURE
        ]
        assert client.pty_input == []
        assert len(visible_failures) == 1
        assert "not delivered" in visible_failures[0].lower()
        assert [item for item in client.rpc_order if item[0] == "input"] == [
            ("input", sentinel.encode())
        ]
    finally:
        binding.close()
        controller._stream = None
        bridge.shutdown(wait=True)


def test_frontend_input_queue_bound_reports_undelivered_input():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(
        dispatcher=dispatcher,
        max_workers=1,
        max_pending_terminal_inputs=1,
    )
    client = _TerminalClient("client")
    client.block_input = True
    errors = []
    controller = _active_controller(client, bridge, on_error=errors.append)
    try:
        controller.send_input(b"first")
        assert client.input_called.wait(2.0)
        controller.send_input(b"second")

        assert [error.code for error in errors] == [
            ErrorCode.TERMINAL_INPUT_BACKPRESSURE
        ]
        assert client.pty_input == []
    finally:
        client.release_input.set()
        bridge.shutdown(wait=True)


def test_active_terminal_rebind_restores_input_and_output():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    old_client = _TerminalClient("old")
    new_client = _TerminalClient("new")
    output = []
    controller = _active_controller(old_client, bridge, on_output=output.append)
    binding = _bind_active_stream(controller, bridge, old_client)
    first = b"HEARTBEAT-000001\r\n"
    second = b"HEARTBEAT-000002\r\n"
    new_client.recovery_frames = [(len(first), second, True)]
    sentinel = b"INPUT-AFTER-CLIENT-RECONNECT"
    terminal = SimpleNamespace(_daemon_controller=controller)
    try:
        assert old_client.emit_output(0, first)
        dispatcher.drain()
        old_callbacks = old_client.output_callbacks

        TerminalWidget.rebind_daemon_client(terminal, new_client)
        dispatcher.run_until(
            lambda: controller.state is TerminalSessionState.ACTIVE
            and second in output
        )
        controller.send_input(sentinel)
        assert new_client.input_called.wait(2.0)
        dispatcher.drain()

        assert controller._client is new_client
        assert new_client.pty_input == [sentinel]
        assert old_client.output_callbacks is not None
        assert old_client.output_callbacks.closed is True
        assert new_client.output_callbacks is not None
        assert new_client.output_callbacks.closed is False
        assert controller.state is TerminalSessionState.ACTIVE
        assert output == [first, second]
        assert new_client.replay_start == len(first)

        old_callbacks.on_output(
            TerminalOutput(
                session_id=_SESSION_ID,
                sequence=len(first) + len(second),
                data=b"STALE-OLD-CLIENT",
            )
        )
        dispatcher.drain()
        assert output == [first, second]
    finally:
        binding.close()
        controller._stream = None
        bridge.shutdown(wait=True)


def test_repeated_reconnect_uses_only_latest_output_binding():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    old_client = _TerminalClient("old")
    intermediate = _TerminalClient("intermediate")
    current = _TerminalClient("current")
    output = []
    controller = _active_controller(old_client, bridge, on_output=output.append)
    binding = _bind_active_stream(controller, bridge, old_client)
    prefix = b"PREFIX"
    wrong = b"INTERMEDIATE"
    right = b"CURRENT"
    intermediate.recovery_frames = [(len(prefix), wrong, True)]
    current.recovery_frames = [(len(prefix), right, True)]
    try:
        assert old_client.emit_output(0, prefix)
        dispatcher.drain()
        controller.set_client(intermediate)
        controller.set_client(current)
        dispatcher.run_until(
            lambda: controller.state is TerminalSessionState.ACTIVE
            and right in output
        )

        assert controller._client is current
        assert output == [prefix, right]
        assert current.output_callbacks.closed is False
        assert old_client.output_callbacks.closed is True
        assert intermediate.output_callbacks.closed is True
    finally:
        binding.close()
        controller.close()
        bridge.shutdown(wait=True)


def test_close_while_output_rebind_is_pending_suppresses_late_delivery():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    old_client = _TerminalClient("old")
    new_client = _TerminalClient("new")
    new_client.block_attach = True
    new_client.recovery_frames = [(0, b"LATE-REPLAY", True)]
    output = []
    controller = _active_controller(old_client, bridge, on_output=output.append)
    binding = _bind_active_stream(controller, bridge, old_client)
    try:
        controller.set_client(new_client)
        assert new_client.attach_called.wait(2.0)
        controller.close()
        new_client.release_attach.set()
        dispatcher.run_until(
            lambda: controller.state is TerminalSessionState.CLOSED,
        )
        dispatcher.drain()

        assert output == []
        assert controller._stream is None
    finally:
        new_client.release_attach.set()
        binding.close()
        bridge.shutdown(wait=True)
