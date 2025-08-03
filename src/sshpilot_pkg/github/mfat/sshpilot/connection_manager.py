"""
Connection Manager for sshPilot
Handles SSH connections, configuration, and secure password storage
"""

import os
import asyncio
import logging
import configparser
import getpass
from typing import Dict, List, Optional, Any, Tuple

import secretstorage
import asyncssh
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
        """Establish the SSH connection"""
        kwargs = {
            'host': self.host,
            'port': self.port,
            'username': self.username,
            'known_hosts': None,  # Disable known hosts check for now
            'client_keys': [self.keyfile] if self.keyfile and os.path.exists(self.keyfile) else None,
            'passphrase': self.key_passphrase if self.keyfile and os.path.exists(self.keyfile) else None,
            'password': self.password if self.auth_method == 1 else None,
            'preferred_auth': ['publickey', 'password'] if self.auth_method == 1 else ['publickey'],
        }
        
        try:
            self.connection = await asyncssh.connect(**{k: v for k, v in kwargs.items() if v is not None})
            self.is_connected = True
            return True
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
            
        # Close the connection
        if self.connection and self.is_connected:
            self.connection.close()
            await self.connection.wait_closed()
            
        self.is_connected = False
        self.forwarders.clear()
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
        """Start dynamic port forwarding (SOCKS proxy)"""
        if not self.connection:
            return
            
        logger.info(f"Starting dynamic forwarding on {listen_addr}:{listen_port}")
        
        # Create a SOCKS5 proxy server
        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                # Handle SOCKS5 handshake
                version = await reader.readexactly(1)
                if version != b'\x05':
                    writer.close()
                    return
                    
                # Authentication methods
                nmethods = (await reader.readexactly(1))[0]
                methods = await reader.readexactly(nmethods)
                
                # We only support no authentication (0x00)
                if 0x00 not in methods:
                    writer.write(b'\x05\xff')  # No acceptable methods
                    await writer.drain()
                    writer.close()
                    return
                    
                # Accept no authentication
                writer.write(b'\x05\x00')
                await writer.drain()
                
                # Get connection request
                version, cmd, rsv, addr_type = await reader.readexactly(4)
                
                if cmd != 1:  # Only support CONNECT
                    writer.write(b'\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00')
                    await writer.drain()
                    writer.close()
                    return
                
                # Parse destination address
                if addr_type == 1:  # IPv4
                    addr = await reader.readexactly(4)
                    dest_addr = '.'.join(str(b) for b in addr)
                elif addr_type == 3:  # Domain name
                    addr_len = (await reader.readexactly(1))[0]
                    dest_addr = (await reader.readexactly(addr_len)).decode('ascii')
                else:  # IPv6 or unsupported
                    writer.write(b'\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00')
                    await writer.drain()
                    writer.close()
                    return
                
                # Get destination port
                dest_port = int.from_bytes(await reader.readexactly(2), 'big')
                
                # Connect to the remote host through the SSH connection
                try:
                    ssh_reader, ssh_writer = await asyncio.wait_for(
                        self.connection.open_connection(dest_addr, dest_port),
                        timeout=30.0
                    )
                except Exception as e:
                    logger.error(f"Failed to connect to {dest_addr}:{dest_port}: {e}")
                    writer.write(b'\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00')  # Host unreachable
                    await writer.drain()
                    writer.close()
                    return
                
                # Send success response (using the original address/port for BND.ADDR and BND.PORT)
                writer.write(b'\x05\x00\x00\x01' + b'\x00\x00\x00\x00\x00\x00')
                await writer.drain()
                
                # Forward data between client and SSH connection
                await asyncio.gather(
                    self._forward_data(reader, ssh_writer, "client -> ssh"),
                    self._forward_data(ssh_reader, writer, "ssh -> client")
                )
                
            except (ConnectionError, asyncio.IncompleteReadError):
                pass  # Connection closed
            except Exception as e:
                logger.error(f"Error in SOCKS proxy: {e}")
            finally:
                writer.close()
                if 'ssh_writer' in locals():
                    ssh_writer.close()
                    await ssh_writer.wait_closed()
        
        # Start the SOCKS server
        server = await asyncio.start_server(
            handle_client,
            host=listen_addr,
            port=listen_port,
            reuse_address=True
        )
        
        # Store the server so it can be closed later
        self.listeners.append(server)
        
        # Run the server in the background
        task = asyncio.create_task(server.serve_forever())
        self.forwarders.append(task)
        
    async def start_local_forwarding(self, listen_addr: str, listen_port: int, remote_host: str, remote_port: int):
        """Start local port forwarding"""
        async def forward_connection(local_reader: asyncio.StreamReader, local_writer: asyncio.StreamWriter):
            try:
                remote_reader, remote_writer = await asyncio.wait_for(
                    self.connection.open_connection(remote_host, remote_port),
                    timeout=30.0
                )
                
                await asyncio.gather(
                    self._forward_data(local_reader, remote_writer, "local -> remote"),
                    self._forward_data(remote_reader, local_writer, "remote -> local")
                )
                
            except (ConnectionError, asyncio.TimeoutError) as e:
                logger.error(f"Local forwarding error: {e}")
            finally:
                local_writer.close()
                if 'remote_writer' in locals():
                    remote_writer.close()
        
        server = await asyncio.start_server(
            forward_connection,
            host=listen_addr,
            port=listen_port,
            reuse_address=True
        )
        
        self.listeners.append(server)
        task = asyncio.create_task(server.serve_forever())
        self.forwarders.append(task)
        
    async def start_remote_forwarding(self, listen_addr: str, listen_port: int, remote_host: str, remote_port: int):
        """Start remote port forwarding"""
        async def forward_connection(remote_reader: asyncio.StreamReader, remote_writer: asyncio.StreamWriter):
            try:
                local_reader, local_writer = await asyncio.open_connection(remote_host, remote_port)
                
                await asyncio.gather(
                    self._forward_data(remote_reader, local_writer, "remote -> local"),
                    self._forward_data(local_reader, remote_writer, "local -> remote")
                )
                
            except (ConnectionError, asyncio.TimeoutError) as e:
                logger.error(f"Remote forwarding error: {e}")
            finally:
                remote_writer.close()
                if 'local_writer' in locals():
                    local_writer.close()
        
        # For remote forwarding, we need to set up a listener on the remote server
        try:
            server = await self.connection.forward_remote_port(
                '', listen_port,  # Listen on all interfaces on the remote
                remote_host, remote_port
            )
            self.listeners.append(server)
            logger.info(f"Remote forwarding set up: remote:{listen_port} -> {remote_host}:{remote_port}")
        except Exception as e:
            logger.error(f"Failed to set up remote forwarding: {e}")
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