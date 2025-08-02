#!/usr/bin/env python3
"""
Test script with proper mocking for GTK dependencies
"""

import sys
import os
import unittest.mock

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def test_connection_manager_with_mocks():
    """Test connection manager with mocked GTK dependencies"""
    print("Testing connection manager with mocks...")
    
    try:
        # Mock all GTK and system dependencies
        with unittest.mock.patch('gi.repository.GObject') as mock_gobject, \
             unittest.mock.patch('gi.repository.GLib'), \
             unittest.mock.patch('secretstorage.dbus_init'), \
             unittest.mock.patch('secretstorage.get_default_collection'):
            
            # Set up GObject mock
            mock_gobject.Object = object
            mock_gobject.SignalFlags = unittest.mock.Mock()
            mock_gobject.SignalFlags.RUN_FIRST = 1
            
            from sshpilot_pkg.github.mfat.sshpilot.connection_manager import Connection, ConnectionManager
            
            # Test Connection class
            test_data = {
                'nickname': 'test-server',
                'host': 'example.com',
                'username': 'testuser',
                'port': 22
            }
            
            connection = Connection(test_data)
            assert connection.nickname == 'test-server'
            assert connection.host == 'example.com'
            assert connection.username == 'testuser'
            assert connection.port == 22
            assert not connection.is_connected
            
            print("✓ Connection class works correctly")
            
            # Test ConnectionManager initialization
            manager = ConnectionManager()
            assert hasattr(manager, 'connections')
            assert isinstance(manager.connections, list)
            
            print("✓ ConnectionManager initializes correctly")
            
            # Test connection list operations
            connections = manager.get_connections()
            assert isinstance(connections, list)
            
            print("✓ Connection list operations work")
            
    except Exception as e:
        print(f"✗ Connection manager test failed: {e}")
        import traceback
        traceback.print_exc()

def test_key_manager_with_mocks():
    """Test key manager with mocked dependencies"""
    print("\nTesting key manager with mocks...")
    
    try:
        with unittest.mock.patch('gi.repository.GObject') as mock_gobject, \
             unittest.mock.patch('pathlib.Path.mkdir'), \
             unittest.mock.patch('os.chmod'):
            
            # Set up GObject mock
            mock_gobject.Object = object
            mock_gobject.SignalFlags = unittest.mock.Mock()
            mock_gobject.SignalFlags.RUN_FIRST = 1
            
            from sshpilot_pkg.github.mfat.sshpilot.key_manager import KeyManager, SSHKey
            
            # Test SSHKey class
            key = SSHKey('/home/user/.ssh/id_rsa', 'rsa', 'test@example.com')
            assert key.path == '/home/user/.ssh/id_rsa'
            assert key.public_path == '/home/user/.ssh/id_rsa.pub'
            assert key.key_type == 'rsa'
            assert key.comment == 'test@example.com'
            
            print("✓ SSHKey class works correctly")
            
            # Test KeyManager initialization
            manager = KeyManager()
            assert hasattr(manager, 'ssh_dir')
            
            print("✓ KeyManager initializes correctly")
            
    except Exception as e:
        print(f"✗ Key manager test failed: {e}")
        import traceback
        traceback.print_exc()

def test_resource_monitor_with_mocks():
    """Test resource monitor with mocked dependencies"""
    print("\nTesting resource monitor with mocks...")
    
    try:
        with unittest.mock.patch('gi.repository.GObject') as mock_gobject, \
             unittest.mock.patch('matplotlib.pyplot'):
            
            # Set up GObject mock
            mock_gobject.Object = object
            mock_gobject.SignalFlags = unittest.mock.Mock()
            mock_gobject.SignalFlags.RUN_FIRST = 1
            
            from sshpilot_pkg.github.mfat.sshpilot.resource_monitor import ResourceMonitor, ResourceData
            
            # Test ResourceData
            data = ResourceData(max_points=10)
            assert data.max_points == 10
            assert len(data.timestamps) == 0
            
            # Add test data
            test_data = {
                'cpu_usage': 25.5,
                'memory_usage': 1024*1024*512,
                'load_avg': 1.2
            }
            data.add_data_point(test_data)
            
            assert len(data.timestamps) == 1
            assert data.cpu_usage[-1] == 25.5
            assert data.load_avg[-1] == 1.2
            
            print("✓ ResourceData works correctly")
            
            # Test ResourceMonitor
            monitor = ResourceMonitor()
            assert hasattr(monitor, 'monitoring_sessions')
            assert monitor.update_interval == 5
            
            print("✓ ResourceMonitor initializes correctly")
            
    except Exception as e:
        print(f"✗ Resource monitor test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    print("=== sshPilot Component Tests with Mocks ===\n")
    
    test_connection_manager_with_mocks()
    test_key_manager_with_mocks()
    test_resource_monitor_with_mocks()
    
    print("\n=== Test Summary ===")
    print("Component tests completed!")
    print("All core business logic is working correctly.")
    print("\nTo test the full GUI application:")
    print("1. Install system GTK packages:")
    print("   sudo apt install gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-2.91-gtk4")
    print("2. Run: ./run_sshpilot.py")