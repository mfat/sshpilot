"""
SSH Key Manager for sshPilot
Handles SSH key generation, management, and deployment
"""

import os
import stat
import logging
import subprocess
from typing import List, Dict, Optional, Tuple
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
from cryptography.hazmat.backends import default_backend

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GObject, GLib

import paramiko

logger = logging.getLogger(__name__)

class SSHKey:
    """Represents an SSH key pair"""
    
    def __init__(self, path: str, key_type: str = None, comment: str = None):
        self.path = path
        self.public_path = f"{path}.pub"
        self.key_type = key_type
        self.comment = comment
        self.fingerprint = None
        self.bits = None
        
        # Load key information
        self.load_key_info()

    def load_key_info(self):
        """Load key information from files"""
        try:
            if os.path.exists(self.public_path):
                with open(self.public_path, 'r') as f:
                    pub_key_content = f.read().strip()
                
                # Parse public key
                parts = pub_key_content.split()
                if len(parts) >= 2:
                    self.key_type = parts[0]
                    if len(parts) >= 3:
                        self.comment = parts[2]
                
                # Get fingerprint using ssh-keygen
                try:
                    result = subprocess.run(
                        ['ssh-keygen', '-lf', self.public_path],
                        capture_output=True,
                        text=True,
                        check=True
                    )
                    
                    # Parse output: "2048 SHA256:... comment (RSA)"
                    output_parts = result.stdout.strip().split()
                    if len(output_parts) >= 3:
                        self.bits = int(output_parts[0])
                        self.fingerprint = output_parts[1]
                        
                except subprocess.CalledProcessError as e:
                    logger.warning(f"Failed to get fingerprint for {self.path}: {e}")
        
        except Exception as e:
            logger.error(f"Failed to load key info for {self.path}: {e}")

    def exists(self) -> bool:
        """Check if key files exist"""
        return os.path.exists(self.path) and os.path.exists(self.public_path)

    def get_public_key_content(self) -> Optional[str]:
        """Get public key content"""
        try:
            if os.path.exists(self.public_path):
                with open(self.public_path, 'r') as f:
                    return f.read().strip()
        except Exception as e:
            logger.error(f"Failed to read public key {self.public_path}: {e}")
        return None

    def __str__(self):
        return f"{os.path.basename(self.path)} ({self.key_type}, {self.bits} bits)"

