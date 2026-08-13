"""Test daemon terminal activation in TerminalManager."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from sshpilot.daemon_terminal_policy import (
    SshTerminalRoute,
    DaemonTerminalReadinessReason,
    resolve_ssh_terminal_route,
    should_use_daemon_ssh_terminal,
)
from sshpilot.terminal_manager import TerminalManager
from sshpilot.api.capabilities import Capability
from sshpilot.api.version import PROTOCOL_VERSION


REQUIRED_CAPS = {
    Capability.SESSIONS_READ,
    Capability.SESSIONS_WRITE,
    Capability.SESSIONS_COMMAND,
    Capability.SESSIONS_EVENTS,
    Capability.TERMINAL_OUTPUT,
    Capability.TERMINAL_INPUT,
    Capability.TERMINAL_RESIZE,
    Capability.TERMINAL_REPLAY,
    Capability.INTERACTIONS_READ,
    Capability.INTERACTIONS_RESPOND,
    Capability.INTERACTIONS_EVENTS,
    Capability.INTERACTIONS_HOST_KEY,
    Capability.INTERACTIONS_PASSWORD,
    Capability.INTERACTIONS_PASSPHRASE,
}


def _settings(mapping):
    return SimpleNamespace(
        get_setting=lambda key, default=None: mapping.get(key, default)
    )


def _capabilities():
    caps = Mock()
    caps.protocol_version = PROTOCOL_VERSION
    caps.supported = frozenset(REQUIRED_CAPS)
    caps.compatibility = Mock(compatible=True)
    return caps


class TestDaemonTerminalActivation:
    def _window(self):
        window = Mock()
        window.config = _settings(
            {
                "use-external-terminal": False,
                "terminal.daemon_backed_ssh": True,
                "terminal.removed_local_ssh_setting": False,
            }
        )
        window.client = Mock()
        window.client.open_session = Mock()
        window.client.server_instance_id = "test-daemon-123"
        window.client.get_capabilities = Mock(return_value=_capabilities())
        window.client_bridge = Mock()
        window.tab_view = Mock()
        window.tab_view.append = Mock(return_value=Mock())
        window.tab_view.close_page = Mock()
        window.active_terminals = {}
        window.connection_to_terminals = {}
        window.terminal_to_connection = {}
        window._is_quitting = False
        window._api_client_selection_pending = False
        window.show_tab_view = Mock()
        window._page_for_child = Mock(return_value=None)
        window.get_application = Mock(return_value=None)
        window.connection_manager = Mock()
        window.group_manager = None
        return window

    def _connection(self):
        connection = Mock()
        connection.id = "TestServer"
        connection.uuid = "TestServer"
        connection.protocol = "ssh"
        connection.nickname = "TestServer"
        connection.native_connect = Mock()
        connection.connect = Mock()
        connection.ssh_cmd = None
        connection.is_connected = False
        return connection

    def test_daemon_mode_when_policy_true(self):
        window = self._window()
        connection = self._connection()
        manager = TerminalManager(window)

        assert (
            resolve_ssh_terminal_route(window, connection)
            is SshTerminalRoute.DAEMON
        )
        assert should_use_daemon_ssh_terminal(
            window, connection, client=window.client
        )

        with patch(
            "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
            return_value=False,
        ), patch(
            "sshpilot.terminal_manager.TerminalWidget"
        ) as mock_terminal_widget, patch(
            "sshpilot.terminal_manager.GLib"
        ), patch.object(
            manager, "_maybe_unlock_secrets_then", return_value=False
        ), patch.object(
            manager, "_show_daemon_error_dialog"
        ):
            terminal = Mock()
            terminal.start_daemon_session = Mock(return_value=True)
            terminal.backend = None
            terminal.vte = None
            terminal.apply_theme = Mock()
            mock_terminal_widget.return_value = terminal
            manager.connect_to_host(connection)
            terminal.start_daemon_session.assert_called_once()
            args = terminal.start_daemon_session.call_args[0]
            assert args[0] is window.client
            assert args[1] is window.client_bridge
            assert str(args[2]) == "TestServer"
            connection.native_connect.assert_not_called()

    def test_retired_daemon_setting_cannot_restore_local_mode(self):
        window = self._window()
        window.config = _settings(
            {
                "use-external-terminal": False,
                "terminal.daemon_backed_ssh": False,
            }
        )
        connection = self._connection()
        manager = TerminalManager(window)

        assert (
            resolve_ssh_terminal_route(window, connection)
            is SshTerminalRoute.DAEMON
        )
        assert should_use_daemon_ssh_terminal(
            window, connection, client=window.client
        )

        with patch(
            "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
            return_value=False,
        ), patch(
            "sshpilot.terminal_manager.TerminalWidget"
        ) as mock_terminal_widget, patch(
            "sshpilot.terminal_manager.GLib"
        ), patch.object(
            manager, "_maybe_unlock_secrets_then", return_value=False
        ):
            terminal = Mock()
            terminal.start_daemon_session = Mock()
            terminal.backend = None
            terminal.vte = None
            terminal.apply_theme = Mock()
            mock_terminal_widget.return_value = terminal
            manager.connect_to_host(connection)
            terminal.start_daemon_session.assert_called_once()

    def test_daemon_failure_does_not_spawn_local_ssh(self):
        window = self._window()
        connection = self._connection()
        manager = TerminalManager(window)

        with patch(
            "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
            return_value=False,
        ), patch(
            "sshpilot.terminal_manager.TerminalWidget"
        ) as mock_terminal_widget, patch(
            "sshpilot.terminal_manager.GLib"
        ) as mock_glib, patch.object(
            manager, "_maybe_unlock_secrets_then", return_value=False
        ), patch.object(
            manager, "_show_daemon_error_dialog"
        ) as show_error:
            terminal = Mock()
            terminal.start_daemon_session = Mock(side_effect=RuntimeError("boom"))
            terminal.backend = None
            terminal.vte = None
            terminal.apply_theme = Mock()
            mock_terminal_widget.return_value = terminal
            manager.connect_to_host(connection)
            show_error.assert_called_once()
            connection.native_connect.assert_not_called()
            assert mock_glib.idle_add.call_count == 0

    def test_unavailable_client_does_not_silently_use_legacy(self):
        """Regression: readiness failure must not change route to local SSH."""

        window = self._window()
        window.client = None
        window.client_bridge = None
        connection = self._connection()
        manager = TerminalManager(window)

        assert should_use_daemon_ssh_terminal(window, connection, client=None)

        with patch(
            "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
            return_value=False,
        ), patch(
            "sshpilot.terminal_manager.TerminalWidget"
        ) as mock_terminal_widget, patch.object(
            manager,
            "_try_start_daemon_client",
            return_value=DaemonTerminalReadinessReason.DAEMON_START_FAILED,
        ), patch.object(
            manager, "_maybe_unlock_secrets_then", return_value=False
        ) as unlock, patch.object(
            manager, "_show_daemon_error_dialog"
        ) as show_error:
            manager.connect_to_host(connection)
            show_error.assert_called_once()
            mock_terminal_widget.assert_not_called()
            connection.native_connect.assert_not_called()
            unlock.assert_not_called()
