"""
Unit tests for SSH port forwarding functionality.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock, call
import paramiko

# Mock GTK dependencies before importing the module
sys.modules['gi'] = MagicMock()
sys.modules['gi.repository'] = MagicMock()
sys.modules['gi.repository.GLib'] = MagicMock()
sys.modules['gi.repository.GObject'] = MagicMock()

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock secretstorage module
sys.modules['secretstorage'] = MagicMock()

# Import the modules to test
with patch('gi.repository.GObject.GObject'):
    from src.sshpilot_pkg.github.mfat.sshpilot.connection_manager import Connection, ConnectionManager


class TestPortForwarding(unittest.TestCase):
    """Test cases for port forwarding functionality."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a test connection
        self.connection_data = {
            'nickname': 'test-connection',
            'host': 'test.example.com',
            'username': 'testuser',
            'port': 22,
            'forwarding_rules': [
                {
                    'type': 'local',
                    'enabled': True,
                    'listen_addr': '127.0.0.1',
                    'listen_port': 8080,
                    'remote_host': 'localhost',
                    'remote_port': 80
                },
                {
                    'type': 'remote',
                    'enabled': True,
                    'listen_addr': '0.0.0.0',
                    'listen_port': 2222,
                    'remote_host': 'localhost',
                    'remote_port': 22
                },
                {
                    'type': 'dynamic',
                    'enabled': True,
                    'listen_addr': '127.0.0.1',
                    'listen_port': 1080,
                    'remote_host': '',
                    'remote_port': 0
                }
            ]
        }
        
        # Create a real Connection object with our test data
        self.connection = Connection(self.connection_data)
        
        # Mock the SSH client and transport
        self.mock_client = MagicMock(spec=paramiko.SSHClient)
        self.mock_transport = MagicMock()
        self.mock_client.get_transport.return_value = self.mock_transport
        
        # Create a real ConnectionManager instance with mocked dependencies
        with patch('gi.repository.GObject.GObject'):
            self.connection_manager = ConnectionManager()
        
        # Mock the connect_ssh method to return our mock client
        self.connection_manager.connect_ssh = MagicMock(return_value=self.mock_client)
        
        # Mock the emit method to avoid GTK dependencies
        self.connection_manager.emit = MagicMock()
        
    def _mock_setup_tunnel(self, client, connection):
        """Mock implementation of setup_tunnel that calls the real method with mocks."""
        # Create a real ConnectionManager instance for testing
        real_manager = ConnectionManager()
        # Mock the emit method to avoid GTK dependencies
        real_manager.emit = MagicMock()
        # Call the real method with our test data
        return real_manager.setup_tunnel(client, connection)
    
    def tearDown(self):
        """Clean up after tests."""
        self.connect_patcher.stop()
    
    def test_setup_tunnel_local_forwarding(self):
        """Test setting up a local port forwarding rule."""
        # Create a connection with only local forwarding
        conn_data = self.connection_data.copy()
        conn_data['forwarding_rules'] = [conn_data['forwarding_rules'][0]]
        connection = Connection(conn_data)
        
        # Reset mock call history
        self.mock_transport.request_port_forward.reset_mock()
        self.mock_transport.open_channel.reset_mock()
        
        # Call the method under test
        errors = self.connection_manager.setup_tunnel(self.mock_client, connection)
        
        # Verify the results
        self.assertEqual(len(errors), 0, f"Expected no errors, got: {errors}")
        self.mock_transport.request_port_forward.assert_called_once_with('127.0.0.1', 8080)
        self.mock_transport.open_channel.assert_called_once_with(
            'direct-tcpip',
            ('localhost', 80),
            ('127.0.0.1', 8080)
        )
        
        # Verify active tunnels were updated
        self.assertEqual(len(connection.active_tunnels), 1)
        self.assertEqual(connection.active_tunnels[0][0], 'local')
        self.assertEqual(connection.active_tunnels[0][1], '127.0.0.1')
        self.assertEqual(connection.active_tunnels[0][2], 8080)
    
    def test_setup_tunnel_remote_forwarding(self):
        """Test setting up a remote port forwarding rule."""
        # Create a connection with only remote forwarding
        conn_data = self.connection_data.copy()
        conn_data['forwarding_rules'] = [conn_data['forwarding_rules'][1]]
        connection = Connection(conn_data)
        
        # Reset mock call history
        self.mock_transport.request_port_forward.reset_mock()
        self.mock_transport.open_channel.reset_mock()
        
        # Call the method under test
        errors = self.connection_manager.setup_tunnel(self.mock_client, connection)
        
        # Verify the results
        self.assertEqual(len(errors), 0, f"Expected no errors, got: {errors}")
        self.mock_transport.request_port_forward.assert_called_once_with('0.0.0.0', 2222)
        self.mock_transport.open_channel.assert_called_once_with(
            'forwarded-tcpip',
            ('localhost', 22),
            ('0.0.0.0', 2222)
        )
        
        # Verify active tunnels were updated
        self.assertEqual(len(connection.active_tunnels), 1)
        self.assertEqual(connection.active_tunnels[0][0], 'remote')
        self.assertEqual(connection.active_tunnels[0][1], '0.0.0.0')
        self.assertEqual(connection.active_tunnels[0][2], 2222)
    
    def test_setup_tunnel_dynamic_forwarding(self):
        """Test setting up a dynamic port forwarding rule."""
        # Create a connection with only dynamic forwarding
        conn_data = self.connection_data.copy()
        conn_data['forwarding_rules'] = [conn_data['forwarding_rules'][2]]
        connection = Connection(conn_data)
        
        # Reset mock call history
        self.mock_transport.request_port_forward.reset_mock()
        
        # Call the method under test
        errors = self.connection_manager.setup_tunnel(self.mock_client, connection)
        
        # Verify the results
        self.assertEqual(len(errors), 0, f"Expected no errors, got: {errors}")
        self.mock_transport.request_port_forward.assert_called_once_with('127.0.0.1', 1080)
        
        # Verify active tunnels were updated
        self.assertEqual(len(connection.active_tunnels), 1)
        self.assertEqual(connection.active_tunnels[0][0], 'dynamic')
        self.assertEqual(connection.active_tunnels[0][1], '127.0.0.1')
        self.assertEqual(connection.active_tunnels[0][2], 1080)
    
    def test_setup_tunnel_invalid_port(self):
        """Test handling of invalid port numbers."""
        # Create a connection with an invalid port
        conn_data = self.connection_data.copy()
        conn_data['forwarding_rules'] = [{
            'type': 'local',
            'enabled': True,
            'listen_addr': '127.0.0.1',
            'listen_port': 99999,  # Invalid port
            'remote_host': 'localhost',
            'remote_port': 80
        }]
        connection = Connection(conn_data)
        
        # Reset mock call history
        self.mock_transport.request_port_forward.reset_mock()
        self.mock_transport.open_channel.reset_mock()
        
        # Call the method under test
        errors = self.connection_manager.setup_tunnel(self.mock_client, connection)
        
        # Verify the results
        self.assertGreater(len(errors), 0, "Expected an error for invalid port")
        self.assertIn("Invalid port", errors[0])
        
        # Verify no tunnel setup was attempted
        self.mock_transport.request_port_forward.assert_not_called()
        self.mock_transport.open_channel.assert_not_called()
        
        # Verify no active tunnels were added
        self.assertEqual(len(connection.active_tunnels), 0)
    
    def test_setup_tunnel_exception_handling(self):
        """Test exception handling during tunnel setup."""
        # Make the transport raise an exception
        self.mock_transport.request_port_forward.side_effect = Exception("Test error")
        
        # Create a simple connection with one forwarding rule
        conn_data = self.connection_data.copy()
        conn_data['forwarding_rules'] = [conn_data['forwarding_rules'][0]]
        connection = Connection(conn_data)
        
        # Call the method under test
        errors = self.connection_manager.setup_tunnel(self.mock_client, connection)
        
        # Verify the results
        self.assertGreater(len(errors), 0, "Expected an error")
        self.assertIn("Test error", errors[0])
        
        # Verify active tunnels were not updated
        self.assertEqual(len(connection.active_tunnels), 0)


if __name__ == '__main__':
    unittest.main()
