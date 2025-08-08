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
import socket
import time
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
        # previously: self.keyfile = data.get('keyfile', '')
        self.keyfile = data.get('keyfile') or data.get('private_key', '') or ''
        self.password = data.get('password', '')
        self.key_passphrase = data.get('key_passphrase', '')
        
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

            # Pull advanced SSH defaults from config when available
            try:
                from .config import Config  # avoid circular import at top level
                cfg = Config()
                ssh_cfg = cfg.get_ssh_config()
            except Exception:
                ssh_cfg = {}
            connect_timeout = int(ssh_cfg.get('connection_timeout', 10))
            connection_attempts = int(ssh_cfg.get('connection_attempts', 1))
            strict_host = str(ssh_cfg.get('strict_host_key_checking', 'accept-new'))
            batch_mode = bool(ssh_cfg.get('batch_mode', True))
            compression = bool(ssh_cfg.get('compression', True))

            # Sane non-interactive defaults to avoid indefinite hangs
            if batch_mode:
                ssh_cmd.extend(['-o', 'BatchMode=yes'])
            ssh_cmd.extend(['-o', f'ConnectTimeout={connect_timeout}'])
            ssh_cmd.extend(['-o', f'ConnectionAttempts={connection_attempts}'])
            if strict_host:
                ssh_cmd.extend(['-o', f'StrictHostKeyChecking={strict_host}'])
            if compression:
                ssh_cmd.append('-C')
            
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
            
            # Wait for the process to complete with a hard timeout safeguard
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=connect_timeout + 2)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except Exception:
                    pass
                await asyncio.sleep(0)
                logger.error("SSH connectivity check timed out")
                self.is_connected = False
                return False
            
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
        if not self.is_connected:
            return
            
        try:
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
            
            logger.info(f"Disconnected from {self}")
            return True
            
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
            return False
        finally:
            # Always ensure is_connected is set to False
            self.is_connected = False
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
            logger.debug(f"Starting dynamic port forwarding setup for {self.host} on {listen_addr}:{listen_port}")
            
            # Build the complete SSH command for dynamic port forwarding
            ssh_cmd = ['ssh', '-v']  # Add verbose flag for debugging

            # Read config for options
            try:
                from .config import Config
                cfg = Config()
                ssh_cfg = cfg.get_ssh_config()
            except Exception:
                ssh_cfg = {}
            connect_timeout = int(ssh_cfg.get('connection_timeout', 10))
            connection_attempts = int(ssh_cfg.get('connection_attempts', 1))
            keepalive_interval = int(ssh_cfg.get('keepalive_interval', 30))
            keepalive_count = int(ssh_cfg.get('keepalive_count_max', 3))
            strict_host = str(ssh_cfg.get('strict_host_key_checking', 'accept-new'))
            batch_mode = bool(ssh_cfg.get('batch_mode', True))

            # Robust non-interactive options to prevent hangs
            if batch_mode:
                ssh_cmd.extend(['-o', 'BatchMode=yes'])
            ssh_cmd.extend(['-o', f'ConnectTimeout={connect_timeout}'])
            ssh_cmd.extend(['-o', f'ConnectionAttempts={connection_attempts}'])
            ssh_cmd.extend(['-o', f'ServerAliveInterval={keepalive_interval}'])
            ssh_cmd.extend(['-o', f'ServerAliveCountMax={keepalive_count}'])
            if strict_host:
                ssh_cmd.extend(['-o', f'StrictHostKeyChecking={strict_host}'])
            
            # Add key file if specified
            if self.keyfile and os.path.exists(self.keyfile):
                logger.debug(f"Using SSH key: {self.keyfile}")
                ssh_cmd.extend(['-i', self.keyfile])
                if self.key_passphrase:
                    logger.debug("Key has a passphrase")
            else:
                logger.debug("No SSH key specified or key not found")
                
            # Add host and port
            if self.port != 22:
                logger.debug(f"Using custom SSH port: {self.port}")
                ssh_cmd.extend(['-p', str(self.port)])
                
            # Add dynamic port forwarding option
            forward_spec = f"{listen_addr}:{listen_port}"
            logger.debug(f"Setting up dynamic forwarding to: {forward_spec}")
            
            ssh_cmd.extend([
                '-N',  # No remote command
                '-D', forward_spec,  # Dynamic port forwarding (SOCKS)
                '-f',  # Run in background
                '-o', 'ExitOnForwardFailure=yes',  # Exit if forwarding fails
                '-o', 'ServerAliveInterval=30',    # Keep connection alive
                '-o', 'ServerAliveCountMax=3'      # Max missed keepalives before disconnect
            ])
            
            # Add username and host
            target = f"{self.username}@{self.host}" if self.username else self.host
            ssh_cmd.append(target)
            
            # Log the full command (without sensitive data)
            logger.debug(f"SSH command: {' '.join(ssh_cmd[:10])}...")
            
            # Start the SSH process
            logger.info(f"Starting dynamic port forwarding with command: {' '.join(ssh_cmd)}")
            self.process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Wait a bit to catch any immediate errors
            try:
                stdout, stderr = await asyncio.wait_for(self.process.communicate(), timeout=5.0)
                if stdout:
                    logger.debug(f"SSH stdout: {stdout.decode().strip()}")
                if stderr:
                    logger.debug(f"SSH stderr: {stderr.decode().strip()}")
                    
                if self.process.returncode != 0:
                    error_msg = stderr.decode().strip() if stderr else "Unknown error"
                    logger.error(f"SSH dynamic port forwarding failed with code {self.process.returncode}: {error_msg}")
                    raise Exception(f"SSH dynamic port forwarding failed: {error_msg}")
                else:
                    logger.info("SSH process started successfully")
            except asyncio.TimeoutError:
                # If we get here, the process is still running which is good
                logger.debug("SSH process is running in background")
                
                # Check if the port is actually listening
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(1)
                        result = s.connect_ex((listen_addr, int(listen_port)))
                        if result == 0:
                            logger.info(f"Successfully verified port {listen_port} is listening")
                        else:
                            logger.warning(f"Port {listen_port} is not listening (connect result: {result})")
                except Exception as e:
                    logger.warning(f"Could not verify if port is listening: {e}")
            
            logger.info(f"Dynamic port forwarding (SOCKS) started on {listen_addr}:{listen_port}")
            
            # Store the forwarding rule
            rule = {
                'type': 'dynamic',
                'listen_addr': listen_addr,
                'listen_port': listen_port,
                'process': self.process,
                'start_time': time.time()
            }
            self.forwarding_rules.append(rule)
            logger.debug(f"Added forwarding rule: {rule}")
            
            # Log all forwarding rules for debugging
            logger.debug(f"Current forwarding rules: {self.forwarding_rules}")
            
            return True
            
        except Exception as e:
            logger.error(f"Dynamic port forwarding failed: {e}", exc_info=True)
            if hasattr(self, 'process') and self.process:
                try:
                    logger.debug("Terminating SSH process due to error")
                    self.process.terminate()
                    await asyncio.wait_for(self.process.wait(), timeout=2.0)
                except (ProcessLookupError, asyncio.TimeoutError) as e:
                    logger.debug(f"Error terminating process: {e}")
                    pass
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
        
        # Load SSH config immediately for fast UI
        self.load_ssh_config()

        # Defer slower operations to idle to avoid blocking startup
        GLib.idle_add(self._post_init_slow_path)

    def _post_init_slow_path(self):
        """Run slower initialization steps after UI is responsive."""
        try:
            # Key scan
            self.load_ssh_keys()
        except Exception as e:
            logger.debug(f"SSH key scan skipped/failed: {e}")
        
        # Initialize secure storage (can be slow)
        try:
            self.bus = secretstorage.dbus_init()
            self.collection = secretstorage.get_default_collection(self.bus)
            logger.info("Secure storage initialized")
        except Exception as e:
            logger.warning(f"Failed to initialize secure storage: {e}")
            self.collection = None
        return False  # run once
        
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
                            # Handle multiple values for the same key (like multiple LocalForward rules)
                            if key in current_config and key in ['localforward', 'remoteforward', 'dynamicforward']:
                                if not isinstance(current_config[key], list):
                                    current_config[key] = [current_config[key]]
                                current_config[key].append(value)
                            else:
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
                # previously: 'private_key': config.get('identityfile'),
                'keyfile': os.path.expanduser(config.get('identityfile')) if config.get('identityfile') else None,
                'forwarding_rules': []
            }
            
            # Handle port forwarding rules
            for forward_type in ['localforward', 'remoteforward', 'dynamicforward']:
                if forward_type not in config:
                    continue
                    
                forward_specs = config[forward_type]
                if not isinstance(forward_specs, list):
                    forward_specs = [forward_specs]
                    
                for forward_spec in forward_specs:
                    if forward_type == 'dynamicforward':
                        # Format is usually "[bind_address:]port"
                        if ':' in forward_spec:
                            bind_addr, port_str = forward_spec.rsplit(':', 1)
                            listen_port = int(port_str)
                        else:
                            bind_addr = '127.0.0.1'  # Default bind address
                            listen_port = int(forward_spec)
                        
                        parsed['forwarding_rules'].append({
                            'type': 'dynamic',
                            'listen_addr': bind_addr,
                            'listen_port': listen_port,
                            'enabled': True
                        })
                    else:
                        # Handle LocalForward and RemoteForward
                        # Format is "[bind_address:]port host:hostport"
                        parts = forward_spec.split()
                        if len(parts) == 2:
                            listen_spec, dest_spec = parts
                            
                            # Parse listen address and port
                            if ':' in listen_spec:
                                bind_addr, port_str = listen_spec.rsplit(':', 1)
                                listen_port = int(port_str)
                            else:
                                bind_addr = '127.0.0.1'  # Default bind address
                                listen_port = int(listen_spec)
                            
                            # Parse destination host and port
                            if ':' in dest_spec:
                                remote_host, remote_port = dest_spec.split(':')
                                remote_port = int(remote_port)
                            else:
                                remote_host = dest_spec
                                remote_port = 22  # Default SSH port
                            
                            rule_type = 'local' if forward_type == 'localforward' else 'remote'
                            parsed['forwarding_rules'].append({
                                'type': rule_type,
                                'listen_addr': bind_addr,
                                'listen_port': listen_port,
                                'remote_host': remote_host,
                                'remote_port': remote_port,
                                'enabled': True
                            })
            
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
        
        # Add basic connection info
        lines.append(f"    HostName {data.get('host', '')}")
        lines.append(f"    User {data.get('username', '')}")
        
        # Add port if specified and not default
        port = data.get('port')
        if port and port != 22:  # Only add port if it's not the default 22
            lines.append(f"    Port {port}")
        
        # Add keyfile if specified and not a placeholder
        keyfile = data.get('keyfile') or data.get('private_key')
        if keyfile and keyfile.strip() and not keyfile.strip().lower().startswith('select key file'):
            # Ensure the keyfile path is properly quoted if it contains spaces
            if ' ' in keyfile and not (keyfile.startswith('"') and keyfile.endswith('"')):
                keyfile = f'"{keyfile}"'
            lines.append(f"    IdentityFile {keyfile}")
        
        # Add X11 forwarding if enabled
        if data.get('x11_forwarding', False):
            lines.append("    ForwardX11 yes")
        
        # Add port forwarding rules if any
        for rule in data.get('forwarding_rules', []):
            listen_spec = f"{rule.get('listen_addr', '')}:{rule.get('listen_port', '')}"
            
            if rule.get('type') == 'local':
                dest_spec = f"{rule.get('remote_host', '')}:{rule.get('remote_port', '')}"
                lines.append(f"    LocalForward {listen_spec} {dest_spec}")
            elif rule.get('type') == 'remote':
                dest_spec = f"{rule.get('remote_host', '')}:{rule.get('remote_port', '')}"
                lines.append(f"    RemoteForward {listen_spec} {dest_spec}")
            elif rule.get('type') == 'dynamic':
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

    def remove_ssh_config_entry(self, host_nickname: str) -> bool:
        """Remove a Host block from ~/.ssh/config by nickname.

        Returns True if a block was removed, False if not found or on error.
        """
        try:
            if not os.path.exists(self.ssh_config_path):
                return False
            try:
                with open(self.ssh_config_path, 'r') as f:
                    lines = f.readlines()
            except IOError as e:
                logger.error(f"Failed to read SSH config for delete: {e}")
                return False

            updated_lines = []
            i = 0
            removed = False
            while i < len(lines):
                raw_line = lines[i]
                line = raw_line.strip()
                if line.startswith('Host '):
                    parts = line.split()
                    name = parts[1] if len(parts) > 1 else ''
                    if name == host_nickname:
                        # Skip this host block
                        removed = True
                        i += 1
                        while i < len(lines) and not lines[i].startswith('Host '):
                            i += 1
                        continue
                # Keep line
                updated_lines.append(raw_line)
                i += 1

            if removed:
                try:
                    with open(self.ssh_config_path, 'w') as f:
                        f.writelines(updated_lines)
                except IOError as e:
                    logger.error(f"Failed to write SSH config after delete: {e}")
                    return False
            return removed
        except Exception as e:
            logger.error(f"Error removing SSH config entry: {e}", exc_info=True)
            return False

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
            
            # Remove from SSH config file
            try:
                removed = self.remove_ssh_config_entry(connection.nickname)
                logger.debug(f"SSH config entry removed={removed} for {connection.nickname}")
            except Exception as e:
                logger.warning(f"Failed to remove SSH config entry for {connection.nickname}: {e}")
            
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
            
            # Update the connection state and emit status change
            connection.is_connected = True
            GLib.idle_add(self.emit, 'connection-status-changed', connection, True)
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
            
            # Update the connection state and emit status change signal
            connection.is_connected = False
            GLib.idle_add(self.emit, 'connection-status-changed', connection, False)
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