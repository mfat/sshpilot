"""Real GLib: the bridge's default dispatcher against a busy main loop."""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

_Gtk, _Adw, _Gio, GLib = requires_gui()

from sshpilot.api.models.common import SessionId  # noqa: E402
from sshpilot.api.models.terminal import TerminalOutput  # noqa: E402
from sshpilot.api.terminal_events import TerminalSubscription  # noqa: E402
from sshpilot.gtk_client_bridge import (  # noqa: E402
    DEFAULT_GTK_TERMINAL_PENDING_BYTES,
    GtkClientBridge,
)

pytestmark = pytest.mark.gui


def _pump_until(predicate, seconds=2):
    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + int(seconds * 1_000_000)
    while not predicate() and GLib.get_monotonic_time() < deadline:
        context.iteration(False)
    return predicate()


class _TerminalClient:
    def subscribe_terminal(self, session_id, on_output, **_callbacks):
        self.on_output = on_output
        return TerminalSubscription(lambda: None)


def test_results_arrive_while_redraw_is_always_ready():
    """A frame clock that never goes idle must not hold daemon results back.

    With the Cairo renderer a large window repaints slower than the frame
    interval, so while the connecting spinner animates GDK's redraw source
    (``PRIORITY_HIGH_IDLE + 20``) is always ready. Results queued at
    ``PRIORITY_DEFAULT_IDLE`` never ran, and the spinner waited on the very
    result that would have stopped it.
    """
    redraw = GLib.idle_add(lambda: True, priority=GLib.PRIORITY_HIGH_IDLE + 20)
    bridge = GtkClientBridge()
    results = []
    try:
        bridge.submit(
            lambda: "opened",
            on_success=results.append,
            on_error=results.append,
        )
        assert _pump_until(lambda: results)
    finally:
        GLib.source_remove(redraw)
        bridge.shutdown()

    assert results == ["opened"]


def test_terminal_output_beyond_one_slice_is_drained():
    """The drain returns True to run again while more than a slice is spooled."""
    client = _TerminalClient()
    delivered = []
    bridge = GtkClientBridge()
    session_id = SessionId("session-1")
    total = 3 * DEFAULT_GTK_TERMINAL_PENDING_BYTES + 123
    bridge.bind_terminal(
        client,
        session_id,
        on_output=lambda output: delivered.append(len(output.data)),
    )
    try:
        sequence = 0
        while sequence < total:
            data = b"x" * min(16 * 1024, total - sequence)
            client.on_output(
                TerminalOutput(session_id=session_id, sequence=sequence, data=data)
            )
            sequence += len(data)
        assert _pump_until(lambda: sum(delivered) == total)
    finally:
        bridge.shutdown()
