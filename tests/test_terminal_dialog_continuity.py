"""Regression evidence for stateful terminal output crossing the GTK handoff."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.terminal import TerminalOutput
from sshpilot.api.terminal_events import TerminalSubscription
from sshpilot.gtk_client_bridge import GtkTerminalBinding
from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_backends import VTETerminalBackend
from sshpilot.terminal_session_controller import (
    DaemonTerminalSessionController,
    DaemonTerminalTabState,
    TerminalSessionState,
)


class _TerminalClient:
    def subscribe_terminal(self, _session_id, on_output, **_callbacks):
        self.on_output = on_output
        return TerminalSubscription(lambda: None)


def _binding(*, max_pending_bytes, on_output, on_continuity_lost=None):
    dispatches = []
    client = _TerminalClient()
    binding = GtkTerminalBinding(
        client,
        SessionId("session-dialog"),
        dispatcher=dispatches.append,
        on_output=on_output,
        on_continuity_lost=on_continuity_lost,
        on_eof=None,
        on_error=None,
        max_pending_bytes=max_pending_bytes,
        on_close=lambda _binding: None,
    )
    return client, binding, dispatches


def _output(sequence: int, data: bytes) -> TerminalOutput:
    return TerminalOutput(
        session_id=SessionId("session-dialog"),
        sequence=sequence,
        data=data,
    )


def test_normal_dialog_bytes_remain_identical_at_every_frontend_boundary():
    """No-pressure path is byte-for-byte and sequence ordered through VTE.feed."""
    chunks = (
        b"\x1b[?1049h\x1b[2J",
        b"\x1b[8;16HCan you see both buttons?",
        b"\x1b[15;29H\x1b[37;41m<Yes>\x1b[30;47m  <No>",
    )
    traces = {name: [] for name in ("pty", "published", "binding", "controller", "widget", "vte")}

    class _Vte:
        def feed(self, data):
            traces["vte"].append(data)

    backend = SimpleNamespace(vte=_Vte())
    widget = SimpleNamespace(
        _feed_display=lambda data: (
            traces["widget"].append(data),
            VTETerminalBackend.feed(backend, data),
        ),
        _shell_output_seen=True,
        _daemon_running_gate_active=lambda: False,
        _update_daemon_connection_state=lambda: None,
    )
    controller = object.__new__(DaemonTerminalSessionController)
    controller._closed = False
    controller._tab_state = DaemonTerminalTabState(
        view_id="view-dialog",
        session_id=SessionId("session-dialog"),
        attachment_id=None,
        connection_id=ConnectionId("connection-dialog"),
        daemon_instance_id="daemon-dialog",
        state=TerminalSessionState.ACTIVE,
    )
    controller._replay_catchup_target = None
    controller._notify_state_changed = lambda: None

    def widget_output(data):
        TerminalWidget._on_daemon_output(widget, data)

    def controller_output(output):
        traces["controller"].append(output.data)
        controller._on_output = widget_output
        DaemonTerminalSessionController._handle_output(controller, output)

    client, binding, dispatches = _binding(
        max_pending_bytes=4096,
        on_output=lambda output: (
            traces["binding"].append(output.data),
            controller_output(output),
        ),
    )
    try:
        sequence = 0
        for chunk in chunks:
            traces["pty"].append(chunk)
            traces["published"].append(chunk)
            client.on_output(_output(sequence, chunk))
            sequence += len(chunk)
        assert len(dispatches) == 1
        dispatches.pop()()
    finally:
        binding.close()

    expected = b"".join(chunks)
    expected_hash = hashlib.sha256(expected).hexdigest()
    for boundary, observed in traces.items():
        joined = b"".join(observed)
        assert joined == expected, boundary
        assert hashlib.sha256(joined).hexdigest() == expected_hash, boundary


@pytest.mark.xfail(
    strict=True,
    reason="current GtkTerminalBinding discards queued terminal bytes on overflow",
)
def test_large_output_followed_by_dialog_remains_a_contiguous_terminal_stream():
    """Regression contract: a GTK stall must not remove the dialog state transition."""
    delivered = []
    losses = []
    client, binding, dispatches = _binding(
        max_pending_bytes=64,
        on_output=lambda output: delivered.append(output.data),
        on_continuity_lost=lambda *loss: losses.append(loss),
    )
    chunks = (
        b"output-line-00000001\r\n" * 2,
        b"output-line-00000003\r\n",
        b"\x1b[?1049h\x1b[2J\x1b[8;16HPOST-FLOOD DIALOG",
        b"\x1b[15;29H<Yes>   <No>",
    )
    try:
        sequence = 0
        for chunk in chunks:
            client.on_output(_output(sequence, chunk))
            sequence += len(chunk)
        dispatches.pop()()
    finally:
        binding.close()

    assert losses == []
    assert b"".join(delivered) == b"".join(chunks)


@pytest.mark.xfail(
    strict=True,
    reason="overflow can resume delivery in the middle of a terminal control sequence",
)
def test_overflow_cannot_resume_in_the_middle_of_a_control_sequence():
    """A split CSI demonstrates the first invalid byte supplied after continuity loss."""
    delivered = []
    client, binding, dispatches = _binding(
        max_pending_bytes=64,
        on_output=lambda output: delivered.append(output.data),
    )
    chunks = (
        b"x" * 63,
        b"\x1b[",
        b"2J\x1b[8;16HPOST-FLOOD DIALOG",
    )
    try:
        sequence = 0
        for chunk in chunks:
            client.on_output(_output(sequence, chunk))
            sequence += len(chunk)
        dispatches.pop()()
    finally:
        binding.close()

    # Current dev supplies b"2J..." as the first post-loss bytes.  VTE sees
    # printable text, not the intended CSI 2 J erase-display operation.
    assert b"".join(delivered) == b"".join(chunks)


def test_dialog_input_bytes_still_reach_controller_after_output_continuity_loss():
    sent = []
    widget = SimpleNamespace(
        _daemon_controller=SimpleNamespace(send_input=sent.append),
        has_input_ownership=True,
    )
    # ncurses/dialog enables application-cursor mode: VTE commits SS3 arrows.
    for committed in ("\x1bOA", "\t", "\x1b[Z", " ", "\r", "\x1b", "\x03"):
        TerminalWidget._on_daemon_commit(widget, None, committed, len(committed))

    assert sent == [
        b"\x1bOA",
        b"\t",
        b"\x1b[Z",
        b" ",
        b"\r",
        b"\x1b",
        b"\x03",
    ]
