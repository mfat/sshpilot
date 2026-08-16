"""Regression tests for GH #1164 (tmux not using the whole displayed area).

Two bugs compounded to produce the reported symptom:

1. Daemon terminal resize was wired to VTE's "char-size-changed" signal,
   which only fires on font-metric changes (zoom), never on a widget
   resize — fixed in terminal_backends.py / daemon_terminal_widget.py.
2. Even with the right signal, DaemonTerminalSessionController.resize()
   requires input ownership, and ownership is granted as part of the same
   attach result that flips session state to ACTIVE. Any resize signal
   that fired earlier — while the widget was growing to its real on-screen
   size but ownership was still pending — was silently dropped, with
   nothing to catch up once ownership existed. This file covers that
   second bug: the daemon terminal must sync its actual current size once,
   the moment it transitions to ACTIVE with ownership.
"""

from unittest.mock import Mock

import pytest

pytest.importorskip("gi")

from sshpilot.api.models.terminal import TerminalDimensions
from sshpilot.connection_model import ConnectionState
from sshpilot.terminal_session_controller import TerminalSessionState


def _terminal(*, is_connected: bool, has_input_ownership: bool, state):
    from sshpilot.terminal import TerminalWidget

    terminal = Mock()
    terminal._daemon_mode = True
    terminal._pending_shell_ready_feeds = []
    terminal._daemon_interaction_dialogs = None
    terminal.is_connected = is_connected
    terminal.connection_state = ConnectionState.CONNECTING
    terminal.has_input_ownership = has_input_ownership
    terminal._daemon_running_gate_active = Mock(return_value=False)

    controller = Mock()
    controller.state = state
    tab_state = Mock()
    tab_state.session_id = "sess-1"
    controller.tab_state = tab_state
    terminal._daemon_controller = controller

    terminal._daemon_terminal_dimensions = Mock(
        return_value=TerminalDimensions(rows=50, columns=200)
    )
    terminal.backend = Mock()

    # Bind the real methods under test to the mock instance.
    terminal._update_daemon_connection_state = (
        lambda: TerminalWidget._update_daemon_connection_state(terminal)
    )
    terminal._resync_daemon_terminal_size = (
        lambda: TerminalWidget._resync_daemon_terminal_size(terminal)
    )
    return terminal, controller


def test_first_active_transition_with_ownership_syncs_daemon_size():
    """The daemon terminal's first ACTIVE+owned state must push its actual
    current dimensions — closing the gap left by every resize signal that
    was dropped while ownership was still pending."""
    terminal, controller = _terminal(
        is_connected=False,
        has_input_ownership=True,
        state=TerminalSessionState.ACTIVE,
    )

    terminal._update_daemon_connection_state()

    controller.resize.assert_called_once_with(
        TerminalDimensions(rows=50, columns=200)
    )
    # A single synchronous read can still race GTK's layout pass; the
    # backend's tick-poll cache must also be invalidated so a stale read
    # here gets self-corrected on the very next frame regardless.
    terminal.backend.invalidate_size_tracking.assert_called_once_with()


def test_repeated_active_notifications_do_not_resync_every_time():
    """Once already connected, further ACTIVE notifications (unrelated
    state pings) must not keep re-sending resize RPCs."""
    terminal, controller = _terminal(
        is_connected=True,
        has_input_ownership=True,
        state=TerminalSessionState.ACTIVE,
    )

    terminal._update_daemon_connection_state()

    controller.resize.assert_not_called()


def test_view_only_active_transition_does_not_resize():
    """A view-only attachment (no input ownership) must never attempt to
    resize — DaemonTerminalSessionController.resize() would drop it anyway,
    and only the owning client should control the shared PTY size."""
    terminal, controller = _terminal(
        is_connected=False,
        has_input_ownership=False,
        state=TerminalSessionState.ACTIVE,
    )

    terminal._update_daemon_connection_state()

    controller.resize.assert_not_called()


def test_take_input_control_syncs_daemon_size_once_claimed():
    """Claiming input control on a previously view-only attachment must
    also sync the daemon to the terminal's actual current size — the same
    "resize dropped while unowned" gap applies here too."""
    from sshpilot.terminal import TerminalWidget
    from sshpilot.api.models.terminal import ClaimTerminalInputRequest

    terminal = Mock()
    terminal._daemon_mode = True

    controller = Mock()
    tab_state = Mock()
    tab_state.session_id = "sess-1"
    tab_state.attachment_id = "att-1"
    tab_state.input_owner = False
    controller._tab_state = tab_state
    controller.tab_state = tab_state
    controller._client = Mock()
    bridge = Mock()
    controller._bridge = bridge
    terminal._daemon_controller = controller
    terminal._hide_view_only_indicator = Mock()
    terminal._daemon_terminal_dimensions = Mock(
        return_value=TerminalDimensions(rows=50, columns=200)
    )
    terminal.backend = Mock()
    terminal._resync_daemon_terminal_size = (
        lambda: TerminalWidget._resync_daemon_terminal_size(terminal)
    )

    TerminalWidget.take_input_control(terminal)

    assert bridge.submit.call_count == 1
    _op, kwargs = bridge.submit.call_args[0], bridge.submit.call_args[1]
    on_success = kwargs["on_success"]
    on_success(Mock())

    assert tab_state.input_owner is True
    controller.resize.assert_called_once_with(
        TerminalDimensions(rows=50, columns=200)
    )
    terminal.backend.invalidate_size_tracking.assert_called_once_with()
