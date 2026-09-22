"""A local shell tab must close whatever status the shell exits with.

Typing ``exit`` in bash after a Ctrl+C returns the last command's status (130),
not 0. The child-exit handler only closed the tab on a 0 exit, so the local
terminal instead kept the tab open behind a red "SSH exited with status 130"
banner — an ssh error for a session that never ran ssh.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("gi")

from sshpilot.connection_model import ConnectionState
from sshpilot.terminal import TerminalWidget


class _Store:
    """ConnectionPresentationStore stand-in: read-only, no state pushes."""

    def get_connections(self):
        return []


def _local_connection():
    # Mirrors terminal_manager's LocalConnection: no ``protocol`` attribute.
    return SimpleNamespace(
        nickname="Terminal",
        hostname="localhost",
        host="localhost",
        username="user",
        is_local_shell=True,
        forwarding_only=False,
    )


def _terminal(connection, root):
    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.connection = connection
    terminal.connection_manager = _Store()
    terminal.connection_state = ConnectionState.CONNECTED
    terminal.is_connected = True
    terminal.last_error_message = None
    terminal._connect_failure_hint = ''
    terminal._used_stored_password = False
    terminal.process_pid = None
    terminal._cancel_connect_grace = lambda: None
    terminal._scrape_recent_terminal_text = lambda max_chars=2000: ''
    terminal.emit = Mock()
    terminal._set_connecting_overlay_visible = Mock()
    terminal._record_error_detail = Mock()
    terminal._set_disconnected_banner_visible = Mock()
    terminal.get_root = lambda: root
    return terminal


def _root():
    root = Mock()
    root._page_for_child = Mock(return_value=Mock())
    # Not a reconnect: a truthy Mock here would short-circuit the handler.
    root._is_controlled_reconnect = False
    return root


@pytest.fixture
def run_idle_now(monkeypatch):
    """Run GLib.idle_add callbacks inline so the test sees their effects."""
    monkeypatch.setattr(
        "sshpilot.terminal.GLib.idle_add",
        lambda callback, *args: callback(*args) or 1,
    )


@pytest.mark.parametrize("exit_code", [0, 130, 1, 127])
def test_local_shell_exit_closes_the_tab_for_any_status(run_idle_now, exit_code):
    root = _root()
    terminal = _terminal(_local_connection(), root)

    terminal._handle_child_exit_cleanup(exit_code << 8)

    root.tab_view.close_page.assert_called_once_with(root._page_for_child.return_value)
    terminal._set_disconnected_banner_visible.assert_not_called()
    assert terminal.is_connected is False
    assert terminal.connection_state == ConnectionState.DISCONNECTED


def test_local_shell_without_a_tab_never_blames_ssh(run_idle_now):
    """Split-view panes have no page in the main tab view — still no ssh error."""
    root = _root()
    root._page_for_child = Mock(return_value=None)
    terminal = _terminal(_local_connection(), root)

    terminal._handle_child_exit_cleanup(130 << 8)

    root.tab_view.close_page.assert_not_called()
    terminal._set_disconnected_banner_visible.assert_called_once()
    assert terminal._set_disconnected_banner_visible.call_args[0][1] == 'Session ended.'


def test_ssh_session_still_reports_a_nonzero_exit(run_idle_now):
    """Regression guard: real ssh sessions keep their status-bearing banner."""
    root = _root()
    connection = SimpleNamespace(
        nickname="host", hostname="example.com", username="user", protocol="ssh"
    )
    terminal = _terminal(connection, root)

    terminal._handle_child_exit_cleanup(130 << 8)

    root.tab_view.close_page.assert_not_called()
    terminal._set_disconnected_banner_visible.assert_called_once()
    assert terminal._set_disconnected_banner_visible.call_args[0][1] == (
        'SSH exited with status 130'
    )
