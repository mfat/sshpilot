"""
Tests for SSH key management functionality
"""

import pytest
from unittest.mock import Mock, patch, mock_open, MagicMock
import os
import tempfile
from pathlib import Path

from io.github.mfat.sshpilot.key_manager import KeyManager, SSHKey


class TestSSHKey:
    """Test SSHKey class"""
    
    def test_ssh_key_creation(self):
        """Test creating SSHKey object"""
        key_path = '/home/user/.ssh/id_rsa'
        key = SSHKey(key_path, 'rsa', 'test@example.com')
        
        assert key.path == key_path
        assert key.public_path == key_path + '.pub'
        assert key.key_type == 'rsa'
        assert key.comment == 'test@example.com'
    
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('subprocess.run')
    def test_load_key_info(self, mock_run, mock_file, mock_exists):
        """Test loading key information from files"""
        mock_exists.return_value = True
        mock_file.return_value.read.return_value = 'ssh-rsa AAAAB3... test@example.com'
        
        # Mock ssh-keygen output
        mock_result = Mock()
        mock_result.stdout = '2048 SHA256:abc123... test@example.com (RSA)'
        mock_run.return_value = mock_result
        
        key = SSHKey('/home/user/.ssh/id_rsa')
        
        assert key.key_type == 'ssh-rsa'
        assert key.comment == 'test@example.com'
        assert key.bits == 2048
        assert key.fingerprint == 'SHA256:abc123...'
    
    @patch('os.path.exists')
    def test_exists(self, mock_exists):
        """Test checking if key files exist"""
        key = SSHKey('/home/user/.ssh/id_rsa')
        
        # Both files exist
        mock_exists.return_value = True
        assert key.exists() is True
        
        # Files don't exist
        mock_exists.return_value = False
        assert key.exists() is False
    
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_get_public_key_content(self, mock_file, mock_exists):
        """Test getting public key content"""
        mock_exists.return_value = True
        mock_file.return_value.read.return_value = 'ssh-rsa AAAAB3... test@example.com'
        
        key = SSHKey('/home/user/.ssh/id_rsa')
        content = key.get_public_key_content()
        
        assert content == 'ssh-rsa AAAAB3... test@example.com'
    
    def test_str_representation(self):
        """Test string representation of SSH key"""
        key = SSHKey('/home/user/.ssh/id_rsa', 'rsa')
        key.bits = 2048
        
        expected = 'id_rsa (rsa, 2048 bits)'
        assert str(key) == expected


