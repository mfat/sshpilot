"""Test broadcast behavior with daemon terminal input ownership."""

import pytest
from unittest.mock import Mock, patch

from sshpilot.terminal_manager import TerminalManager


class TestDaemonBroadcastOwnership:
    """Test that broadcast respects daemon terminal input ownership."""

    @pytest.fixture
    def mock_window(self):
        """Create a mock window."""
        window = Mock()
        window.config = Mock()
        return window

    @pytest.fixture
    def terminal_manager(self, mock_window):
        """Create a terminal manager."""
        return TerminalManager(mock_window)

    def create_mock_terminal(self, is_daemon=False, has_input=True, connection_nickname="test"):
        """Create a mock terminal widget."""
        terminal = Mock()
        terminal.connection = Mock()
        terminal.connection.nickname = connection_nickname
        terminal.is_daemon_backed = is_daemon
        terminal.has_input_ownership = has_input
        
        if is_daemon:
            terminal._daemon_controller = Mock()
        else:
            terminal.backend = Mock()
            terminal.feed_child_data = terminal.backend.feed_child_data
            
        return terminal

    def test_broadcast_to_local_terminals(self, terminal_manager):
        """Test broadcast works normally with local terminals."""
        # Create local terminals
        terminal1 = self.create_mock_terminal(is_daemon=False, connection_nickname="local1")
        terminal2 = self.create_mock_terminal(is_daemon=False, connection_nickname="local2")
        
        terminals = [terminal1, terminal2]
        
        with patch.object(terminal_manager, 'iter_ssh_terminals', return_value=terminals):
            sent_count, failed_count = terminal_manager.broadcast_command("ls -la")
            
            # Both terminals should receive the command
            assert sent_count == 2
            assert failed_count == 0
            
            terminal1.backend.feed_child_data.assert_called_once()
            terminal2.backend.feed_child_data.assert_called_once()

    def test_broadcast_to_daemon_terminals_with_input(self, terminal_manager):
        """Test broadcast to daemon terminals with input ownership."""
        # Create daemon terminals with input ownership
        terminal1 = self.create_mock_terminal(is_daemon=True, has_input=True, connection_nickname="daemon1")
        terminal2 = self.create_mock_terminal(is_daemon=True, has_input=True, connection_nickname="daemon2")
        
        terminals = [terminal1, terminal2]
        
        with patch.object(terminal_manager, 'iter_ssh_terminals', return_value=terminals):
            sent_count, failed_count = terminal_manager.broadcast_command("ps aux")
            
            # Both terminals should receive the command
            assert sent_count == 2
            assert failed_count == 0
            
            # Verify send_input was called on daemon controllers
            terminal1._daemon_controller.send_input.assert_called_once()
            terminal2._daemon_controller.send_input.assert_called_once()

    def test_broadcast_skips_daemon_terminals_without_input(self, terminal_manager):
        """Test broadcast skips daemon terminals without input ownership."""
        # Create daemon terminals without input ownership
        terminal1 = self.create_mock_terminal(is_daemon=True, has_input=False, connection_nickname="daemon1")
        terminal2 = self.create_mock_terminal(is_daemon=True, has_input=False, connection_nickname="daemon2")
        
        terminals = [terminal1, terminal2]
        
        with patch.object(terminal_manager, 'iter_ssh_terminals', return_value=terminals):
            sent_count, failed_count = terminal_manager.broadcast_command("whoami")
            
            # No terminals should receive the command
            assert sent_count == 0
            assert failed_count == 2
            
            # Verify send_input was NOT called
            terminal1._daemon_controller.send_input.assert_not_called()
            terminal2._daemon_controller.send_input.assert_not_called()

    def test_broadcast_mixed_terminals(self, terminal_manager):
        """Test broadcast with mixed local and daemon terminals."""
        # Create mixed terminals
        local_terminal = self.create_mock_terminal(is_daemon=False, connection_nickname="local")
        daemon_with_input = self.create_mock_terminal(is_daemon=True, has_input=True, connection_nickname="daemon_input")
        daemon_without_input = self.create_mock_terminal(is_daemon=True, has_input=False, connection_nickname="daemon_no_input")
        
        terminals = [local_terminal, daemon_with_input, daemon_without_input]
        
        with patch.object(terminal_manager, 'iter_ssh_terminals', return_value=terminals):
            sent_count, failed_count = terminal_manager.broadcast_command("pwd")
            
            # Only local and daemon with input should receive command
            assert sent_count == 2
            assert failed_count == 1
            
            # Verify correct methods were called
            local_terminal.backend.feed_child_data.assert_called_once()
            daemon_with_input._daemon_controller.send_input.assert_called_once()
            daemon_without_input._daemon_controller.send_input.assert_not_called()

    def test_broadcast_daemon_controller_missing(self, terminal_manager):
        """Test broadcast handles missing daemon controller gracefully."""
        # Create daemon terminal without controller
        terminal = self.create_mock_terminal(is_daemon=True, has_input=True, connection_nickname="broken_daemon")
        terminal._daemon_controller = None  # Missing controller
        
        terminals = [terminal]
        
        with patch.object(terminal_manager, 'iter_ssh_terminals', return_value=terminals):
            sent_count, failed_count = terminal_manager.broadcast_command("echo test")
            
            # Should fail gracefully
            assert sent_count == 0
            assert failed_count == 1

    def test_broadcast_command_encoding(self, terminal_manager):
        """Test that broadcast commands are properly encoded."""
        terminal = self.create_mock_terminal(is_daemon=True, has_input=True, connection_nickname="daemon")
        terminals = [terminal]
        
        with patch.object(terminal_manager, 'iter_ssh_terminals', return_value=terminals):
            terminal_manager.broadcast_command("echo 'hello world'")
            
            # Verify the command was encoded with newline
            expected_data = b"echo 'hello world'\n"
            terminal._daemon_controller.send_input.assert_called_once_with(expected_data)

    def test_broadcast_handles_exceptions(self, terminal_manager):
        """Test broadcast handles exceptions gracefully."""
        terminal = self.create_mock_terminal(is_daemon=True, has_input=True, connection_nickname="error_terminal")
        terminal._daemon_controller.send_input.side_effect = Exception("Network error")
        
        terminals = [terminal]
        
        with patch.object(terminal_manager, 'iter_ssh_terminals', return_value=terminals):
            sent_count, failed_count = terminal_manager.broadcast_command("ls")
            
            # Should count as failure
            assert sent_count == 0
            assert failed_count == 1

    def test_empty_terminal_list(self, terminal_manager):
        """Test broadcast with no terminals."""
        with patch.object(terminal_manager, 'iter_ssh_terminals', return_value=[]):
            sent_count, failed_count = terminal_manager.broadcast_command("test")
            
            # Should complete without errors
            assert sent_count == 0
            assert failed_count == 0
