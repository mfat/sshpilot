"""Terminal reconnect actions preserve daemon ownership for remote SSH."""

from types import SimpleNamespace
from unittest.mock import Mock

from sshpilot.terminal import TerminalWidget


def _remote_terminal():
    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.connection = SimpleNamespace(
        protocol="ssh",
        hostname="example.test",
    )
    terminal._set_disconnected_banner_visible = Mock()
    terminal._set_connecting_overlay_visible = Mock()
    terminal._record_error_detail = Mock()
    terminal._refresh_connection_command = Mock(return_value=True)
    terminal._connect_ssh = Mock(return_value=True)
    return terminal


def test_banner_reconnect_delegates_to_daemon_owner():
    terminal = _remote_terminal()
    terminal._reconnect_handler = Mock(return_value=True)

    terminal._on_reconnect_clicked()

    terminal._reconnect_handler.assert_called_once_with(terminal)
    terminal._refresh_connection_command.assert_not_called()
    terminal._connect_ssh.assert_not_called()


def test_public_reconnect_delegates_to_daemon_owner():
    terminal = _remote_terminal()
    terminal._reconnect_handler = Mock(return_value=True)

    assert terminal.reconnect() is True

    terminal._reconnect_handler.assert_called_once_with(terminal)
    terminal._refresh_connection_command.assert_not_called()
    terminal._connect_ssh.assert_not_called()


def test_remote_reconnect_without_daemon_owner_refuses_local_spawn():
    terminal = _remote_terminal()
    terminal._reconnect_handler = None

    terminal._on_reconnect_clicked()

    terminal._refresh_connection_command.assert_not_called()
    terminal._connect_ssh.assert_not_called()
    terminal._set_disconnected_banner_visible.assert_called_with(
        True, "Reconnect failed"
    )