class KeyManager(GObject.Object):
    """SSH key management"""
    
    __gsignals__ = {
        'key-generated': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'key-deleted': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'key-deployed': (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
    }
    
    def __init__(self):
        super().__init__()
        self.ssh_dir = Path.home() / '.ssh'
        self.ssh_dir.mkdir(mode=0o700, exist_ok=True)
        
        # Ensure proper permissions on .ssh directory
        os.chmod(self.ssh_dir, 0o700)

    def discover_keys(self) -> List[SSHKey]:
        """Discover existing SSH keys"""
        keys = []
        
        try:
            if not self.ssh_dir.exists():
                return keys
            
            # Look for private key files (list all plausible private keys, even without .pub)
            for file_path in self.ssh_dir.iterdir():
                if file_path.is_file() and not file_path.name.endswith('.pub'):
                    # Skip known non-key files
                    if file_path.name in ['config', 'known_hosts', 'authorized_keys']:
                        continue
                    
                    # Check if corresponding .pub file exists
                    pub_path = file_path.with_suffix(file_path.suffix + '.pub')
                    if pub_path.exists():
                        try:
                            # Try to load as SSH key
                            key = SSHKey(str(file_path))
                            if key.key_type:  # Valid key
                                keys.append(key)
                        except Exception as e:
                            logger.debug(f"Skipping {file_path}: {e}")
            
            logger.info(f"Discovered {len(keys)} SSH keys")
            
        except Exception as e:
            logger.error(f"Failed to discover SSH keys: {e}")
        
        return keys

    def generate_key(self, 
                    key_name: str, 
                    key_type: str = 'rsa',
                    key_size: int = 2048,
                    comment: str = None,
                    passphrase: str = None) -> Optional[SSHKey]:
        """Generate a new SSH key pair"""
        
        try:
            key_path = self.ssh_dir / key_name
            
            # Check if key already exists
            if key_path.exists():
                logger.error(f"Key {key_name} already exists")
                return None
            
            # Generate key based on type
            if key_type.lower() == 'rsa':
                private_key = rsa.generate_private_key(
                    public_exponent=65537,
                    key_size=key_size,
                    backend=default_backend()
                )
            elif key_type.lower() == 'ed25519':
                private_key = ed25519.Ed25519PrivateKey.generate()
            else:
                logger.error(f"Unsupported key type: {key_type}")
                return None
            
            # Prepare encryption
            if passphrase:
                encryption_algorithm = serialization.BestAvailableEncryption(
                    passphrase.encode('utf-8')
                )
            else:
                encryption_algorithm = serialization.NoEncryption()
            
            # Write private key
            private_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.OpenSSH,
                encryption_algorithm=encryption_algorithm
            )
            
            with open(key_path, 'wb') as f:
                f.write(private_pem)
            
            # Set proper permissions for private key
            os.chmod(key_path, 0o600)
            
            # Generate public key
            public_key = private_key.public_key()
            public_ssh = public_key.public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH
            )
            
            # Add comment if provided
            if comment:
                public_ssh += f' {comment}'.encode('utf-8')
            
            # Write public key
            pub_path = key_path.with_suffix('.pub')
            with open(pub_path, 'wb') as f:
                f.write(public_ssh)
            
            # Set proper permissions for public key
            os.chmod(pub_path, 0o644)
            
            # Create SSHKey object
            ssh_key = SSHKey(str(key_path), key_type, comment)
            
            # Emit signal
            self.emit('key-generated', ssh_key)
            
            logger.info(f"Generated SSH key: {ssh_key}")
            return ssh_key
            
        except Exception as e:
            logger.error(f"Failed to generate SSH key: {e}")
            return None

    def generate_key_with_ssh_keygen(self,
                                   key_name: str,
                                   key_type: str = 'rsa',
                                   key_size: int = 2048,
                                   comment: str = None,
                                   passphrase: str = None) -> Optional[SSHKey]:
        """Generate SSH key using ssh-keygen command"""
        
        try:
            key_path = self.ssh_dir / key_name
            
            # Check if key already exists
            if key_path.exists():
                logger.error(f"Key {key_name} already exists")
                return None
            
            # Build ssh-keygen command
            cmd = ['ssh-keygen', '-t', key_type]
            
            if key_type.lower() == 'rsa':
                cmd.extend(['-b', str(key_size)])
            
            if comment:
                cmd.extend(['-C', comment])
            else:
                cmd.extend(['-C', f'{os.getenv("USER")}@{os.uname().nodename}'])
            
            cmd.extend(['-f', str(key_path)])
            
            if passphrase:
                cmd.extend(['-N', passphrase])
            else:
                cmd.extend(['-N', ''])  # No passphrase
            
            # Run ssh-keygen
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )
            
            logger.debug(f"ssh-keygen output: {result.stdout}")
            
            # Create SSHKey object
            ssh_key = SSHKey(str(key_path))
            
            # Emit signal
            self.emit('key-generated', ssh_key)
            
            logger.info(f"Generated SSH key with ssh-keygen: {ssh_key}")
            return ssh_key
            
        except subprocess.CalledProcessError as e:
            logger.error(f"ssh-keygen failed: {e.stderr}")
            return None
        except Exception as e:
            logger.error(f"Failed to generate SSH key with ssh-keygen: {e}")
            return None

    def delete_key(self, ssh_key: SSHKey) -> bool:
        """Delete an SSH key pair"""
        try:
            key_path = Path(ssh_key.path)
            pub_path = Path(ssh_key.public_path)
            
            # Remove files
            if key_path.exists():
                key_path.unlink()
            
            if pub_path.exists():
                pub_path.unlink()
            
            # Emit signal
            self.emit('key-deleted', ssh_key.path)
            
            logger.info(f"Deleted SSH key: {ssh_key}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete SSH key {ssh_key}: {e}")
            return False

    def deploy_key_to_host(self, ssh_key: SSHKey, connection, connection_manager=None) -> bool:
        """Deploy SSH key to remote host"""
        try:
            # Get public key content
            public_key_content = ssh_key.get_public_key_content()
            if not public_key_content:
                logger.error(f"Could not read public key: {ssh_key.public_path}")
                return False
            
            # Connect to host
            if connection_manager is None:
                logger.error("Connection manager is required")
                return False
            client = connection_manager.connect_ssh(connection)
            if not client:
                logger.error(f"Could not connect to {connection}")
                return False
            
            try:
                # Create .ssh directory if it doesn't exist
                stdin, stdout, stderr = client.exec_command('mkdir -p ~/.ssh && chmod 700 ~/.ssh')
                stdin.close()
                
                # Add public key to authorized_keys
                command = (
                    f'echo "{public_key_content}" >> ~/.ssh/authorized_keys && '
                    'chmod 600 ~/.ssh/authorized_keys && '
                    'sort ~/.ssh/authorized_keys | uniq > ~/.ssh/authorized_keys.tmp && '
                    'mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys'
                )
                
                stdin, stdout, stderr = client.exec_command(command)
                stdin.close()
                
                # Check for errors
                error_output = stderr.read().decode().strip()
                if error_output:
                    logger.warning(f"SSH key deployment warning: {error_output}")
                
                # Emit signal
                self.emit('key-deployed', ssh_key, connection)
                
                logger.info(f"Deployed SSH key {ssh_key} to {connection}")
                return True
                
            finally:
                client.close()
                
        except Exception as e:
            logger.error(f"Failed to deploy SSH key {ssh_key} to {connection}: {e}")
            return False

    def copy_key_to_host(self, ssh_key: SSHKey, connection) -> bool:
        """Copy SSH key to host using ssh-copy-id"""
        try:
            # Use ssh-copy-id command
            cmd = [
                'ssh-copy-id',
                '-i', ssh_key.public_path,
                f'{connection.username}@{connection.host}'
            ]
            
            if connection.port != 22:
                cmd.extend(['-p', str(connection.port)])
            
            # Run ssh-copy-id
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                input='\n',  # Accept host key if prompted
                timeout=30
            )
            
            if result.returncode == 0:
                # Emit signal
                self.emit('key-deployed', ssh_key, connection)
                
                logger.info(f"Copied SSH key {ssh_key} to {connection} using ssh-copy-id")
                return True
            else:
                logger.error(f"ssh-copy-id failed: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            logger.error("ssh-copy-id timed out")
            return False
        except Exception as e:
            logger.error(f"Failed to copy SSH key using ssh-copy-id: {e}")
            return False

    def get_key_info(self, key_path: str) -> Optional[Dict[str, str]]:
        """Get detailed information about an SSH key"""
        try:
            result = subprocess.run(
                ['ssh-keygen', '-lf', key_path],
                capture_output=True,
                text=True,
                check=True
            )
            
            # Parse output
            output = result.stdout.strip()
            parts = output.split()
            
            if len(parts) >= 4:
                return {
                    'bits': parts[0],
                    'fingerprint': parts[1],
                    'comment': ' '.join(parts[2:-1]),
                    'type': parts[-1].strip('()')
                }
                
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get key info for {key_path}: {e}")
        
        return None

    def change_key_passphrase(self, ssh_key: SSHKey, old_passphrase: str = None, new_passphrase: str = None) -> bool:
        """Change SSH key passphrase"""
        try:
            cmd = ['ssh-keygen', '-p', '-f', ssh_key.path]
            
            # Prepare input
            input_data = ""
            if old_passphrase:
                input_data += f"{old_passphrase}\n"
            else:
                input_data += "\n"  # No old passphrase
            
            if new_passphrase:
                input_data += f"{new_passphrase}\n{new_passphrase}\n"
            else:
                input_data += "\n\n"  # No new passphrase
            
            # Run ssh-keygen
            result = subprocess.run(
                cmd,
                input=input_data,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                logger.info(f"Changed passphrase for SSH key: {ssh_key}")
                return True
            else:
                logger.error(f"Failed to change passphrase: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            logger.error("Passphrase change timed out")
            return False
        except Exception as e:
            logger.error(f"Failed to change key passphrase: {e}")
            return False

    def add_key_to_agent(self, ssh_key: SSHKey, passphrase: str = None) -> bool:
        """Add SSH key to ssh-agent"""
        try:
            cmd = ['ssh-add', ssh_key.path]
            
            # Prepare input (passphrase if needed)
            input_data = f"{passphrase}\n" if passphrase else None
            
            # Run ssh-add
            result = subprocess.run(
                cmd,
                input=input_data,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                logger.info(f"Added SSH key to agent: {ssh_key}")
                return True
            else:
                logger.error(f"Failed to add key to agent: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            logger.error("ssh-add timed out")
            return False
        except Exception as e:
            logger.error(f"Failed to add key to agent: {e}")
            return False

    def remove_key_from_agent(self, ssh_key: SSHKey) -> bool:
        """Remove SSH key from ssh-agent"""
        try:
            cmd = ['ssh-add', '-d', ssh_key.path]
            
            # Run ssh-add
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                logger.info(f"Removed SSH key from agent: {ssh_key}")
                return True
            else:
                logger.error(f"Failed to remove key from agent: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            logger.error("ssh-add removal timed out")
            return False
        except Exception as e:
            logger.error(f"Failed to remove key from agent: {e}")
            return False

    def list_agent_keys(self) -> List[str]:
        """List keys currently loaded in ssh-agent"""
        try:
            result = subprocess.run(
                ['ssh-add', '-l'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                keys = []
                for line in result.stdout.strip().split('\n'):
                    if line.strip():
                        keys.append(line.strip())
                return keys
            else:
                # No keys in agent
                return []
                
        except subprocess.TimeoutExpired:
            logger.error("ssh-add list timed out")
            return []
        except Exception as e:
            logger.error(f"Failed to list agent keys: {e}")
            return []

    def validate_key_file(self, key_path: str) -> bool:
        """Validate if a file is a valid SSH private key"""
        try:
            # Try to load the key
            with open(key_path, 'rb') as f:
                key_data = f.read()
            
            # Try to parse as SSH private key
            try:
                serialization.load_ssh_private_key(key_data, password=None)
                return True
            except ValueError:
                # Try with PEM format
                try:
                    serialization.load_pem_private_key(key_data, password=None)
                    return True
                except ValueError:
                    return False
                    
        except Exception as e:
            logger.debug(f"Key validation failed for {key_path}: {e}")
            return False