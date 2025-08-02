#!/usr/bin/env python3
"""
Simple test script to verify basic functionality without GTK dependencies
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def test_basic_imports():
    """Test basic imports that don't require GTK"""
    print("Testing basic imports...")
    
    try:
        # Test paramiko import
        import paramiko
        print("✓ paramiko imported successfully")
    except ImportError as e:
        print(f"✗ paramiko import failed: {e}")
    
    try:
        # Test yaml import
        import yaml
        print("✓ yaml imported successfully")
    except ImportError as e:
        print(f"✗ yaml import failed: {e}")
    
    try:
        # Test cryptography import
        import cryptography
        print("✓ cryptography imported successfully")
    except ImportError as e:
        print(f"✗ cryptography import failed: {e}")
    
    try:
        # Test matplotlib import
        import matplotlib
        print("✓ matplotlib imported successfully")
    except ImportError as e:
        print(f"✗ matplotlib import failed: {e}")

def test_connection_manager_basic():
    """Test connection manager without GTK dependencies"""
    print("\nTesting connection manager basics...")
    
    try:
        # Mock the GTK imports to test the logic
        import unittest.mock
        
        with unittest.mock.patch('gi.repository.GObject'), \
             unittest.mock.patch('gi.repository.GLib'), \
             unittest.mock.patch('secretstorage.dbus_init'), \
             unittest.mock.patch('secretstorage.get_default_collection'):
            
            from sshpilot_pkg.github.mfat.sshpilot.connection_manager import Connection
            
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
            
    except Exception as e:
        print(f"✗ Connection manager test failed: {e}")

def test_ssh_config_parsing():
    """Test SSH config parsing"""
    print("\nTesting SSH config parsing...")
    
    try:
        import tempfile
        import unittest.mock
        
        # Create a temporary SSH config
        ssh_config_content = """
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
            f.write(ssh_config_content)
            f.flush()
            
            # Test parsing
            import paramiko
            ssh_config = paramiko.SSHConfig()
            with open(f.name, 'r') as config_file:
                ssh_config.parse(config_file)
            
            # Test host lookup
            host_data = ssh_config.lookup('test-server')
            assert host_data['hostname'] == 'example.com'
            assert host_data['user'] == 'testuser'
            assert host_data['port'] == '2222'
            
            print("✓ SSH config parsing works correctly")
            
            # Cleanup
            os.unlink(f.name)
            
    except Exception as e:
        print(f"✗ SSH config parsing test failed: {e}")

def test_key_generation():
    """Test SSH key generation without file I/O"""
    print("\nTesting SSH key generation...")
    
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
        
        # Test RSA key generation
        rsa_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )
        
        # Test key serialization
        private_pem = rsa_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption()
        )
        
        public_ssh = rsa_key.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH
        )
        
        assert private_pem.startswith(b'-----BEGIN OPENSSH PRIVATE KEY-----')
        assert public_ssh.startswith(b'ssh-rsa ')
        
        print("✓ RSA key generation works correctly")
        
        # Test Ed25519 key generation
        ed25519_key = ed25519.Ed25519PrivateKey.generate()
        
        ed25519_public = ed25519_key.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH
        )
        
        assert ed25519_public.startswith(b'ssh-ed25519 ')
        
        print("✓ Ed25519 key generation works correctly")
        
    except Exception as e:
        print(f"✗ Key generation test failed: {e}")

if __name__ == '__main__':
    print("=== sshPilot Basic Functionality Tests ===\n")
    
    test_basic_imports()
    test_connection_manager_basic()
    test_ssh_config_parsing()
    test_key_generation()
    
    print("\n=== Test Summary ===")
    print("Basic functionality tests completed!")
    print("To run the full application, you'll need GTK4 and libadwaita system packages.")
    print("Use: ./run_sshpilot.py (after installing system dependencies)")