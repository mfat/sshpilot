"""
Pytest configuration and fixtures for sshPilot tests
"""

import pytest
import os
import sys
from unittest.mock import Mock, patch

# Add src directory to Python path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

@pytest.fixture
def mock_gtk_environment():
    """Mock GTK environment for testing without display"""
    with patch('gi.repository.Gtk'), \
         patch('gi.repository.Adw'), \
         patch('gi.repository.GLib'), \
         patch('gi.repository.Gio'), \
         patch('gi.repository.Vte'), \
         patch('gi.repository.Pango'), \
         patch('gi.repository.Gdk'):
        yield

@pytest.fixture
def mock_ssh_client():
    """Mock SSH client for testing"""
    with patch('paramiko.SSHClient') as mock:
        client = mock.return_value
        client.connect.return_value = None
        client.exec_command.return_value = (Mock(), Mock(), Mock())
        yield client

@pytest.fixture
def mock_keyring():
    """Mock system keyring for testing"""
    with patch('secretstorage.dbus_init'), \
         patch('secretstorage.get_default_collection') as mock_collection:
        collection = Mock()
        mock_collection.return_value = collection
        yield collection

@pytest.fixture
def temp_ssh_config(tmp_path):
    """Create temporary SSH config file"""
    config_content = """
Host test-server
    HostName example.com
    User testuser
    Port 2222
    IdentityFile ~/.ssh/id_rsa

Host another-server
    HostName another.example.com
    User anotheruser
"""
    config_file = tmp_path / "ssh_config"
    config_file.write_text(config_content)
    return str(config_file)

@pytest.fixture
def sample_connection_data():
    """Sample connection data for testing"""
    return {
        'nickname': 'test-server',
        'host': 'example.com',
        'username': 'testuser',
        'port': 22,
        'keyfile': '/home/user/.ssh/id_rsa',
        'x11_forwarding': False,
        'tunnel_type': '',
        'tunnel_port': 0
    }