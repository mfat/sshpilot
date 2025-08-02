"""
Connection Manager for sshPilot
Handles SSH connections, configuration, and secure password storage
"""

import os
import logging
import configparser
from typing import Dict, List, Optional, Any

import secretstorage
import paramiko
from gi.repository import GObject, GLib

logger = logging.getLogger(__name__)

class Connection:
    """Represents an SSH connection"""
    
    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.is_connected = False
        self.client = None
        self.nickname = data.get('nickname', data.get('host', 'Unknown'))
        self.host = data.get('host', '')
        self.username = data.get('username', '')
        self.port = data.get('port', 22)
        self.keyfile = data.get('keyfile', '')
        self.password = data.get('password', '')
        self.key_passphrase = data.get('key_passphrase', '')
        self.auth_method = data.get('auth_method', 0)  # 0=key-based, 1=password
        self.x11_forwarding = data.get('x11_forwarding', False)
        
        # Port forwarding settings
        self.port_forwarding_enabled = data.get('port_forwarding_enabled', False)
        self.tunnel_type = data.get('tunnel_type', '')  # local, remote, or dynamic
        self.tunnel_port = data.get('tunnel_port', None)
        
        # Local forwarding
        self.local_forward = data.get('local_forward', {
            'enabled': False,
            'listen_addr': 'localhost',
            'listen_port': 8080,
            'dest_addr': 'localhost',
            'dest_port': 80
        })
        
        # Remote forwarding
        self.remote_forward = data.get('remote_forward', {
            'enabled': False,
            'listen_addr': 'localhost',
            'listen_port': 8080,
            'dest_addr': 'localhost',
            'dest_port': 80
        })
        
        # Dynamic forwarding
        self.dynamic_forward = data.get('dynamic_forward', {
            'enabled': False,
            'listen_addr': 'localhost',
            'listen_port': 1080
        })

    def __str__(self):
        return f"{self.nickname} ({self.username}@{self.host})"

