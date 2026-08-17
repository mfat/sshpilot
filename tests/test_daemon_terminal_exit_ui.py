"""Test daemon terminal tab reaction to session exit (issue #1147).

When the daemon session ends underneath the tab — the remote host reboots,
the connection drops, or the user types exit — the tab must either show the
reconnect banner (unexpected exit) or close (clean exit), mirroring the
legacy child-exit path.
"""

from unittest.mock import Mock

import pytest

from sshpilot.api.models.sessions import SessionExitInfo
from sshpilot.connection_model import ConnectionState
from sshpilot.gtk.connection_store import ConnectionPresentationStore
from sshpilot.terminal_session_controller import TerminalSessionState


@pytest.fixture
def mock_terminal_widget():
    """Mock terminal widget in daemon mode with a live session."""
    from sshpilot.terminal import TerminalWidget

    terminal = Mock()
    terminal._daemon_mode = True
    terminal._daemon_exit_handled = False
    terminal._daemon_interaction_dialogs = None

    controller = Mock()
    controller.state = TerminalSessionState.CLOSED
    controller.exit_info = None
    terminal._daemon_controller = controller

    tab_state = Mock()
    tab_state.session_id = 'test-session-123'
    terminal._daemon_tab_state = tab_state

    terminal.connection = Mock()
    terminal.connection.protocol = 'ssh'
    terminal.connection_state = ConnectionState.CONNECTED
    terminal.is_connected = True
    terminal.last_error_message = None
    terminal._connect_failure_hint = ''
    terminal._used_stored_password = False
    terminal.has_input_ownership = True
    terminal.emit = Mock()
    # Daemon terminals receive an immutable presentation store. Runtime
    # connection status is projected from daemon session events separately.
    terminal.connection_manager = ConnectionPresentationStore()

    # Bind the real methods under test to the mock instance.
    terminal._classify_exit = (
        lambda code, was_connected, extra='': TerminalWidget._classify_exit(
            terminal, code, was_connected, extra
        )
    )
    terminal._handle_daemon_session_exit = (
        lambda was_connected: TerminalWidget._handle_daemon_session_exit(
            terminal, was_connected
        )
    )

    root = Mock()
    page = Mock()
    root._page_for_child = Mock(return_value=page)
    terminal.get_root = Mock(return_value=root)
    terminal._root = root
    terminal._page = page
    return terminal


def _update(terminal):
    from sshpilot.terminal import TerminalWidget

    TerminalWidget._update_daemon_connection_state(terminal)


def test_reboot_exit_shows_reconnect_banner(mock_terminal_widget):
    """ssh exiting 255 after a reboot shows the banner and keeps the tab."""
    terminal = mock_terminal_widget
    terminal._daemon_controller.exit_info = SessionExitInfo(
        exit_code=255, reason="process_exit"
    )

    _update(terminal)

    terminal._set_disconnected_banner_visible.assert_called_once()
    args = terminal._set_disconnected_banner_visible.call_args[0]
    assert args[0] is True
    assert args[1] == 'Connection lost'
    terminal._root.tab_view.close_page.assert_not_called()
    assert terminal.is_connected is False
    assert terminal.connection_state == ConnectionState.DISCONNECTED


def test_clean_exit_closes_tab_without_banner(mock_terminal_widget):
    """Typing exit (exit code 0) closes the tab, no banner."""
    terminal = mock_terminal_widget
    terminal._daemon_controller.exit_info = SessionExitInfo(
        exit_code=0, reason="process_exit"
    )

    _update(terminal)

    terminal._root.tab_view.close_page.assert_called_once_with(terminal._page)
    terminal._set_disconnected_banner_visible.assert_not_called()


def test_session_end_without_exit_info_shows_generic_banner(mock_terminal_widget):
    """A CLOSED session with no exit details still shows the banner."""
    terminal = mock_terminal_widget
    terminal._daemon_controller.exit_info = None

    _update(terminal)

    terminal._set_disconnected_banner_visible.assert_called_once()
    args = terminal._set_disconnected_banner_visible.call_args[0]
    assert args[0] is True
    assert args[1] == 'Session ended.'
    terminal._root.tab_view.close_page.assert_not_called()


def test_clean_exit_code_after_recorded_failure_still_shows_banner(mock_terminal_widget):
    """exit_code=0 alone isn't proof of a clean exit: a session that already
    went through FAILED (session_runtime classified it as a real failure —
    see GH #1166, where a spawn bug made ssh exit "cleanly" without ever
    starting) records last_error_message via _on_connection_failed before
    the final CLOSED transition arrives here. The tab must not silently
    swallow that banner just because the raw exit code happens to be 0."""
    terminal = mock_terminal_widget
    terminal.last_error_message = "The session ended before it produced any output"
    terminal._daemon_controller.exit_info = SessionExitInfo(
        exit_code=0, reason="process_exit"
    )

    _update(terminal)

    terminal._root.tab_view.close_page.assert_not_called()
    terminal._set_disconnected_banner_visible.assert_called_once()
    args = terminal._set_disconnected_banner_visible.call_args[0]
    assert args[0] is True
    assert args[1] == "The session ended before it produced any output"


def test_exit_handling_is_idempotent(mock_terminal_widget):
    """Repeated state updates must not double-close or re-banner the tab."""
    terminal = mock_terminal_widget
    terminal._daemon_controller.exit_info = SessionExitInfo(
        exit_code=0, reason="process_exit"
    )

    _update(terminal)
    _update(terminal)

    terminal._root.tab_view.close_page.assert_called_once_with(terminal._page)
