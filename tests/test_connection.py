"""
Tests for SSH connection functionality
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import os
import tempfile

from io.github.mfat.sshpilot.connection_manager import ConnectionManager, Connection


class TestConnection:
    """Test Connection class"""
    
    def test_connection_creation(self):
        """Test creating a connection object"""
        data = {
            'nickname': 'test-server',
            'host': 'example.com',
            'username': 'testuser',
            'port': 22
        }
        
        connection = Connection(data)
        
        assert connection.nickname == 'test-server'
        assert connection.host == 'example.com'
        assert connection.username == 'testuser'
        assert connection.port == 22
        assert not connection.is_connected

    def test_connection_str(self):
        """Test string representation of connection"""
        data = {
            'nickname': 'test-server',
            'host': 'example.com',
            'username': 'testuser'
        }
        
        connection = Connection(data)
        expected = "test-server (testuser@example.com)"
        
        assert str(connection) == expected


class TestConnectionManager:
    """Test ConnectionManager class"""
    
    def setup_method(self):
        """Set up test fixtures"""
        self.manager = ConnectionManager()
    
    def test_manager_initialization(self):
        """Test connection manager initialization"""
        assert self.manager is not None
        assert hasattr(self.manager, 'connections')
        assert isinstance(self.manager.connections, list)
    
    @patch('os.path.exists')
    @patch('builtins.open')
    def test_load_ssh_config_no_file(self, mock_open, mock_exists):
        """Test loading SSH config when file doesn't exist"""
        mock_exists.return_value = False
        
        self.manager.load_ssh_config()
        
        # Should create empty config
        mock_open.assert_called_once()
    
    def test_parse_host_basic(self):
        """Test parsing basic host configuration"""
        # Mock SSH config data
        with patch.object(self.manager.ssh_config, 'lookup') as mock_lookup:
            mock_lookup.return_value = {
                'hostname': 'example.com',
                'user': 'testuser',
                'port': '22'
            }
            
            result = self.manager.parse_host('test-host')
            
            assert result is not None
            assert result['nickname'] == 'test-host'
            assert result['host'] == 'example.com'
            assert result['username'] == 'testuser'
            assert result['port'] == 22
    
    @patch('secretstorage.dbus_init')
    @patch('secretstorage.get_default_collection')
    def test_store_password(self, mock_get_collection, mock_dbus_init):
        """Test storing password in keyring"""
        mock_collection = Mock()
        mock_get_collection.return_value = mock_collection
        
        # Reinitialize manager with mocked keyring
        self.manager.collection = mock_collection
        
        result = self.manager.store_password('example.com', 'testuser', 'password123')
        
        assert result is True
        mock_collection.create_item.assert_called_once()
    
    @patch('secretstorage.dbus_init')
    @patch('secretstorage.get_default_collection')
    def test_get_password(self, mock_get_collection, mock_dbus_init):
        """Test retrieving password from keyring"""
        mock_collection = Mock()
        mock_item = Mock()
        mock_item.get_secret.return_value = b'password123'
        mock_collection.search_items.return_value = [mock_item]
        mock_get_collection.return_value = mock_collection
        
        # Reinitialize manager with mocked keyring
        self.manager.collection = mock_collection
        
        password = self.manager.get_password('example.com', 'testuser')
        
        assert password == 'password123'
    
    def test_get_password_not_found(self):
        """Test retrieving non-existent password"""
        # Mock empty collection
        if self.manager.collection:
            with patch.object(self.manager.collection, 'search_items') as mock_search:
                mock_search.return_value = []
                
                password = self.manager.get_password('example.com', 'testuser')
                
                assert password is None
    
    @patch('paramiko.SSHClient')
    def test_connect_success(self, mock_ssh_client):
        """Test successful SSH connection"""
        mock_client = Mock()
        mock_ssh_client.return_value = mock_client
        
        # Create test connection
        data = {
            'nickname': 'test-server',
            'host': 'example.com',
            'username': 'testuser',
            'port': 22
        }
        connection = Connection(data)
        
        result = self.manager.connect_ssh(connection)
        
        assert result is not None
        mock_client.set_missing_host_key_policy.assert_called_once()
        mock_client.connect.assert_called_once()
    
    @patch('paramiko.SSHClient')
    def test_connect_with_key(self, mock_ssh_client):
        """Test SSH connection with key authentication"""
        mock_client = Mock()
        mock_ssh_client.return_value = mock_client
        
        # Create test connection with key
        data = {
            'nickname': 'test-server',
            'host': 'example.com',
            'username': 'testuser',
            'port': 22,
            'keyfile': '/home/user/.ssh/id_rsa'
        }
        connection = Connection(data)
        
        with patch('os.path.exists', return_value=True):
            result = self.manager.connect_ssh(connection)
        
        assert result is not None
        # Verify key file was used
        call_args = mock_client.connect.call_args
        assert 'key_filename' in call_args.kwargs
    
    @patch('paramiko.SSHClient')
    def test_connect_failure(self, mock_ssh_client):
        """Test SSH connection failure"""
        mock_client = Mock()
        mock_client.connect.side_effect = Exception("Connection failed")
        mock_ssh_client.return_value = mock_client
        
        # Create test connection
        data = {
            'nickname': 'test-server',
            'host': 'example.com',
            'username': 'testuser',
            'port': 22
        }
        connection = Connection(data)
        
        result = self.manager.connect_ssh(connection)
        
        assert result is None
    
    def test_disconnect(self):
        """Test disconnecting from SSH host"""
        # Create test connection with mock client
        data = {
            'nickname': 'test-server',
            'host': 'example.com',
            'username': 'testuser'
        }
        connection = Connection(data)
        connection.client = Mock()
        connection.is_connected = True
        
        self.manager.disconnect(connection)
        
        assert not connection.is_connected
        connection.client.close.assert_called_once()
    
    def test_get_connections(self):
        """Test getting list of connections"""
        # Add test connections
        data1 = {'nickname': 'server1', 'host': 'host1.com', 'username': 'user1'}
        data2 = {'nickname': 'server2', 'host': 'host2.com', 'username': 'user2'}
        
        self.manager.connections = [Connection(data1), Connection(data2)]
        
        connections = self.manager.get_connections()
        
        assert len(connections) == 2
        assert connections[0].nickname == 'server1'
        assert connections[1].nickname == 'server2'
    
    def test_find_connection_by_nickname(self):
        """Test finding connection by nickname"""
        # Add test connection
        data = {'nickname': 'test-server', 'host': 'example.com', 'username': 'testuser'}
        connection = Connection(data)
        self.manager.connections = [connection]
        
        found = self.manager.find_connection_by_nickname('test-server')
        
        assert found is not None
        assert found.nickname == 'test-server'
    
    def test_find_connection_by_nickname_not_found(self):
        """Test finding non-existent connection"""
        found = self.manager.find_connection_by_nickname('non-existent')
        
        assert found is None


@pytest.fixture
def temp_ssh_config():
    """Create temporary SSH config file for testing"""
    content = """
Host test-server
    HostName example.com
    User testuser
    Port 2222
    IdentityFile ~/.ssh/id_rsa

Host another-server
    HostName another.example.com
    User anotheruser
    """
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.config', delete=False) as f:
        f.write(content)
        f.flush()
        yield f.name
    
    os.unlink(f.name)


class TestConnectionManagerIntegration:
    """Integration tests for ConnectionManager"""
    
    def test_load_real_ssh_config(self, temp_ssh_config):
        """Test loading actual SSH config file"""
        manager = ConnectionManager()
        
        # Override config path
        manager.ssh_config_path = temp_ssh_config
        manager.load_ssh_config()
        
        assert len(manager.connections) == 2
        
        # Check first connection
        conn1 = manager.find_connection_by_nickname('test-server')
        assert conn1 is not None
        assert conn1.host == 'example.com'
        assert conn1.username == 'testuser'
        assert conn1.port == 2222
        
        # Check second connection
        conn2 = manager.find_connection_by_nickname('another-server')
        assert conn2 is not None
        assert conn2.host == 'another.example.com'
        assert conn2.username == 'anotheruser'