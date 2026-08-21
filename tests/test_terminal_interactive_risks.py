"""Focused contracts for the remaining daemon-terminal interaction risks."""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import AttachmentId, ConnectionId, SessionId
from sshpilot.api.models.terminal import TerminalDimensions, TerminalOutput
from sshpilot.api.terminal_events import TerminalSubscription
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
        self.resize_rpc_count = 0
        self.replay_rpc_count = 0

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

    def emit_output(self, sequence: int, data: bytes) -> bool:
        callbacks = self.output_callbacks
        if callbacks is None or callbacks.closed:
            return False
        callbacks.on_output(
            TerminalOutput(
                session_id=_SESSION_ID,
                sequence=sequence,
                data=data,
            )
        )
        return True

    def emit_continuity(self, expected: int, available: int) -> None:
        callbacks = self.output_callbacks
        assert callbacks is not None and not callbacks.closed
        callbacks.on_continuity_lost(_SESSION_ID, expected, available)

    def send_terminal_input(self, request) -> None:
        self.rpc_order.append(("input", request.data))
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
        self.input_called.set()

    def resize_terminal(self, request) -> None:
        self.resize_rpc_count += 1
        call_number = self.resize_rpc_count
        self.rpc_order.append(("resize", request.dimensions))
        if self.block_first_resize and call_number == 1:
            self.first_resize_entered.set()
            assert self.release_first_resize.wait(2.0)

    def replay_terminal(self, _request):
        self.replay_rpc_count += 1


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
    binding = bridge.bind_terminal(
        client,
        _SESSION_ID,
        on_output=controller._handle_output,
        on_continuity_lost=controller._handle_continuity_lost,
        on_error=controller._on_error,
    )
    controller._stream = binding
    return binding


@pytest.mark.xfail(
    strict=True,
    reason="continuity loss leaves an ACTIVE binding permanently suppressing output",
)
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
    later = b"HEARTBEAT-0002\r\n"
    token = "INPUT-AFTER-CONTINUITY-LOSS"
    input_widget = SimpleNamespace(
        _daemon_controller=controller,
        has_input_ownership=True,
    )
    try:
        assert client.emit_output(0, first)
        dispatcher.drain()
        client.emit_continuity(len(first), len(first) + 64)
        assert client.emit_output(len(first) + 64, later)
        dispatcher.drain()

        TerminalWidget._on_daemon_commit(
            input_widget,
            None,
            token,
            len(token),
        )
        assert client.input_called.wait(2.0)

        assert output == [first]
        assert client.pty_input == [token.encode()]
        assert binding.continuity_lost is True
        assert controller.state is TerminalSessionState.ACTIVE
        assert resets == [(False, True)]
        assert len(display) == 1
        assert b"no longer available" in display[0]
        assert client.replay_rpc_count == 0

        # Contract: the view must recover, or its state must explicitly stop
        # claiming to be a usable active terminal.
        assert later in output or controller.state is not TerminalSessionState.ACTIVE
    finally:
        binding.close()
        controller._stream = None
        bridge.shutdown(wait=True)


@pytest.mark.xfail(
    strict=True,
    reason="resize and input RPCs share one FIFO worker without resize coalescing",
)
def test_terminal_input_not_blocked_by_resize_backlog():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    client = _TerminalClient("client")
    client.block_first_resize = True
    controller = _active_controller(client, bridge)
    frontend_resize_events = 24
    sentinel = b"INPUT-SENTINEL-RESIZE"
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
        controller.send_input(sentinel)
        client.release_first_resize.set()
        assert client.input_called.wait(2.0)

        input_index = client.rpc_order.index(("input", sentinel))
        assert client.resize_rpc_count == frontend_resize_events
        assert input_index == frontend_resize_events

        # Coalescing fewer RPCs or prioritising input ahead of stale geometry
        # would satisfy the interactive contract. Current production does
        # neither, placing input after every historical resize.
        assert (
            client.resize_rpc_count < frontend_resize_events
            or input_index < client.resize_rpc_count
        )
    finally:
        client.release_first_resize.set()
        bridge.shutdown(wait=True)


@pytest.mark.xfail(
    strict=True,
    reason="terminal input backpressure is neither retried nor shown as a delivery failure",
)
def test_terminal_input_backpressure_does_not_silently_lose_keystroke():
    dispatcher = _Dispatcher()
    bridge = GtkClientBridge(dispatcher=dispatcher, max_workers=1)
    client = _TerminalClient("client")
    client.reject_input = True
    visible_failures = []
    observed_errors = []
    widget = SimpleNamespace(_on_connection_failed=visible_failures.append)

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
        assert visible_failures == []
        assert [item for item in client.rpc_order if item[0] == "input"] == [
            ("input", sentinel.encode())
        ]

        assert client.pty_input == [sentinel.encode()] or visible_failures
    finally:
        binding.close()
        controller._stream = None
        bridge.shutdown(wait=True)


@pytest.mark.xfail(
    strict=True,
    reason="active reconnect updates the client pointer but leaves output subscribed to the old client",
)
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
    sentinel = b"INPUT-AFTER-CLIENT-RECONNECT"
    terminal = SimpleNamespace(_daemon_controller=controller)
    try:
        assert old_client.emit_output(0, first)
        dispatcher.drain()

        TerminalWidget.rebind_daemon_client(terminal, new_client)
        controller.send_input(sentinel)
        assert new_client.input_called.wait(2.0)
        new_client_output_had_subscriber = new_client.emit_output(len(first), second)
        dispatcher.drain()

        assert controller._client is new_client
        assert new_client.pty_input == [sentinel]
        assert old_client.output_callbacks is not None
        assert old_client.output_callbacks.closed is False
        assert new_client_output_had_subscriber is False
        assert controller.state is TerminalSessionState.ACTIVE

        assert second in output or controller.state is not TerminalSessionState.ACTIVE
    finally:
        binding.close()
        controller._stream = None
        bridge.shutdown(wait=True)
