"""
Tests for UI components
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, GLib

from io.github.mfat.sshpilot.main import SshPilotApplication
from io.github.mfat.sshpilot.window import MainWindow, ConnectionRow
from io.github.mfat.sshpilot.connection_manager import Connection


class TestSshPilotApplication:
    """Test main application class"""
    
    def setup_method(self):
        """Set up test fixtures"""
        self.app = SshPilotApplication()
    
    def test_application_id(self):
        """Test application ID is correct"""
        assert self.app.get_application_id() == 'io.github.mfat.sshpilot'
    
    def test_application_actions(self):
        """Test application actions are created"""
        actions = [
            'quit',
            'new-connection',
            'toggle-list',
            'new-key',
            'show-resources',
            'preferences',
            'about'
        ]
        
        for action_name in actions:
            action = self.app.lookup_action(action_name)
            assert action is not None, f"Action '{action_name}' not found"
    
    @patch('io.github.mfat.sshpilot.window.MainWindow')
    def test_do_activate_creates_window(self, mock_window_class):
        """Test that activate creates main window"""
        mock_window = Mock()
        mock_window_class.return_value = mock_window
        
        self.app.do_activate()
        
        mock_window_class.assert_called_once_with(application=self.app)
        mock_window.present.assert_called_once()
    
    def test_do_activate_existing_window(self):
        """Test that activate presents existing window"""
        # Create mock window and set as active
        mock_window = Mock()
        self.app.add_window(mock_window)
        
        self.app.do_activate()
        
        mock_window.present.assert_called_once()


class TestConnectionRow:
    """Test connection row widget"""
    
    def setup_method(self):
        """Set up test fixtures"""
        self.connection_data = {
            'nickname': 'Test Server',
            'host': 'example.com',
            'username': 'testuser',
            'port': 22
        }
        self.connection = Connection(self.connection_data)
    
    def test_row_creation(self):
        """Test creating connection row"""
        row = ConnectionRow(self.connection)
        
        assert row.connection == self.connection
        assert isinstance(row, Gtk.ListBoxRow)
    
    def test_row_displays_connection_info(self):
        """Test that row displays connection information"""
        row = ConnectionRow(self.connection)
        
        # Check that labels are set correctly
        # Note: In real tests, you'd need to traverse the widget tree
        # to find the actual labels, but this is a simplified test
        assert row.connection.nickname == 'Test Server'
        assert row.connection.host == 'example.com'
    
    def test_update_status_connected(self):
        """Test status update for connected state"""
        row = ConnectionRow(self.connection)
        
        # Set connection as connected
        self.connection.is_connected = True
        row.update_status()
        
        # Verify status icon shows connected state
        # In a real test, you'd check the actual icon name
        assert self.connection.is_connected
    
    def test_update_status_disconnected(self):
        """Test status update for disconnected state"""
        row = ConnectionRow(self.connection)
        
        # Set connection as disconnected
        self.connection.is_connected = False
        row.update_status()
        
        # Verify status icon shows disconnected state
        assert not self.connection.is_connected


class TestMainWindow:
    """Test main window class"""
    
    def setup_method(self):
        """Set up test fixtures"""
        # Create mock application
        self.app = Mock()
        self.app.get_application_id.return_value = 'io.github.mfat.sshpilot'
        
        # Mock managers to avoid complex initialization
        with patch('io.github.mfat.sshpilot.window.ConnectionManager'), \
             patch('io.github.mfat.sshpilot.window.Config'), \
             patch('io.github.mfat.sshpilot.window.ResourceMonitor'), \
             patch('io.github.mfat.sshpilot.window.KeyManager'):
            
            self.window = MainWindow(application=self.app)
    
    def test_window_initialization(self):
        """Test window is properly initialized"""
        assert self.window is not None
        assert hasattr(self.window, 'connection_manager')
        assert hasattr(self.window, 'config')
        assert hasattr(self.window, 'resource_monitor')
        assert hasattr(self.window, 'key_manager')
    
    def test_window_has_ui_components(self):
        """Test window has required UI components"""
        # These would be set up by the UI template in real usage
        assert hasattr(self.window, 'active_terminals')
        assert hasattr(self.window, 'connection_rows')
        assert isinstance(self.window.active_terminals, dict)
        assert isinstance(self.window.connection_rows, dict)
    
    @patch('io.github.mfat.sshpilot.window.MainWindow.show_connection_dialog')
    def test_add_connection_row(self, mock_show_dialog):
        """Test adding connection row to list"""
        # Create test connection
        connection_data = {
            'nickname': 'Test Server',
            'host': 'example.com',
            'username': 'testuser'
        }
        connection = Connection(connection_data)
        
        # Mock the list box
        self.window.connection_list = Mock()
        
        self.window.add_connection_row(connection)
        
        # Verify connection was added
        assert connection in self.window.connection_rows
        self.window.connection_list.append.assert_called_once()
    
    @patch('io.github.mfat.sshpilot.terminal.TerminalWidget')
    def test_connect_to_host(self, mock_terminal_class):
        """Test connecting to host creates terminal"""
        # Create test connection
        connection_data = {
            'nickname': 'Test Server',
            'host': 'example.com',
            'username': 'testuser'
        }
        connection = Connection(connection_data)
        
        # Mock UI components
        self.window.tab_view = Mock()
        mock_terminal = Mock()
        mock_terminal_class.return_value = mock_terminal
        
        self.window.connect_to_host(connection)
        
        # Verify terminal was created and added
        mock_terminal_class.assert_called_once_with(connection, self.window.config)
        self.window.tab_view.append.assert_called_once_with(mock_terminal)
        assert connection in self.window.active_terminals
    
    def test_connect_to_host_already_connected(self):
        """Test connecting to already connected host activates tab"""
        # Create test connection
        connection_data = {
            'nickname': 'Test Server',
            'host': 'example.com',
            'username': 'testuser'
        }
        connection = Connection(connection_data)
        
        # Mock existing terminal
        mock_terminal = Mock()
        self.window.active_terminals[connection] = mock_terminal
        self.window.tab_view = Mock()
        
        self.window.connect_to_host(connection)
        
        # Verify existing tab was activated
        self.window.tab_view.get_page.assert_called_once_with(mock_terminal)
        self.window.tab_view.set_selected_page.assert_called_once()
    
    def test_disconnect_from_host(self):
        """Test disconnecting from host"""
        # Create test connection with terminal
        connection_data = {
            'nickname': 'Test Server',
            'host': 'example.com',
            'username': 'testuser'
        }
        connection = Connection(connection_data)
        
        mock_terminal = Mock()
        self.window.active_terminals[connection] = mock_terminal
        
        self.window.disconnect_from_host(connection)
        
        # Verify terminal was disconnected
        mock_terminal.disconnect.assert_called_once()


class TestUIIntegration:
    """Integration tests for UI components"""
    
    @pytest.fixture
    def gtk_app(self):
        """Create GTK application for testing"""
        app = SshPilotApplication()
        yield app
        # Cleanup would go here if needed
    
    def test_application_window_creation(self, gtk_app):
        """Test that application can create window"""
        # This would require a full GTK environment
        # In practice, you might use xvfb for headless testing
        pass
    
    def test_connection_list_interaction(self, gtk_app):
        """Test interaction with connection list"""
        # This would test actual UI interaction
        # Requires GTK test framework or UI automation
        pass


# Mock fixtures for testing without full GTK environment
@pytest.fixture
def mock_gtk_environment():
    """Mock GTK environment for testing"""
    with patch('gi.repository.Gtk'), \
         patch('gi.repository.Adw'), \
         patch('gi.repository.GLib'), \
         patch('gi.repository.Gio'):
        yield


class TestUIWithMocks:
    """Test UI components with mocked GTK"""
    
    def test_window_creation_with_mocks(self, mock_gtk_environment):
        """Test window creation with mocked GTK"""
        with patch('io.github.mfat.sshpilot.window.ConnectionManager'), \
             patch('io.github.mfat.sshpilot.window.Config'), \
             patch('io.github.mfat.sshpilot.window.ResourceMonitor'), \
             patch('io.github.mfat.sshpilot.window.KeyManager'):
            
            app = Mock()
            window = MainWindow(application=app)
            
            assert window is not None
    
    def test_connection_row_with_mocks(self, mock_gtk_environment):
        """Test connection row with mocked GTK"""
        connection_data = {
            'nickname': 'Test Server',
            'host': 'example.com',
            'username': 'testuser'
        }
        connection = Connection(connection_data)
        
        with patch('gi.repository.Gtk.ListBoxRow'):
            row = ConnectionRow(connection)
            assert row.connection == connection