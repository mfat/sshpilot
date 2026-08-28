"""Regression evidence for stateful terminal output crossing the GTK handoff."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

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
    def subscribe_terminal(self, _session_id, on_output, **callbacks):
        self.on_output = on_output
        self.on_continuity_lost = callbacks.get("on_continuity_lost")
        return TerminalSubscription(lambda: None)


def _binding(
    *,
    max_pending_bytes,
    on_output,
    on_continuity_lost=None,
    max_spool_bytes=None,
    session_id=SessionId("session-dialog"),
):
    dispatches = []
    client = _TerminalClient()
    options = {}
    if max_spool_bytes is not None:
        options["max_spool_bytes"] = max_spool_bytes
    binding = GtkTerminalBinding(
        client,
        session_id,
        dispatcher=dispatches.append,
        on_output=on_output,
        on_continuity_lost=on_continuity_lost,
        on_eof=None,
        on_error=None,
        max_pending_bytes=max_pending_bytes,
        on_close=lambda _binding: None,
        **options,
    )
    return client, binding, dispatches


def _drain_all(dispatches):
    assert len(dispatches) == 1
    callback = dispatches.pop()
    while callback():
        pass


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
        _drain_all(dispatches)
    finally:
        binding.close()

    expected = b"".join(chunks)
    expected_hash = hashlib.sha256(expected).hexdigest()
    for boundary, observed in traces.items():
        joined = b"".join(observed)
        assert joined == expected, boundary
        assert hashlib.sha256(joined).hexdigest() == expected_hash, boundary


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
        _drain_all(dispatches)
    finally:
        binding.close()

    assert losses == []
    assert b"".join(delivered) == b"".join(chunks)


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
        _drain_all(dispatches)
    finally:
        binding.close()

    # Current dev supplies b"2J..." as the first post-loss bytes.  VTE sees
    # printable text, not the intended CSI 2 J erase-display operation.
    assert b"".join(delivered) == b"".join(chunks)


def test_more_than_one_mibibyte_eventually_drains_byte_for_byte_in_order():
    delivered = []
    client, binding, dispatches = _binding(
        max_pending_bytes=32 * 1024,
        on_output=lambda output: delivered.append(
            (output.sequence, output.data, output.replay)
        ),
    )
    chunks = tuple(
        (f"output-line-{index:08d} ".encode() + b"x" * 1000 + b"\r\n")
        for index in range(1200)
    ) + (b"\x1b[?1049h\x1b[2JPOST-FLOOD DIALOG<Yes><No>",)
    try:
        sequence = 0
        for chunk in chunks:
            client.on_output(_output(sequence, chunk))
            sequence += len(chunk)
        _drain_all(dispatches)
    finally:
        binding.close()

    assert binding.high_water_mark > 1024 * 1024
    assert binding.pending_bytes == 0
    assert binding.last_received_sequence == sum(map(len, chunks))
    assert binding.last_delivered_sequence == sum(map(len, chunks))
    assert [item[0] for item in delivered] == [
        sum(map(len, chunks[:index])) for index in range(len(chunks))
    ]
    assert b"".join(item[1] for item in delivered) == b"".join(chunks)


def test_chunk_boundaries_preserve_osc_utf8_and_replay_live_order():
    delivered = []
    client, binding, dispatches = _binding(
        max_pending_bytes=7,
        on_output=lambda output: delivered.append(output),
    )
    chunks = (
        (b"\x1b]0;", True),
        (b"a" * 257, True),
        (b"\x07utf8:\xe2", False),
        (b"\x82", False),
        (b"\xac\r\n", False),
    )
    try:
        sequence = 0
        for data, replay in chunks:
            output = _output(sequence, data)
            client.on_output(
                TerminalOutput(
                    session_id=output.session_id,
                    sequence=output.sequence,
                    data=output.data,
                    replay=replay,
                )
            )
            sequence += len(data)
        _drain_all(dispatches)
    finally:
        binding.close()

    assert [(item.data, item.replay) for item in delivered] == list(chunks)


def test_hard_spool_limit_fails_closed_instead_of_resuming_later_bytes():
    delivered = []
    losses = []
    client, binding, dispatches = _binding(
        max_pending_bytes=8,
        max_spool_bytes=96,
        on_output=lambda output: delivered.append(output.data),
        on_continuity_lost=lambda *loss: losses.append(loss),
    )
    chunks = (b"known-prefix", b"x" * 80, b"2J-arbitrary-suffix")
    try:
        sequence = 0
        for chunk in chunks:
            client.on_output(_output(sequence, chunk))
            sequence += len(chunk)
        _drain_all(dispatches)
    finally:
        binding.close()

    assert delivered == [b"known-prefix"]
    assert losses == [(SessionId("session-dialog"), 12, 92)]
    assert binding.continuity_lost is True


def test_upstream_continuity_loss_drains_prefix_then_suppresses_live_suffix():
    delivered = []
    events = []
    client, binding, dispatches = _binding(
        max_pending_bytes=8,
        on_output=lambda output: delivered.append(output.data),
        on_continuity_lost=lambda *loss: events.append(("loss", loss)),
    )
    try:
        client.on_output(_output(0, b"coherent-prefix"))
        client.on_continuity_lost(SessionId("session-dialog"), 15, 40)
        client.on_output(_output(40, b"must-not-reach-vte"))
        _drain_all(dispatches)
    finally:
        binding.close()

    assert delivered == [b"coherent-prefix"]
    assert events == [("loss", (SessionId("session-dialog"), 15, 40))]


def test_close_during_pressure_suppresses_late_gtk_delivery():
    delivered = []
    client, binding, dispatches = _binding(
        max_pending_bytes=8,
        on_output=lambda output: delivered.append(output.data),
    )
    client.on_output(_output(0, b"queued-before-close" * 100))
    callback = dispatches.pop()
    binding.close()

    assert callback() is False
    assert delivered == []


def test_flooding_terminal_does_not_delay_another_terminal_gtk_drain():
    flood_delivered = []
    quiet_delivered = []
    flood_client, flood, flood_dispatches = _binding(
        session_id=SessionId("session-flood"),
        max_pending_bytes=1024,
        on_output=lambda output: flood_delivered.append(output.data),
    )
    quiet_client, quiet, quiet_dispatches = _binding(
        session_id=SessionId("session-quiet"),
        max_pending_bytes=1024,
        on_output=lambda output: quiet_delivered.append(output.data),
    )
    try:
        sequence = 0
        for _index in range(2048):
            data = b"f" * 1024
            flood_client.on_output(
                TerminalOutput(SessionId("session-flood"), sequence, data)
            )
            sequence += len(data)
        quiet_client.on_output(
            TerminalOutput(SessionId("session-quiet"), 0, b"prompt$ ")
        )

        _drain_all(quiet_dispatches)
        assert quiet_delivered == [b"prompt$ "]
        assert flood.pending_bytes == 2 * 1024 * 1024
        _drain_all(flood_dispatches)
    finally:
        flood.close()
        quiet.close()

    assert len(flood_delivered) == 2048


def test_fixed_size_spool_wraps_without_changing_terminal_bytes():
    delivered = []
    client, binding, dispatches = _binding(
        max_pending_bytes=20,
        max_spool_bytes=128,
        on_output=lambda output: delivered.append(output.data),
    )
    chunks = (b"a" * 20, b"b" * 20, b"\x1b[2J" + b"c" * 16)
    try:
        client.on_output(_output(0, chunks[0]))
        client.on_output(_output(20, chunks[1]))
        callback = dispatches.pop()
        assert callback() is True
        client.on_output(_output(40, chunks[2]))
        while callback():
            pass
    finally:
        binding.close()

    assert delivered == list(chunks)


def test_input_and_resize_remain_immediate_while_output_waits_for_gtk():
    sent = []
    resized = []
    client, binding, dispatches = _binding(
        max_pending_bytes=1024,
        on_output=lambda _output: None,
    )
    widget = SimpleNamespace(
        _daemon_controller=SimpleNamespace(
            send_input=sent.append,
            resize=resized.append,
        ),
        has_input_ownership=True,
        backend=SimpleNamespace(get_size=lambda: (37, 119)),
    )
    widget._daemon_terminal_dimensions = lambda: (
        TerminalWidget._daemon_terminal_dimensions(widget)
    )
    try:
        for index in range(2048):
            client.on_output(_output(index * 1024, b"x" * 1024))
        TerminalWidget._on_daemon_commit(widget, None, "\x1bOA\t\r", 5)
        TerminalWidget._on_daemon_size_changed(widget, None, 0, 0)
        assert sent == [b"\x1bOA\t\r"]
        assert [(item.rows, item.columns) for item in resized] == [(37, 119)]
        _drain_all(dispatches)
    finally:
        binding.close()


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


def test_dialog_x10_mouse_commit_keeps_high_bytes():
    sent = []
    widget = SimpleNamespace(
        _daemon_controller=SimpleNamespace(send_input=sent.append),
        has_input_ownership=True,
    )
    # X10 mouse: CSI M + button + x + y. Column 200 is byte 232.
    text = "\x1b[M" + chr(32) + chr(232) + chr(40)
    TerminalWidget._on_daemon_commit(widget, None, text, 6)
    assert sent == [text.encode("latin-1")]
    assert sent[0][4] == 232
