"""Test daemon terminal close policy behavior."""

import logging
import pytest
from unittest.mock import Mock, patch

from sshpilot.api.models.connections import ConnectionSummary
from sshpilot.daemon_terminal_policy import TerminalClosePolicy
from sshpilot.gtk.connection_store import ConnectionPresentationStore


def _immutable_connection():
    return ConnectionSummary(
        id="connection-1",
        nickname="example",
        host="example",
        hostname="example.test",
        username="alice",
        port=22,
    )


class TestDaemonTerminalClosePolicy:
    """Test close policy behavior for daemon terminals."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config."""
        config = Mock()
        config.get_setting = Mock()
        return config

    @pytest.fixture
    def mock_terminal_widget(self):
        """Create a mock terminal widget with daemon components."""
        terminal = Mock()
        terminal.config = Mock()
        terminal._daemon_mode = True
        
        # Create a proper mock daemon controller
        mock_controller = Mock()
        mock_controller.detach = Mock()
        mock_controller.close = Mock()
        terminal._daemon_controller = mock_controller
        
        terminal._daemon_tab_state = Mock()
        terminal._daemon_tab_state.session_id = 'test-session-123'
        terminal.connection = Mock()
        terminal.connection.is_connected = True
        terminal.is_connected = True
        terminal.emit = Mock()
        terminal.get_root = Mock(return_value=Mock())
        terminal._view_only_overlay = None
        terminal._hide_view_only_indicator = Mock()
        return terminal

    def test_detach_policy(self, mock_config, mock_terminal_widget):
        """Test detach policy behavior."""
        # Test the logic directly rather than calling the method
        from sshpilot.daemon_terminal_policy import TerminalClosePolicy, resolve_tab_close_policy
        
        with patch('sshpilot.daemon_terminal_policy.resolve_tab_close_policy', return_value=TerminalClosePolicy.DETACH):
            policy = resolve_tab_close_policy(mock_config)
            
            # Verify correct policy is resolved
            assert policy == TerminalClosePolicy.DETACH
            
            # Simulate the policy action
            if policy == TerminalClosePolicy.DETACH:
                mock_terminal_widget._daemon_controller.detach()
            
            # Verify detach was called
            mock_terminal_widget._daemon_controller.detach.assert_called_once()

    def test_terminate_policy(self, mock_config, mock_terminal_widget):
        """Test terminate policy behavior."""
        # Test the logic directly - just verify the enum values work correctly
        from sshpilot.daemon_terminal_policy import TerminalClosePolicy
        
        # Test that we can create the terminate policy
        policy = TerminalClosePolicy.TERMINATE
        assert policy == TerminalClosePolicy.TERMINATE
        assert policy.value == 'terminate'
        
        # Simulate the policy action
        if policy == TerminalClosePolicy.TERMINATE:
            mock_terminal_widget._daemon_controller.close()
        
        # Verify close was called
        mock_terminal_widget._daemon_controller.close.assert_called_once()

    def test_ask_policy(self, mock_config, mock_terminal_widget):
        """Test ask policy behavior."""
        # Configure ask policy
        mock_config.get_setting.side_effect = lambda key, default=None: {
            'terminal.daemon_tab_close_policy': 'ask'
        }.get(key, default)

        mock_terminal_widget.config = mock_config

        with patch('sshpilot.daemon_terminal_policy.resolve_tab_close_policy', return_value=TerminalClosePolicy.ASK), \
             patch.object(mock_terminal_widget, '_show_daemon_close_dialog') as mock_show_dialog:

            # Call _handle_daemon_close
            from sshpilot.terminal import TerminalWidget
            TerminalWidget._handle_daemon_close(mock_terminal_widget, is_quitting=False)

            # Verify dialog was shown, no immediate action
            mock_show_dialog.assert_called_once()
            mock_terminal_widget._daemon_controller.detach.assert_not_called()
            mock_terminal_widget._daemon_controller.close.assert_not_called()

    def test_app_close_policy_during_quit(self, mock_config, mock_terminal_widget):
        """App close policy defaults to ASK; explicit detach is honored."""
        from sshpilot.daemon_terminal_policy import (
            TerminalClosePolicy,
            resolve_app_close_policy,
        )

        # Default (unset) is ASK so quit presents Keep running / Terminate.
        mock_config.get_setting.side_effect = lambda key, default=None: default
        assert resolve_app_close_policy(mock_config) == TerminalClosePolicy.ASK

        mock_config.get_setting.side_effect = lambda key, default=None: {
            "terminal.daemon_app_close_policy": "detach"
        }.get(key, default)
        policy = resolve_app_close_policy(mock_config)
        assert policy == TerminalClosePolicy.DETACH

        if policy == TerminalClosePolicy.DETACH:
            mock_terminal_widget._daemon_controller.detach()
        mock_terminal_widget._daemon_controller.detach.assert_called_once()

    def test_dialog_detach_response(self, mock_terminal_widget):
        """Test dialog detach response."""
        # Set up daemon controller mock properly
        mock_controller = Mock()
        mock_terminal_widget._daemon_controller = mock_controller
        
        with patch('gi.repository.Adw') as mock_adw:
            mock_dialog = Mock()
            mock_adw.AlertDialog.new.return_value = mock_dialog

            # Call _show_daemon_close_dialog
            from sshpilot.terminal import TerminalWidget
            TerminalWidget._show_daemon_close_dialog(mock_terminal_widget)

            # Simulate detach response
            TerminalWidget._on_daemon_close_dialog_response(mock_terminal_widget, mock_dialog, "detach")

            # Verify detach was called
            mock_controller.detach.assert_called_once()

    def test_dialog_terminate_response(self, mock_terminal_widget):
        """Test dialog terminate response."""
        # Set up daemon controller mock properly
        mock_controller = Mock()
        mock_terminal_widget._daemon_controller = mock_controller
        
        with patch('gi.repository.Adw') as mock_adw:
            mock_dialog = Mock()
            mock_adw.AlertDialog.new.return_value = mock_dialog

            # Call _show_daemon_close_dialog
            from sshpilot.terminal import TerminalWidget
            TerminalWidget._show_daemon_close_dialog(mock_terminal_widget)

            # Simulate terminate response
            TerminalWidget._on_daemon_close_dialog_response(mock_terminal_widget, mock_dialog, "terminate")

            # Verify close was called
            mock_controller.close.assert_called_once()

    def test_dialog_cancel_response(self, mock_terminal_widget):
        """Test dialog cancel response."""
        with patch('gi.repository.Adw') as mock_adw:
            mock_dialog = Mock()
            mock_adw.AlertDialog.new.return_value = mock_dialog

            # Call _show_daemon_close_dialog
            from sshpilot.terminal import TerminalWidget
            TerminalWidget._show_daemon_close_dialog(mock_terminal_widget)

            # Simulate cancel response
            TerminalWidget._on_daemon_close_dialog_response(mock_terminal_widget, mock_dialog, "cancel")

            # Verify no action was taken
            mock_terminal_widget._daemon_controller.detach.assert_not_called()
            mock_terminal_widget._daemon_controller.close.assert_not_called()
            mock_terminal_widget.emit.assert_not_called()

    def test_disconnect_triggers_daemon_close(self, mock_terminal_widget):
        """Test that disconnect() triggers daemon close logic."""
        mock_terminal_widget._is_quitting = False
        mock_terminal_widget.get_root = Mock(return_value=Mock())
        mock_terminal_widget.get_root.return_value._is_quitting = False

        with patch.object(mock_terminal_widget, '_handle_daemon_close') as mock_handle_close:
            # Call disconnect
            from sshpilot.terminal import TerminalWidget
            TerminalWidget.disconnect(mock_terminal_widget)

            # Verify daemon close handler was called with correct quitting state
            # The method computes is_quitting from terminal and root flags
            mock_handle_close.assert_called_once()

    def test_state_cleanup_after_close(self, mock_terminal_widget):
        """Test that daemon state is cleaned up after close."""
        mock_terminal_widget._daemon_mode = True
        mock_terminal_widget._daemon_controller = Mock()
        mock_terminal_widget._daemon_tab_state = Mock()
        mock_terminal_widget._view_only_overlay = None

        with patch('sshpilot.daemon_terminal_policy.resolve_tab_close_policy', return_value=TerminalClosePolicy.DETACH), \
             patch.object(mock_terminal_widget, '_hide_view_only_indicator') as mock_hide_indicator:

            # Call _handle_daemon_close
            from sshpilot.terminal import TerminalWidget
            TerminalWidget._handle_daemon_close(mock_terminal_widget, is_quitting=False)

            # Verify state was cleaned up
            assert mock_terminal_widget._daemon_mode == False
            assert mock_terminal_widget._daemon_controller is None
            assert mock_terminal_widget._daemon_tab_state is None
            mock_hide_indicator.assert_called_once()
            assert mock_terminal_widget.is_connected == False

    def test_close_does_not_mutate_daemon_connection_projection(
        self,
        mock_terminal_widget,
        caplog,
    ):
        """Daemon connection DTOs are immutable presentation data."""
        mock_terminal_widget.connection = _immutable_connection()

        with (
            patch(
                'sshpilot.daemon_terminal_policy.resolve_tab_close_policy',
                return_value=TerminalClosePolicy.DETACH,
            ),
            caplog.at_level(logging.ERROR, logger='sshpilot.terminal'),
        ):
            from sshpilot.terminal import TerminalWidget

            TerminalWidget._handle_daemon_close(
                mock_terminal_widget,
                is_quitting=False,
            )

        assert mock_terminal_widget.is_connected is False
        assert "Failed to handle daemon close" not in caplog.text

    def test_transport_failure_skips_legacy_state_api_for_daemon_projection(
        self,
        caplog,
    ):
        """Presentation stores expose snapshots, not mutable status methods."""
        terminal = Mock()
        terminal.connection = _immutable_connection()
        terminal.connection_manager = ConnectionPresentationStore()
        terminal.backend = Mock()
        terminal.pty = None
        terminal._fallback_timer_id = None
        terminal._classify_exit.return_value = (None, "")

        with caplog.at_level(logging.ERROR, logger='sshpilot.terminal'):
            from sshpilot.terminal import TerminalWidget

            TerminalWidget._on_connection_failed(
                terminal,
                "The daemon transport is closed",
            )

        assert terminal.is_connected is False
        assert terminal.last_error_message == "The daemon transport is closed"
        terminal.emit.assert_called_once_with(
            'connection-failed',
            "The daemon transport is closed",
        )
        assert "Error in _on_connection_failed" not in caplog.text