class TestKeyManager:
    """Test KeyManager class"""
    
    def setup_method(self):
        """Set up test fixtures"""
        with patch('pathlib.Path.mkdir'):
            self.manager = KeyManager()
    
    def test_manager_initialization(self):
        """Test KeyManager initialization"""
        assert self.manager is not None
        assert hasattr(self.manager, 'ssh_dir')
        assert isinstance(self.manager.ssh_dir, Path)
    
    @patch('pathlib.Path.iterdir')
    @patch('pathlib.Path.exists')
    def test_discover_keys(self, mock_exists, mock_iterdir):
        """Test discovering existing SSH keys"""
        # Mock SSH directory contents
        mock_files = [
            Mock(name='id_rsa', is_file=lambda: True, suffix=''),
            Mock(name='id_rsa.pub', is_file=lambda: True, suffix='.pub'),
            Mock(name='id_ed25519', is_file=lambda: True, suffix=''),
            Mock(name='id_ed25519.pub', is_file=lambda: True, suffix='.pub'),
            Mock(name='config', is_file=lambda: True, suffix=''),
            Mock(name='known_hosts', is_file=lambda: True, suffix='')
        ]
        
        # Set up path attributes
        for f in mock_files:
            f.name = f.name
        
        mock_iterdir.return_value = mock_files
        mock_exists.return_value = True
        
        with patch('io.github.mfat.sshpilot.key_manager.SSHKey') as mock_ssh_key:
            mock_key = Mock()
            mock_key.key_type = 'ssh-rsa'
            mock_ssh_key.return_value = mock_key
            
            keys = self.manager.discover_keys()
            
            # Should find 2 keys (id_rsa and id_ed25519), excluding config files
            assert len(keys) == 2
    
    @patch('cryptography.hazmat.primitives.asymmetric.rsa.generate_private_key')
    @patch('builtins.open', new_callable=mock_open)
    @patch('os.chmod')
    def test_generate_rsa_key(self, mock_chmod, mock_file, mock_generate):
        """Test generating RSA key"""
        # Mock private key
        mock_private_key = Mock()
        mock_public_key = Mock()
        mock_private_key.public_key.return_value = mock_public_key
        mock_generate.return_value = mock_private_key
        
        # Mock serialization
        mock_private_key.private_bytes.return_value = b'private_key_data'
        mock_public_key.public_bytes.return_value = b'public_key_data'
        
        result = self.manager.generate_key('test_key', 'rsa', 2048, 'test@example.com')
        
        assert result is not None
        assert isinstance(result, SSHKey)
        mock_generate.assert_called_once()
        mock_chmod.assert_called()  # Should set proper permissions
    
    @patch('cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey.generate')
    @patch('builtins.open', new_callable=mock_open)
    @patch('os.chmod')
    def test_generate_ed25519_key(self, mock_chmod, mock_file, mock_generate):
        """Test generating Ed25519 key"""
        # Mock private key
        mock_private_key = Mock()
        mock_public_key = Mock()
        mock_private_key.public_key.return_value = mock_public_key
        mock_generate.return_value = mock_private_key
        
        # Mock serialization
        mock_private_key.private_bytes.return_value = b'private_key_data'
        mock_public_key.public_bytes.return_value = b'public_key_data'
        
        result = self.manager.generate_key('test_key', 'ed25519')
        
        assert result is not None
        assert isinstance(result, SSHKey)
        mock_generate.assert_called_once()
    
    def test_generate_key_unsupported_type(self):
        """Test generating key with unsupported type"""
        result = self.manager.generate_key('test_key', 'unsupported')
        
        assert result is None
    
    @patch('pathlib.Path.exists')
    def test_generate_key_already_exists(self, mock_exists):
        """Test generating key when file already exists"""
        mock_exists.return_value = True
        
        result = self.manager.generate_key('existing_key')
        
        assert result is None
    
    @patch('subprocess.run')
    def test_generate_key_with_ssh_keygen(self, mock_run):
        """Test generating key using ssh-keygen"""
        mock_result = Mock()
        mock_result.stdout = 'Key generated successfully'
        mock_run.return_value = mock_result
        
        with patch('pathlib.Path.exists', return_value=False):
            result = self.manager.generate_key_with_ssh_keygen('test_key', 'rsa', 2048)
        
        assert result is not None
        mock_run.assert_called_once()
        
        # Check ssh-keygen command
        call_args = mock_run.call_args[0][0]
        assert 'ssh-keygen' in call_args
        assert '-t' in call_args
        assert 'rsa' in call_args
    
    @patch('pathlib.Path.unlink')
    @patch('pathlib.Path.exists')
    def test_delete_key(self, mock_exists, mock_unlink):
        """Test deleting SSH key"""
        mock_exists.return_value = True
        
        key = SSHKey('/home/user/.ssh/test_key')
        result = self.manager.delete_key(key)
        
        assert result is True
        # Should delete both private and public key files
        assert mock_unlink.call_count == 2
    
    def test_deploy_key_to_host(self):
        """Test deploying key to remote host"""
        # Mock SSH key
        key = Mock()
        key.get_public_key_content.return_value = 'ssh-rsa AAAAB3... test@example.com'
        
        # Mock connection
        connection = Mock()
        connection.manager.connect.return_value = Mock()
        
        # Mock SSH client
        mock_client = Mock()
        mock_stdout = Mock()
        mock_stderr = Mock()
        mock_stderr.read.return_value.decode.return_value = ""
        
        mock_client.exec_command.return_value = (Mock(), mock_stdout, mock_stderr)
        connection.manager.connect.return_value = mock_client
        
        result = self.manager.deploy_key_to_host(key, connection)
        
        assert result is True
        mock_client.exec_command.assert_called()
        mock_client.close.assert_called_once()
    
    def test_deploy_key_no_public_key(self):
        """Test deploying key when public key can't be read"""
        key = Mock()
        key.get_public_key_content.return_value = None
        
        connection = Mock()
        
        result = self.manager.deploy_key_to_host(key, connection)
        
        assert result is False
    
    def test_deploy_key_connection_failed(self):
        """Test deploying key when connection fails"""
        key = Mock()
        key.get_public_key_content.return_value = 'ssh-rsa AAAAB3... test@example.com'
        
        connection = Mock()
        connection.manager.connect.return_value = None
        
        result = self.manager.deploy_key_to_host(key, connection)
        
        assert result is False
    
    @patch('subprocess.run')
    def test_copy_key_to_host(self, mock_run):
        """Test copying key using ssh-copy-id"""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        key = Mock()
        key.public_path = '/home/user/.ssh/id_rsa.pub'
        
        connection = Mock()
        connection.username = 'testuser'
        connection.host = 'example.com'
        connection.port = 22
        
        result = self.manager.copy_key_to_host(key, connection)
        
        assert result is True
        mock_run.assert_called_once()
        
        # Check ssh-copy-id command
        call_args = mock_run.call_args[0][0]
        assert 'ssh-copy-id' in call_args
        assert key.public_path in call_args
    
    @patch('subprocess.run')
    def test_copy_key_to_host_custom_port(self, mock_run):
        """Test copying key with custom port"""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        key = Mock()
        key.public_path = '/home/user/.ssh/id_rsa.pub'
        
        connection = Mock()
        connection.username = 'testuser'
        connection.host = 'example.com'
        connection.port = 2222
        
        result = self.manager.copy_key_to_host(key, connection)
        
        assert result is True
        
        # Check that custom port is included
        call_args = mock_run.call_args[0][0]
        assert '-p' in call_args
        assert '2222' in call_args
    
    @patch('subprocess.run')
    def test_get_key_info(self, mock_run):
        """Test getting key information"""
        mock_result = Mock()
        mock_result.stdout = '2048 SHA256:abc123... test@example.com (RSA)'
        mock_run.return_value = mock_result
        
        info = self.manager.get_key_info('/home/user/.ssh/id_rsa')
        
        assert info is not None
        assert info['bits'] == '2048'
        assert info['fingerprint'] == 'SHA256:abc123...'
        assert info['type'] == 'RSA'
    
    @patch('subprocess.run')
    def test_change_key_passphrase(self, mock_run):
        """Test changing key passphrase"""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        key = Mock()
        key.path = '/home/user/.ssh/id_rsa'
        
        result = self.manager.change_key_passphrase(key, 'old_pass', 'new_pass')
        
        assert result is True
        mock_run.assert_called_once()
        
        # Check ssh-keygen command
        call_args = mock_run.call_args[0][0]
        assert 'ssh-keygen' in call_args
        assert '-p' in call_args
        assert key.path in call_args
    
    @patch('subprocess.run')
    def test_add_key_to_agent(self, mock_run):
        """Test adding key to SSH agent"""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        key = Mock()
        key.path = '/home/user/.ssh/id_rsa'
        
        result = self.manager.add_key_to_agent(key, 'passphrase')
        
        assert result is True
        mock_run.assert_called_once()
        
        # Check ssh-add command
        call_args = mock_run.call_args[0][0]
        assert 'ssh-add' in call_args
        assert key.path in call_args
    
    @patch('subprocess.run')
    def test_remove_key_from_agent(self, mock_run):
        """Test removing key from SSH agent"""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        key = Mock()
        key.path = '/home/user/.ssh/id_rsa'
        
        result = self.manager.remove_key_from_agent(key)
        
        assert result is True
        mock_run.assert_called_once()
        
        # Check ssh-add command
        call_args = mock_run.call_args[0][0]
        assert 'ssh-add' in call_args
        assert '-d' in call_args
        assert key.path in call_args
    
    @patch('subprocess.run')
    def test_list_agent_keys(self, mock_run):
        """Test listing keys in SSH agent"""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = '2048 SHA256:abc123... /home/user/.ssh/id_rsa (RSA)\n' \
                           '256 SHA256:def456... /home/user/.ssh/id_ed25519 (ED25519)\n'
        mock_run.return_value = mock_result
        
        keys = self.manager.list_agent_keys()
        
        assert len(keys) == 2
        assert '2048 SHA256:abc123...' in keys[0]
        assert '256 SHA256:def456...' in keys[1]
    
    @patch('subprocess.run')
    def test_list_agent_keys_empty(self, mock_run):
        """Test listing keys when agent has no keys"""
        mock_result = Mock()
        mock_result.returncode = 1  # ssh-add returns 1 when no keys
        mock_run.return_value = mock_result
        
        keys = self.manager.list_agent_keys()
        
        assert keys == []
    
    @patch('builtins.open', new_callable=mock_open)
    @patch('cryptography.hazmat.primitives.serialization.load_ssh_private_key')
    def test_validate_key_file_ssh(self, mock_load_ssh, mock_file):
        """Test validating SSH private key file"""
        mock_file.return_value.read.return_value = b'ssh_key_data'
        mock_load_ssh.return_value = Mock()  # Valid key
        
        result = self.manager.validate_key_file('/home/user/.ssh/id_rsa')
        
        assert result is True
        mock_load_ssh.assert_called_once()
    
    @patch('builtins.open', new_callable=mock_open)
    @patch('cryptography.hazmat.primitives.serialization.load_ssh_private_key')
    @patch('cryptography.hazmat.primitives.serialization.load_pem_private_key')
    def test_validate_key_file_pem(self, mock_load_pem, mock_load_ssh, mock_file):
        """Test validating PEM private key file"""
        mock_file.return_value.read.return_value = b'pem_key_data'
        mock_load_ssh.side_effect = ValueError("Not SSH format")
        mock_load_pem.return_value = Mock()  # Valid PEM key
        
        result = self.manager.validate_key_file('/home/user/.ssh/id_rsa')
        
        assert result is True
        mock_load_pem.assert_called_once()
    
    @patch('builtins.open', new_callable=mock_open)
    @patch('cryptography.hazmat.primitives.serialization.load_ssh_private_key')
    @patch('cryptography.hazmat.primitives.serialization.load_pem_private_key')
    def test_validate_key_file_invalid(self, mock_load_pem, mock_load_ssh, mock_file):
        """Test validating invalid key file"""
        mock_file.return_value.read.return_value = b'invalid_data'
        mock_load_ssh.side_effect = ValueError("Invalid SSH key")
        mock_load_pem.side_effect = ValueError("Invalid PEM key")
        
        result = self.manager.validate_key_file('/home/user/.ssh/invalid')
        
        assert result is False


