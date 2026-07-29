"""Test daemon terminal activation in TerminalManager."""

from unittest.mock import Mock, patch

from sshpilot.connection_identity import new_connection_uuid
from sshpilot.daemon_terminal_policy import should_use_daemon_ssh_terminal
from sshpilot.terminal_manager import TerminalManager
from sshpilot.api.capabilities import Capability


REQUIRED_CAPS = {
    Capability.SESSIONS_READ,
    Capability.SESSIONS_WRITE,
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


class TestDaemonTerminalActivation:
    def _window(self):
        window = Mock()
        window.config = Mock()
        window.client = Mock()
        window.client.open_session = Mock()
        window.client.server_instance_id = "test-daemon-123"
        window.client.get_capabilities = Mock(
            return_value=Mock(supported=REQUIRED_CAPS)
        )
        window.client_bridge = Mock()
        window.tab_view = Mock()
        window.tab_view.append = Mock(return_value=Mock())
        window.active_terminals = {}
        window.connection_to_terminals = {}
        window.terminal_to_connection = {}
        window._is_quitting = False
        window.show_tab_view = Mock()
        window._page_for_child = Mock(return_value=None)
        return window

    def _connection(self):
        connection = Mock()
        connection.uuid = new_connection_uuid()
        connection.protocol = "ssh"
        connection.nickname = "TestServer"
        return connection

    def test_daemon_mode_when_policy_true(self):
        window = self._window()
        connection = self._connection()
        window.config.get_setting.side_effect = lambda key, default=None: {
            "use-external-terminal": False,
            "terminal.daemon_backed_ssh": True,
            "terminal.legacy_local_ssh_fallback": False,
        }.get(key, default)
        manager = TerminalManager(window)

        assert should_use_daemon_ssh_terminal(
            window, connection, client=window.client
        )

        with patch("sshpilot.terminal_manager.should_hide_external_terminal_options", return_value=False), \
             patch("sshpilot.terminal_manager.TerminalWidget") as mock_terminal_widget, \
             patch("sshpilot.terminal_manager.GLib"), \
             patch.object(manager, "_show_daemon_error_dialog"):
            terminal = Mock()
            terminal.start_daemon_session = Mock(return_value=True)
            mock_terminal_widget.return_value = terminal
            manager.connect_to_host(connection)
            terminal.start_daemon_session.assert_called_once()
            args = terminal.start_daemon_session.call_args[0]
            assert args[0] is window.client
            assert args[1] is window.client_bridge
            assert str(args[2]).startswith("connection:")

    def test_local_mode_when_daemon_disabled(self):
        window = self._window()
        connection = self._connection()
        window.config.get_setting.side_effect = lambda key, default=None: {
            "use-external-terminal": False,
            "terminal.daemon_backed_ssh": False,
        }.get(key, default)
        manager = TerminalManager(window)

        assert not should_use_daemon_ssh_terminal(
            window, connection, client=window.client
        )

        with patch("sshpilot.terminal_manager.should_hide_external_terminal_options", return_value=False), \
             patch("sshpilot.terminal_manager.TerminalWidget") as mock_terminal_widget, \
             patch("sshpilot.terminal_manager.GLib"):
            terminal = Mock()
            terminal.start_daemon_session = Mock()
            mock_terminal_widget.return_value = terminal
            manager.connect_to_host(connection)
            terminal.start_daemon_session.assert_not_called()

    def test_daemon_failure_does_not_spawn_local_ssh(self):
        window = self._window()
        connection = self._connection()
        window.config.get_setting.side_effect = lambda key, default=None: {
            "use-external-terminal": False,
            "terminal.daemon_backed_ssh": True,
            "terminal.legacy_local_ssh_fallback": False,
        }.get(key, default)
        manager = TerminalManager(window)

        with patch("sshpilot.terminal_manager.should_hide_external_terminal_options", return_value=False), \
             patch("sshpilot.terminal_manager.TerminalWidget") as mock_terminal_widget, \
             patch("sshpilot.terminal_manager.GLib") as mock_glib, \
             patch.object(manager, "_show_daemon_error_dialog") as show_error:
            terminal = Mock()
            terminal.start_daemon_session = Mock(side_effect=RuntimeError("boom"))
            mock_terminal_widget.return_value = terminal
            manager.connect_to_host(connection)
            show_error.assert_called_once()
            mock_glib.idle_add.assert_not_called()
