"""Activation ownership — no silent legacy fallback for daemon route."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from sshpilot.api.capabilities import Capability
from sshpilot.api.version import PROTOCOL_VERSION
from sshpilot.daemon_terminal_policy import DaemonTerminalReadinessReason
from sshpilot.terminal_manager import TerminalManager
from sshpilot.terminal_session_controller import (
    required_daemon_terminal_capabilities as required_caps,
)


REQUIRED_CAPS = required_caps()


def _settings(mapping):
    return SimpleNamespace(
        get_setting=lambda key, default=None: mapping.get(key, default)
    )


def _capabilities(supported=REQUIRED_CAPS):
    caps = Mock()
    caps.protocol_version = PROTOCOL_VERSION
    caps.supported = frozenset(supported)
    caps.compatibility = Mock(compatible=True)
    return caps


class TestDaemonActivationOwnership:
    def _window(self, *, ready: bool = True, settings=None):
        window = Mock()
        window.config = _settings(
            settings
            or {
                "use-external-terminal": False,
                "terminal.daemon_backed_ssh": True,
                "terminal.removed_local_ssh_setting": False,
            }
        )
        window.client = Mock()
        window.client.open_session = Mock()
        window.client.server_instance_id = "test-daemon-123"
        window.client.get_capabilities = Mock(return_value=_capabilities())
        window.client_bridge = Mock() if ready else None
        if not ready:
            window.client = None
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
        window._open_connection_in_external_terminal = Mock()
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
        connection.ssh_cmd = None
        connection.native_connect = Mock()
        connection.connect = Mock()
        connection.is_connected = False
        return connection

    def test_ready_daemon_opens_session_without_local_ssh(self):
        window = self._window(ready=True)
        connection = self._connection()
        manager = TerminalManager(window)

        with patch(
            "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
            return_value=False,
        ), patch(
            "sshpilot.terminal_manager.TerminalWidget"
        ) as mock_terminal_widget, patch(
            "sshpilot.terminal_manager.GLib"
        ), patch.object(
            manager, "_maybe_unlock_secrets_then", return_value=False
        ) as unlock, patch.object(
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
            assert terminal._reconnect_handler == manager.reconnect_terminal
            connection.native_connect.assert_not_called()
            connection.connect.assert_not_called()
            unlock.assert_called()

    def test_reconnect_reopens_through_daemon_without_local_ssh(self):
        window = self._window(ready=True)
        connection = self._connection()
        manager = TerminalManager(window)
        terminal_output = Mock()
        old_controller = SimpleNamespace(
            close=Mock(),
            _on_output=terminal_output,
            _on_continuity_lost=Mock(),
            _on_state_changed=Mock(),
            _on_error=Mock(),
        )
        terminal = Mock(
            connection=connection,
            _daemon_controller=old_controller,
            _daemon_mode=True,
            _daemon_tab_state=Mock(),
            last_error_message="previous failure",
            connection_state_reason="failed",
        )
        terminal.start_daemon_session.return_value = True

        assert manager.reconnect_terminal(terminal) is True

        old_controller.close.assert_called_once_with()
        assert old_controller._on_output is None
        assert terminal_output.call_count == 0
        old_controller._on_error(RuntimeError("late close failure"))
        terminal._on_connection_failed.assert_not_called()
        terminal.start_daemon_session.assert_called_once_with(
            window.client,
            window.client_bridge,
            "TestServer",
        )
        connection.native_connect.assert_not_called()
        connection.connect.assert_not_called()

    def test_controlled_reconnect_uses_daemon_reopen(self):
        window = self._window(ready=True)
        connection = self._connection()
        terminal = Mock(connection=connection)
        window.active_terminals[connection] = terminal
        manager = TerminalManager(window)

        with patch.object(manager, "reconnect_terminal", return_value=True) as reopen:
            assert manager._reconnect_terminal(connection) is False

        reopen.assert_called_once_with(terminal)
        connection.native_connect.assert_not_called()

    def test_unavailable_daemon_no_local_spawn_no_blank_tab(self):
        window = self._window(ready=False)
        connection = self._connection()
        manager = TerminalManager(window)

        with patch(
            "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
            return_value=False,
        ), patch(
            "sshpilot.terminal_manager.TerminalWidget"
        ) as mock_terminal_widget, patch.object(
            manager, "_try_start_daemon_client",
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
            assert window.tab_view.append.call_count == 0

    def test_capability_mismatch_no_local_spawn(self):
        window = self._window(ready=True)
        supported = REQUIRED_CAPS - {Capability.TERMINAL_REPLAY}
        window.client.get_capabilities = Mock(
            return_value=_capabilities(supported)
        )
        connection = self._connection()
        manager = TerminalManager(window)

        with patch(
            "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
            return_value=False,
        ), patch(
            "sshpilot.terminal_manager.TerminalWidget"
        ) as mock_terminal_widget, patch.object(
            manager, "_maybe_unlock_secrets_then", return_value=False
        ) as unlock, patch.object(
            manager, "_show_daemon_error_dialog"
        ) as show_error:
            manager.connect_to_host(connection)
            show_error.assert_called_once()
            mock_terminal_widget.assert_not_called()
            connection.native_connect.assert_not_called()
            unlock.assert_not_called()

    def test_retired_legacy_setting_cannot_bypass_daemon_ownership(self):
        window = self._window(
            ready=True,
            settings={
                "use-external-terminal": False,
                "terminal.daemon_backed_ssh": True,
                "terminal.removed_local_ssh_setting": True,
            },
        )
        connection = self._connection()
        manager = TerminalManager(window)

        with patch(
            "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
            return_value=False,
        ), patch(
            "sshpilot.terminal_manager.TerminalWidget"
        ) as mock_terminal_widget, patch(
            "sshpilot.terminal_manager.GLib"
        ), patch.object(
            manager, "_maybe_unlock_secrets_then", return_value=False
        ) as unlock, patch.object(
            manager, "_show_daemon_error_dialog"
        ) as show_error:
            terminal = Mock()
            terminal.start_daemon_session = Mock()
            terminal.backend = None
            terminal.vte = None
            terminal.apply_theme = Mock()
            mock_terminal_widget.return_value = terminal

            manager.connect_to_host(connection)

            terminal.start_daemon_session.assert_called_once()
            show_error.assert_not_called()
            unlock.assert_called()
            connection.native_connect.assert_not_called()

    def test_external_route_no_internal_tab(self):
        window = self._window(
            ready=True,
            settings={
                "use-external-terminal": True,
                "terminal.daemon_backed_ssh": True,
                "terminal.removed_local_ssh_setting": False,
            },
        )
        connection = self._connection()
        manager = TerminalManager(window)

        with patch(
            "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
            return_value=False,
        ), patch(
            "sshpilot.terminal_manager.TerminalWidget"
        ) as mock_terminal_widget, patch.object(
            manager, "_maybe_unlock_secrets_then", return_value=False
        ):
            manager.connect_to_host(connection)
            window._open_connection_in_external_terminal.assert_called_once_with(
                connection
            )
            mock_terminal_widget.assert_not_called()
            connection.native_connect.assert_not_called()

    def test_daemon_route_skips_unlock_when_readiness_fails(self):
        window = self._window(ready=False)
        connection = self._connection()
        manager = TerminalManager(window)

        with patch(
            "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
            return_value=False,
        ), patch.object(
            manager,
            "_try_start_daemon_client",
            return_value=DaemonTerminalReadinessReason.CLIENT_UNAVAILABLE,
        ), patch.object(
            manager, "_maybe_unlock_secrets_then", return_value=False
        ) as unlock, patch.object(
            manager, "_show_daemon_error_dialog"
        ):
            manager.connect_to_host(connection)
            unlock.assert_not_called()