class ConnectionManager(GObject.Object):
    """Manages SSH connections and configuration"""
    
    __gsignals__ = {
        'connection-added': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'connection-removed': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'connection-updated': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'connection-status-changed': (GObject.SignalFlags.RUN_FIRST, None, (object, bool)),
    }

    def __init__(self):
        super().__init__()
        self.connections: List[Connection] = []
        self.ssh_config = paramiko.SSHConfig()
        self.ssh_config_path = os.path.expanduser('~/.ssh/config')
        
        # Initialize secure storage
        try:
            self.bus = secretstorage.dbus_init()
            self.collection = secretstorage.get_default_collection(self.bus)
            logger.info("Secure storage initialized")
        except Exception as e:
            logger.warning(f"Failed to initialize secure storage: {e}")
            self.collection = None
        
        # Load existing connections
        self.load_ssh_config()
        self.load_ssh_keys()

    def load_ssh_config(self):
        """Load connections from SSH config file"""
        if not os.path.exists(self.ssh_config_path):
            logger.info("SSH config file not found, creating empty one")
            os.makedirs(os.path.dirname(self.ssh_config_path), exist_ok=True)
            with open(self.ssh_config_path, 'w') as f:
                f.write("# SSH configuration file\n")
            return

        try:
            with open(self.ssh_config_path, 'r') as f:
                self.ssh_config.parse(f)
            
            # Parse hosts from config
            self.connections.clear()
            for host in self.ssh_config.get_hostnames():
                if host != '*':  # Skip wildcard entries
                    host_data = self.parse_host(host)
                    if host_data:
                        connection = Connection(host_data)
                        self.connections.append(connection)
                        logger.debug(f"Loaded connection: {connection}")
            
            logger.info(f"Loaded {len(self.connections)} connections from SSH config")
            
        except Exception as e:
            logger.error(f"Failed to load SSH config: {e}")

    def parse_host(self, host: str) -> Optional[Dict[str, Any]]:
        """Parse host configuration from SSH config"""
        try:
            host_data = self.ssh_config.lookup(host)
            
            # Extract relevant configuration
            parsed = {
                'nickname': host,
                'host': host_data.get('hostname', host),
                'username': host_data.get('user', os.getenv('USER')),
                'port': int(host_data.get('port', 22)),
                'keyfile': host_data.get('identityfile', [''])[0] if host_data.get('identityfile') else '',
                'x11_forwarding': host_data.get('forwardx11', 'no').lower() == 'yes',
            }
            
            # Parse tunnel configuration
            if 'localforward' in host_data:
                parsed['tunnel_type'] = 'local'
                parsed['tunnel_port'] = host_data['localforward'][0].split(':')[0]
            elif 'remoteforward' in host_data:
                parsed['tunnel_type'] = 'remote'
                parsed['tunnel_port'] = host_data['remoteforward'][0].split(':')[0]
            elif 'dynamicforward' in host_data:
                parsed['tunnel_type'] = 'dynamic'
                parsed['tunnel_port'] = host_data['dynamicforward'][0]
            
            return parsed
            
        except Exception as e:
            logger.error(f"Failed to parse host {host}: {e}")
            return None

    def load_ssh_keys(self):
        """Auto-detect SSH keys in ~/.ssh/"""
        ssh_dir = os.path.expanduser('~/.ssh')
        if not os.path.exists(ssh_dir):
            return
        
        try:
            keys = []
            for filename in os.listdir(ssh_dir):
                if filename.endswith('.pub'):
                    private_key = os.path.join(ssh_dir, filename[:-4])
                    if os.path.exists(private_key):
                        keys.append(private_key)
            
            logger.info(f"Found {len(keys)} SSH keys: {keys}")
            return keys
            
        except Exception as e:
            logger.error(f"Failed to load SSH keys: {e}")
            return []

    def store_password(self, host: str, username: str, password: str):
        """Store password securely in system keyring"""
        if not self.collection:
            logger.warning("Secure storage not available, password not stored")
            return False
        
        try:
            attributes = {
                'application': 'sshPilot',
                'host': host,
                'username': username
            }
            
            # Delete existing password if any
            existing_items = list(self.collection.search_items(attributes))
            for item in existing_items:
                item.delete()
            
            # Store new password
            self.collection.create_item(
                f'sshPilot: {username}@{host}',
                attributes,
                password.encode()
            )
            
            logger.debug(f"Password stored for {username}@{host}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to store password: {e}")
            return False

    def get_password(self, host: str, username: str) -> Optional[str]:
        """Retrieve password from system keyring"""
        if not self.collection:
            return None
        
        try:
            attributes = {
                'application': 'sshPilot',
                'host': host,
                'username': username
            }
            
            items = list(self.collection.search_items(attributes))
            if items:
                password = items[0].get_secret().decode()
                logger.debug(f"Password retrieved for {username}@{host}")
                return password
                
        except Exception as e:
            logger.error(f"Failed to retrieve password: {e}")
        
        return None

    def save_connection(self, connection_data: Dict[str, Any]) -> bool:
        """Save connection to SSH config file"""
        try:
            # Add to SSH config
            config_entry = self.format_ssh_config_entry(connection_data)
            
            with open(self.ssh_config_path, 'a') as f:
                f.write(f"\n{config_entry}")
            
            # Store password if provided
            if connection_data.get('password'):
                self.store_password(
                    connection_data['host'],
                    connection_data['username'],
                    connection_data['password']
                )
            
            # Create connection object and add to list
            connection = Connection(connection_data)
            self.connections.append(connection)
            
            # Emit signal
            self.emit('connection-added', connection)
            
            logger.info(f"Connection saved: {connection}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save connection: {e}")
            return False

    def format_ssh_config_entry(self, data: Dict[str, Any]) -> str:
        """Format connection data as SSH config entry"""
        lines = [f"Host {data['nickname']}"]
        lines.append(f"    HostName {data['host']}")
        lines.append(f"    User {data['username']}")
        
        if data.get('port', 22) != 22:
            lines.append(f"    Port {data['port']}")
        
        if data.get('keyfile'):
            lines.append(f"    IdentityFile {data['keyfile']}")
        
        if data.get('x11_forwarding'):
            lines.append("    ForwardX11 yes")
        
        # Add tunnel configuration
        tunnel_type = data.get('tunnel_type')
        tunnel_port = data.get('tunnel_port')
        
        if tunnel_type and tunnel_port:
            if tunnel_type == 'local':
                lines.append(f"    LocalForward {tunnel_port} localhost:{tunnel_port}")
            elif tunnel_type == 'remote':
                lines.append(f"    RemoteForward {tunnel_port} localhost:{tunnel_port}")
            elif tunnel_type == 'dynamic':
                lines.append(f"    DynamicForward {tunnel_port}")
        
        return '\n'.join(lines)

    def update_ssh_config_file(self, connection: Connection, new_data: Dict[str, Any]):
        """Update SSH config file with new connection data"""
        try:
            if not os.path.exists(self.ssh_config_path):
                return
            
            # Read current config
            with open(self.ssh_config_path, 'r') as f:
                lines = f.readlines()
            
            # Find and update the connection's Host block
            updated_lines = []
            in_target_host = False
            host_nickname = connection.nickname
            
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                
                # Check if this is the start of our target host block
                if line.startswith('Host ') and line.split()[1] == host_nickname:
                    in_target_host = True
                    # Write the updated host block
                    updated_config = self.format_ssh_config_entry(new_data)
                    updated_lines.append(updated_config + '\n')
                    
                    # Skip the old host block
                    i += 1
                    while i < len(lines) and not lines[i].strip().startswith('Host '):
                        i += 1
                    continue
                
                # If we're not in our target host, keep the line
                updated_lines.append(lines[i])
                i += 1
            
            # Write back to file
            with open(self.ssh_config_path, 'w') as f:
                f.writelines(updated_lines)
                
            logger.debug(f"Updated SSH config for {host_nickname}")
            
        except Exception as e:
            logger.error(f"Failed to update SSH config file: {e}")

    def update_connection(self, connection: Connection, new_data: Dict[str, Any]) -> bool:
        """Update an existing connection"""
        try:
            # Update connection data
            connection.data.update(new_data)
            
            # Update the SSH config file
            self.update_ssh_config_file(connection, new_data)
            
            # Store password if provided
            if new_data.get('password'):
                self.store_password(
                    new_data['host'],
                    new_data['username'],
                    new_data['password']
                )
            
            # Reload SSH config to reflect changes
            self.load_ssh_config()
            
            # Emit signal
            self.emit('connection-updated', connection)
            
            logger.info(f"Connection updated: {connection}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update connection: {e}")
            return False

    def remove_connection(self, connection: Connection) -> bool:
        """Remove connection from config and list"""
        try:
            # Remove from list
            if connection in self.connections:
                self.connections.remove(connection)
            
            # Remove password from keyring
            if self.collection:
                try:
                    attributes = {
                        'application': 'sshPilot',
                        'host': connection.host,
                        'username': connection.username
                    }
                    items = list(self.collection.search_items(attributes))
                    for item in items:
                        item.delete()
                except Exception as e:
                    logger.warning(f"Failed to remove password from keyring: {e}")
            
            # TODO: Remove from SSH config file (requires config rewriting)
            
            # Emit signal
            self.emit('connection-removed', connection)
            
            logger.info(f"Connection removed: {connection}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to remove connection: {e}")
            return False

    def connect_ssh(self, connection: Connection) -> Optional[paramiko.SSHClient]:
        """Establish SSH connection"""
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # Prepare connection parameters
            connect_kwargs = {
                'hostname': connection.host,
                'port': connection.port,
                'username': connection.username,
                'timeout': 30,
            }
            
            # Handle authentication based on method
            auth_method = getattr(connection, 'auth_method', 0)
            
            if auth_method == 1:  # Password authentication
                # Use password from connection or stored password
                password = connection.password or self.get_password(connection.host, connection.username)
                if password:
                    connect_kwargs['password'] = password
                    logger.debug("Using password authentication")
                else:
                    logger.warning("Password authentication selected but no password available")
                
                # Disable key-based methods for password-only auth
                connect_kwargs['allow_agent'] = False
                connect_kwargs['look_for_keys'] = False
                
            else:  # Key-based authentication (default)
                # Use key file if specified, otherwise auto-detect
                if connection.keyfile and os.path.exists(connection.keyfile):
                    connect_kwargs['key_filename'] = connection.keyfile
                    logger.debug(f"Using key file: {connection.keyfile}")
                    
                    # Add key passphrase if provided
                    if hasattr(connection, 'key_passphrase') and connection.key_passphrase:
                        connect_kwargs['passphrase'] = connection.key_passphrase
                        logger.debug("Using key passphrase")
                else:
                    logger.debug("Using auto-detected keys")
                
                # Enable SSH agent and key lookup for auto-detection
                connect_kwargs['allow_agent'] = True
                connect_kwargs['look_for_keys'] = True
            
            # Connect
            client.connect(**connect_kwargs)
            
            # Set up tunneling if configured
            if connection.tunnel_type and connection.tunnel_port:
                self.setup_tunnel(client, connection)
            
            connection.client = client
            connection.is_connected = True
            
            # Emit status change signal
            self.emit('connection-status-changed', connection, True)
            
            logger.info(f"Connected to {connection}")
            return client
            
        except Exception as e:
            logger.error(f"Failed to connect to {connection}: {e}")
            if 'client' in locals():
                client.close()
            return None

    def setup_tunnel(self, client: paramiko.SSHClient, connection: Connection):
        """Set up SSH tunnel"""
        try:
            transport = client.get_transport()
            port = int(connection.tunnel_port)
            
            if connection.tunnel_type == 'local':
                transport.request_port_forward('', port)
                logger.info(f"Local tunnel established on port {port}")
            elif connection.tunnel_type == 'remote':
                transport.request_port_forward('', port, connection.host, port)
                logger.info(f"Remote tunnel established on port {port}")
            elif connection.tunnel_type == 'dynamic':
                # Dynamic forwarding requires more complex setup
                logger.info(f"Dynamic tunnel requested on port {port}")
                
        except Exception as e:
            logger.error(f"Failed to set up tunnel: {e}")

    def disconnect(self, connection: Connection):
        """Disconnect from SSH host"""
        try:
            if connection.client:
                connection.client.close()
                connection.client = None
            
            connection.is_connected = False
            
            # Emit status change signal
            self.emit('connection-status-changed', connection, False)
            
            logger.info(f"Disconnected from {connection}")
            
        except Exception as e:
            logger.error(f"Failed to disconnect from {connection}: {e}")

    def get_connections(self) -> List[Connection]:
        """Get list of all connections"""
        return self.connections.copy()

    def find_connection_by_nickname(self, nickname: str) -> Optional[Connection]:
        """Find connection by nickname"""
        for connection in self.connections:
            if connection.nickname == nickname:
                return connection
        return None