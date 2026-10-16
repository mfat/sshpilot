"""Real GLib: the bridge's default dispatcher against a busy main loop."""

from __future__ import annotations

import time

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


def test_worker_result_wakes_a_loop_blocked_in_poll():
    """A result from the worker must wake a main loop that is asleep.

    The tests above pump with ``iteration(False)`` and never sleep in poll, so
    they cannot tell a priority problem from a missing context wakeup. Here
    the loop blocks in ``iteration(True)`` before the worker finishes; only
    ``g_main_context_wakeup`` from ``idle_add`` can end that wait before the
    guard timeout does.
    """
    timed_out = []

    def _guard():
        timed_out.append(True)
        return False

    guard = GLib.timeout_add(2000, _guard)
    bridge = GtkClientBridge()
    results = []
    started = time.monotonic()
    try:
        bridge.submit(
            lambda: time.sleep(0.2) or "opened",
            on_success=results.append,
            on_error=results.append,
        )
        context = GLib.MainContext.default()
        while not results and not timed_out:
            context.iteration(True)
    finally:
        if not timed_out:
            GLib.source_remove(guard)
        bridge.shutdown()

    assert results == ["opened"]
    assert not timed_out
    assert time.monotonic() - started < 1.0


def test_terminal_output_beyond_one_slice_is_drained():
    """The drain returns True to run again while more than a slice is spooled."""
    client = _TerminalClient()
    delivered = []
    bridge = GtkClientBridge()
    session_id = SessionId("session-1")
    total = 3 * DEFAULT_GTK_TERMINAL_PENDING_BYTES + 123
    try:
        bridge.bind_terminal(
            client,
            session_id,
            on_output=lambda output: delivered.append(len(output.data)),
        )
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