class TestKeyManagerIntegration:
    """Integration tests for KeyManager"""
    
    def test_full_key_lifecycle(self):
        """Test complete key lifecycle"""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Override SSH directory
            manager = KeyManager()
            manager.ssh_dir = Path(temp_dir)
            
            # Generate key
            with patch('cryptography.hazmat.primitives.asymmetric.rsa.generate_private_key') as mock_gen:
                mock_private_key = Mock()
                mock_public_key = Mock()
                mock_private_key.public_key.return_value = mock_public_key
                mock_gen.return_value = mock_private_key
                
                # Mock serialization
                mock_private_key.private_bytes.return_value = b'private_key_data'
                mock_public_key.public_bytes.return_value = b'public_key_data'
                
                key = manager.generate_key('test_key', 'rsa', 2048)
                
                assert key is not None
                assert key.path == str(Path(temp_dir) / 'test_key')
            
            # Discover keys
            with patch('io.github.mfat.sshpilot.key_manager.SSHKey') as mock_ssh_key:
                mock_key = Mock()
                mock_key.key_type = 'ssh-rsa'
                mock_ssh_key.return_value = mock_key
                
                keys = manager.discover_keys()
                # Would find the generated key if files actually existed
            
            # Delete key
            with patch('pathlib.Path.exists', return_value=True), \
                 patch('pathlib.Path.unlink') as mock_unlink:
                
                result = manager.delete_key(key)
                assert result is True