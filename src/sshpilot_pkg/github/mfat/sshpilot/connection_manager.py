"""
Connection Manager for sshPilot
Handles SSH connections, configuration, and secure password storage
"""

import os
import asyncio
import logging
import configparser
import getpass
import subprocess
import shlex
import signal
from typing import Dict, List, Optional, Any, Tuple, Union

import secretstorage
from gi.repository import GObject, GLib

# Set up asyncio event loop for GTK integration
if os.name == 'posix':
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, GLib
    
    # Set up the asyncio event loop
    if not hasattr(GLib, 'MainLoop'):
        import asyncio
        import asyncio.events
        import asyncio.base_events
        import asyncio.unix_events
        
        class GLibEventLoopPolicy(asyncio.events.BaseDefaultEventLoopPolicy):
            _loop_factory = asyncio.SelectorEventLoop
            
            def new_event_loop(self):
                return asyncio.unix_events.DefaultEventLoopPolicy.new_event_loop(self)
        
        asyncio.set_event_loop_policy(GLibEventLoopPolicy())

logger = logging.getLogger(__name__)

class Connection:
    """Represents an SSH connection"""
    
    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.is_connected = False
        self.connection = None
        self.forwarders: List[asyncio.Task] = []
        self.listeners: List[asyncio.Server] = []
        
        self.nickname = data.get('nickname', data.get('host', 'Unknown'))
        self.host = data.get('host', '')
        self.username = data.get('username', '')
        self.port = data.get('port', 22)
        self.keyfile = data.get('keyfile', '')
        self.password = data.get('password', '')
        self.key_passphrase = data.get('key_passphrase', '')
        self.auth_method = data.get('auth_method', 0)  # 0=key-based, 1=password
        self.x11_forwarding = data.get('x11_forwarding', False)
        
        # Port forwarding rules
        self.forwarding_rules = data.get('forwarding_rules', [])
        
        # Asyncio event loop
        self.loop = asyncio.get_event_loop()

    def __str__(self):
        return f"{self.nickname} ({self.username}@{self.host})"
        
    async def connect(self):
        """Establish the SSH connection using system SSH client"""
        try:
            # Build SSH command
            ssh_cmd = ['ssh']
            
            # Add key file if specified
            if self.keyfile and os.path.exists(self.keyfile):
                ssh_cmd.extend(['-i', self.keyfile])
                if self.key_passphrase:
                    # Note: For passphrase-protected keys, you might need to use ssh-agent
                    # or expect script in a real implementation
                    logger.warning("Passphrase-protected keys may require additional setup")
            
            # Add host and port
            if self.port != 22:
                ssh_cmd.extend(['-p', str(self.port)])
                
            # Add username if specified
            ssh_cmd.append(f"{self.username}@{self.host}" if self.username else self.host)
            
            # For non-interactive checks, just run a simple command
            test_cmd = ssh_cmd + ["echo", "Connection successful"]
            
            # Run the command
            process = await asyncio.create_subprocess_exec(
                *test_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Wait for the process to complete
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                self.is_connected = True
                # Store the base SSH command for later use
                self.ssh_cmd = ssh_cmd
                return True
            else:
                logger.error(f"SSH connection failed: {stderr.decode().strip()}")
                self.is_connected = False
                return False
                
        except Exception as e:
            logger.error(f"Failed to connect to {self}: {e}")
            self.is_connected = False
            return False
            
    async def disconnect(self):
        """Close the SSH connection and clean up"""
        # Cancel all forwarding tasks
        for task in self.forwarders:
            if not task.done():
                task.cancel()
        
        # Close all listeners
        for listener in self.listeners:
            listener.close()
        
        # Clean up any running processes
        if hasattr(self, 'process') and self.process:
            try:
                # Try to terminate gracefully first
                self.process.terminate()
                try:
                    # Wait a bit for the process to terminate
                    await asyncio.wait_for(self.process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    # Force kill if it doesn't terminate
                    self.process.kill()
                    await self.process.wait()
            except ProcessLookupError:
                # Process already terminated
                pass
            except Exception as e:
                logger.error(f"Error terminating SSH process: {e}")
            finally:
                self.process = None
        
        self.is_connected = False
        logger.info(f"Disconnected from {self}")
        self.listeners.clear()
        
    async def setup_forwarding(self):
        """Set up all forwarding rules"""
        if not self.is_connected or not self.connection:
            return False
            
        success = True
        for rule in self.forwarding_rules:
            if not rule.get('enabled', True):
                continue
                
            rule_type = rule.get('type')
            listen_addr = rule.get('listen_addr', 'localhost')
            listen_port = rule.get('listen_port')
            
            try:
                if rule_type == 'dynamic':
                    # Start SOCKS proxy server
                    await self.start_dynamic_forwarding(listen_addr, listen_port)
                elif rule_type == 'local':
                    # Local port forwarding
                    remote_host = rule.get('remote_host', 'localhost')
                    remote_port = rule.get('remote_port')
                    await self.start_local_forwarding(listen_addr, listen_port, remote_host, remote_port)
                elif rule_type == 'remote':
                    # Remote port forwarding
                    remote_host = rule.get('remote_host', 'localhost')
                    remote_port = rule.get('remote_port')
                    await self.start_remote_forwarding(listen_addr, listen_port, remote_host, remote_port)
                    
            except Exception as e:
                logger.error(f"Failed to set up {rule_type} forwarding: {e}")
                success = False
                
        return success
        
    async def _forward_data(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, label: str):
        """Helper method to forward data between two streams"""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass  # Connection closed
        except Exception as e:
            logger.error(f"Error in {label}: {e}")
        finally:
            writer.close()
            
    async def start_dynamic_forwarding(self, listen_addr: str, listen_port: int):
        """Start dynamic port forwarding (SOCKS proxy) using system SSH client"""
        try:
            # Build the SSH command for dynamic port forwarding (SOCKS proxy)
            ssh_cmd = self.ssh_cmd + [
                '-N',  # No remote command
                '-D', f"{listen_addr}:{listen_port}"  # Dynamic port forwarding (SOCKS)
            ]
            
            # Start the SSH process
            self.process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Check if the process started successfully
            if self.process.returncode is not None and self.process.returncode != 0:
                stderr = await self.process.stderr.read()
                raise Exception(f"SSH dynamic port forwarding failed: {stderr.decode().strip()}")
            
            logger.info(f"Dynamic port forwarding (SOCKS) started on {listen_addr}:{listen_port}")
            
            # Store the forwarding rule
            self.forwarding_rules.append({
                'type': 'dynamic',
                'listen_addr': listen_addr,
                'listen_port': listen_port,
                'process': self.process
            })
            
            # Wait for the process to complete
            await self.process.wait()
            
        except Exception as e:
            logger.error(f"Dynamic port forwarding failed: {e}")
            if hasattr(self, 'process') and self.process:
                self.process.terminate()
                await self.process.wait()
            raise

    async def start_local_forwarding(self, listen_addr: str, listen_port: int, remote_host: str, remote_port: int):
        """Start local port forwarding using system SSH client"""
        try:
            # Build the SSH command for local port forwarding
            ssh_cmd = self.ssh_cmd + [
                '-N',  # No remote command
                '-L', f"{listen_addr}:{listen_port}:{remote_host}:{remote_port}"
            ]
            
            # Start the SSH process
            self.process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Check if the process started successfully
            if self.process.returncode is not None and self.process.returncode != 0:
                stderr = await self.process.stderr.read()
                raise Exception(f"SSH port forwarding failed: {stderr.decode().strip()}")
            
            logger.info(f"Local forwarding started: {listen_addr}:{listen_port} -> {remote_host}:{remote_port}")
            
            # Store the forwarding rule
            self.forwarding_rules.append({
                'type': 'local',
                'listen_addr': listen_addr,
                'listen_port': listen_port,
                'remote_host': remote_host,
                'remote_port': remote_port,
                'process': self.process
            })
            
            # Wait for the process to complete
            await self.process.wait()
            
        except Exception as e:
            logger.error(f"Local forwarding failed: {e}")
            if hasattr(self, 'process') and self.process:
                self.process.terminate()
                await self.process.wait()
            raise

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
        self.ssh_config = {}
        self.ssh_config_path = os.path.expanduser('~/.ssh/config')
        self.known_hosts_path = os.path.expanduser('~/.ssh/known_hosts')
        self.loop = asyncio.get_event_loop()
        self.active_connections: Dict[str, asyncio.Task] = {}
        
        # Initialize SSH config
        self.load_ssh_config()
        self.load_ssh_keys()
        
        # Initialize secure storage
        try:
            self.bus = secretstorage.dbus_init()
            self.collection = secretstorage.get_default_collection(self.bus)
            logger.info("Secure storage initialized")
        except Exception as e:
            logger.warning(f"Failed to initialize secure storage: {e}")
            self.collection = None
        
    def load_ssh_config(self):
        """Load connections from SSH config file"""
        try:
            if not os.path.exists(self.ssh_config_path):
                logger.info("SSH config file not found, creating empty one")
                os.makedirs(os.path.dirname(self.ssh_config_path), exist_ok=True)
                with open(self.ssh_config_path, 'w') as f:
                    f.write("# SSH configuration file\n")
                return

            # Simple SSH config parser
            current_host = None
            current_config = {}
            
            with open(self.ssh_config_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                        
                    if ' ' in line:
                        key, value = line.split(maxsplit=1)
                        key = key.lower()
                        value = value.strip('\"\'')
                        
                        if key == 'host':
                            # Save previous host if exists
                            if current_host and current_config:
                                connection_data = self.parse_host_config(current_config)
                                if connection_data:
                                    self.connections.append(Connection(connection_data))
                            
                            # Start new host
                            current_host = value
                            current_config = {'host': value}
                        else:
                            # Add to current config
                            current_config[key] = value
            
            # Add the last host
            if current_host and current_config:
                connection_data = self.parse_host_config(current_config)
                if connection_data:
                    self.connections.append(Connection(connection_data))
            
            logger.info(f"Loaded {len(self.connections)} connections from SSH config")
            
        except Exception as e:
            logger.error(f"Failed to load SSH config: {e}", exc_info=True)

    def parse_host_config(self, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse host configuration from SSH config"""
        try:
            host = config.get('host', '')
            if not host:
                return None
                
            # Extract relevant configuration
            parsed = {
                'nickname': host,
                'host': config.get('hostname', host),
                'port': int(config.get('port', 22)),
                'username': config.get('user', getpass.getuser()),
                'private_key': config.get('identityfile'),
                'forwarding_rules': []
            }
            
            # Handle proxy settings if any
            if 'proxycommand' in config:
                parsed['proxy_command'] = config['proxycommand']
                
            return parsed
            
        except Exception as e:
            logger.error(f"Error parsing host config: {e}", exc_info=True)
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
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving password for {username}@{host}: {e}")
            return None

    def format_ssh_config_entry(self, data: Dict[str, Any]) -> str:
        """Format connection data as SSH config entry"""
        lines = [f"Host {data['nickname']}"]
        lines.append(f"    HostName {data['host']}")
        lines.append(f"    User {data['username']}")
        
        if data.get('port', 22) != 22:
            lines.append(f"    Port {data['port']}")
        
        if data.get('private_key'):
            lines.append(f"    IdentityFile {data['private_key']}")
        
        # Add port forwarding rules
        for rule in data.get('forwarding_rules', []):
            if not rule.get('enabled', True):
                continue
                
            listen_spec = f"{rule['listen_addr']}:{rule['listen_port']}"
            
            if rule['type'] == 'local':
                dest_spec = f"{rule['remote_host']}:{rule['remote_port']}"
                lines.append(f"    LocalForward {listen_spec} {dest_spec}")
            elif rule['type'] == 'remote':
                dest_spec = f"{rule['remote_host']}:{rule['remote_port']}"
                lines.append(f"    RemoteForward {listen_spec} {dest_spec}")
            elif rule['type'] == 'dynamic':
                lines.append(f"    DynamicForward {listen_spec}")
        
        return '\n'.join(lines)

    def update_ssh_config_file(self, connection: Connection, new_data: Dict[str, Any]):
        """Update SSH config file with new connection data"""
        try:
            if not os.path.exists(self.ssh_config_path):
                # If config file doesn't exist, create it with the new connection
                os.makedirs(os.path.dirname(self.ssh_config_path), exist_ok=True)
                with open(self.ssh_config_path, 'w') as f:
                    f.write("# SSH configuration file\n")
                
                # Add the new connection
                with open(self.ssh_config_path, 'a') as f:
                    updated_config = self.format_ssh_config_entry(new_data)
                    f.write('\n' + updated_config + '\n')
                return
            
            # Read current config
            try:
                with open(self.ssh_config_path, 'r') as f:
                    lines = f.readlines()
            except IOError as e:
                logger.error(f"Failed to read SSH config: {e}")
                return
            
            # Find and update the connection's Host block
            updated_lines = []
            in_target_host = False
            host_nickname = connection.nickname
            host_found = False
            
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                
                # Check if this is the start of our target host block
                if line.startswith('Host ') and line.split()[1] == host_nickname:
                    host_found = True
                    in_target_host = True
                    # Write the updated host block
                    updated_config = self.format_ssh_config_entry(new_data)
                    updated_lines.append(updated_config + '\n')
                    
                    # Skip all lines until the next Host or end of file
                    i += 1
                    while i < len(lines) and not lines[i].startswith('Host '):
                        i += 1
                    in_target_host = False
                    continue
                
                if not in_target_host:
                    updated_lines.append(lines[i])
                
                i += 1
            
            # If host not found, append the new config
            if not host_found:
                updated_config = self.format_ssh_config_entry(new_data)
                updated_lines.append('\n' + updated_config + '\n')
            
            # Write the updated config back to file
            try:
                with open(self.ssh_config_path, 'w') as f:
                    f.writelines(updated_lines)
            except IOError as e:
                logger.error(f"Failed to write SSH config: {e}")
                
        except Exception as e:
            logger.error(f"Error updating SSH config: {e}", exc_info=True)
            raise

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

    async def connect(self, connection: Connection):
        """Connect to an SSH host asynchronously"""
        try:
            # Connect to the SSH server
            connected = await connection.connect()
            if not connected:
                raise Exception("Failed to establish SSH connection")
            
            # Set up port forwarding if needed
            if connection.forwarding_rules:
                await connection.setup_forwarding()
            
            # Store the connection task
            if connection.host in self.active_connections:
                self.active_connections[connection.host].cancel()
            
            # Create a task to keep the connection alive
            async def keepalive():
                try:
                    while connection.is_connected:
                        try:
                            # Send keepalive every 30 seconds
                            await asyncio.sleep(30)
                            if connection.connection and connection.is_connected:
                                await connection.connection.ping()
                        except (ConnectionError, asyncio.CancelledError):
                            break
                        except Exception as e:
                            logger.error(f"Keepalive error for {connection}: {e}")
                            break
                finally:
                    if connection.is_connected:
                        await connection.disconnect()
                    connection.is_connected = False
                    self.emit('connection-status-changed', connection, False)
                    logger.info(f"Disconnected from {connection}")
            
            # Start the keepalive task
            task = asyncio.create_task(keepalive())
            self.active_connections[connection.host] = task
            
            # Emit status change
            self.emit('connection-status-changed', connection, True)
            logger.info(f"Connected to {connection}")
            
            return True
            
        except Exception as e:
            error_msg = f"Failed to connect to {connection}: {e}"
            logger.error(error_msg, exc_info=True)
            if hasattr(connection, 'connection') and connection.connection:
                await connection.disconnect()
            connection.is_connected = False
            raise Exception(error_msg) from e
    
    async def disconnect(self, connection: Connection):
        """Disconnect from SSH host and clean up resources asynchronously"""
        try:
            # Cancel the keepalive task if it exists
            if connection.host in self.active_connections:
                self.active_connections[connection.host].cancel()
                try:
                    await self.active_connections[connection.host]
                except asyncio.CancelledError:
                    pass
                del self.active_connections[connection.host]
            
            # Disconnect the connection
            if hasattr(connection, 'connection') and connection.connection and connection.is_connected:
                await connection.disconnect()
            
            # Emit status change signal
            self.emit('connection-status-changed', connection, False)
            logger.info(f"Disconnected from {connection}")
            
        except Exception as e:
            logger.error(f"Failed to disconnect from {connection}: {e}", exc_info=True)
            raise

    def get_connections(self) -> List[Connection]:
        """Get list of all connections"""
        return self.connections.copy()

    def find_connection_by_nickname(self, nickname: str) -> Optional[Connection]:
        """Find connection by nickname"""
        for connection in self.connections:
            if connection.nickname == nickname:
                return connection
        return None