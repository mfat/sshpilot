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

from .ssh_config_utils import resolve_ssh_config_files, get_effective_ssh_config

try:
    import secretstorage
except Exception:
    secretstorage = None
try:
    import keyring
except Exception:
    keyring = None
import socket
import time
from gi.repository import GObject, GLib
from .askpass_utils import get_ssh_env_with_askpass, get_ssh_env_with_askpass_for_password

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
_SERVICE_NAME = "sshPilot"

def _is_macos():
    """Check if running on macOS"""
    return os.name == 'posix' and hasattr(os, 'uname') and os.uname().sysname == 'Darwin'

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
        self.aliases = data.get('aliases', [])
        self.username = data.get('username', '')
        self.port = data.get('port', 22)
        # previously: self.keyfile = data.get('keyfile', '')
        self.keyfile = data.get('keyfile') or data.get('private_key', '') or ''
        self.certificate = data.get('certificate') or ''
        self.password = data.get('password', '')
        self.key_passphrase = data.get('key_passphrase', '')
        # Source file of this configuration block
        self.source = data.get('source', '')
        # Proxy settings
        self.proxy_command = data.get('proxy_command', '')
        self.proxy_jump = data.get('proxy_jump', '')
        # Commands
        self.local_command = data.get('local_command', '')
        self.remote_command = data.get('remote_command', '')
        # Extra SSH config parameters
        self.extra_ssh_config = data.get('extra_ssh_config', '')
        self.pubkey_auth_no = bool(data.get('pubkey_auth_no', False))
        # Authentication method: 0 = key-based, 1 = password
        try:
            self.auth_method = int(data.get('auth_method', 0))
        except Exception:
            self.auth_method = 0
        # X11 forwarding preference
        self.x11_forwarding = bool(data.get('x11_forwarding', False))
        
        # Key selection mode: 0 try all, 1 specific key
        try:
            self.key_select_mode = int(data.get('key_select_mode', 0) or 0)
        except Exception:
            self.key_select_mode = 0

        # Port forwarding rules
        self.forwarding_rules = data.get('forwarding_rules', [])
        
        # Asyncio event loop
        self.loop = asyncio.get_event_loop()

    def __str__(self):
        return f"{self.nickname} ({self.username}@{self.host})"
        
    async def connect(self):
        """Prepare SSH command for later use (no preflight echo)."""
        try:
            ssh_cmd = ['ssh']

            # Pull advanced SSH defaults from config when available
            try:
                from .config import Config  # avoid circular import at top level
                cfg = Config()
                ssh_cfg = cfg.get_ssh_config()
            except Exception:
                ssh_cfg = {}
            apply_adv = bool(ssh_cfg.get('apply_advanced', False))
            connect_timeout = int(ssh_cfg.get('connection_timeout', 10)) if apply_adv else None
            connection_attempts = int(ssh_cfg.get('connection_attempts', 1)) if apply_adv else None
            strict_host = str(ssh_cfg.get('strict_host_key_checking', '')) if apply_adv else ''
            batch_mode = bool(ssh_cfg.get('batch_mode', False)) if apply_adv else False
            compression = bool(ssh_cfg.get('compression', True)) if apply_adv else False
            verbosity = int(ssh_cfg.get('verbosity', 0))
            debug_enabled = bool(ssh_cfg.get('debug_enabled', False))
            auto_add_host_keys = bool(ssh_cfg.get('auto_add_host_keys', True))

            # Apply advanced args only when user explicitly enabled them
            if apply_adv:
                if batch_mode:
                    ssh_cmd.extend(['-o', 'BatchMode=yes'])
                if connect_timeout is not None:
                    ssh_cmd.extend(['-o', f'ConnectTimeout={connect_timeout}'])
                if connection_attempts is not None:
                    ssh_cmd.extend(['-o', f'ConnectionAttempts={connection_attempts}'])
                if strict_host:
                    ssh_cmd.extend(['-o', f'StrictHostKeyChecking={strict_host}'])
                if compression:
                    ssh_cmd.append('-C')
            ssh_cmd.extend(['-o', 'ExitOnForwardFailure=yes'])
            ssh_cmd.extend(['-o', 'NumberOfPasswordPrompts=1'])

            # Apply default host key behavior when not explicitly set
            try:
                if (not strict_host) and auto_add_host_keys:
                    ssh_cmd.extend(['-o', 'StrictHostKeyChecking=accept-new'])
            except Exception:
                pass

            # Apply verbosity flags
            try:
                v = max(0, min(3, int(verbosity)))
                for _ in range(v):
                    ssh_cmd.append('-v')
                if v == 1:
                    ssh_cmd.extend(['-o', 'LogLevel=VERBOSE'])
                elif v == 2:
                    ssh_cmd.extend(['-o', 'LogLevel=DEBUG2'])
                elif v >= 3:
                    ssh_cmd.extend(['-o', 'LogLevel=DEBUG3'])
                elif debug_enabled:
                    ssh_cmd.extend(['-o', 'LogLevel=DEBUG'])
            except Exception:
                pass

            # Resolve effective SSH configuration for this nickname/host
            effective_cfg: Dict[str, Union[str, List[str]]] = {}
            target_alias = self.nickname or self.host
            if target_alias:
                effective_cfg = get_effective_ssh_config(target_alias)

            # Determine final parameters, falling back to resolved config when needed
            resolved_host = str(effective_cfg.get('hostname', self.host))
            resolved_user = self.username or str(effective_cfg.get('user', ''))
            try:
                resolved_port = int(effective_cfg.get('port', self.port))
            except Exception:
                resolved_port = self.port
            self.host = resolved_host
            self.port = resolved_port
            if resolved_user:
                self.username = resolved_user

            # Add key and certificate options
            try:
                if int(getattr(self, 'auth_method', 0) or 0) == 0:
                    identity_files: List[str] = []
                    if int(getattr(self, 'key_select_mode', 0) or 0) == 1:
                        if self.keyfile and os.path.exists(self.keyfile):
                            identity_files.append(self.keyfile)
                    else:
                        cfg_ids = effective_cfg.get('identityfile')
                        if cfg_ids:
                            if isinstance(cfg_ids, list):
                                identity_files.extend(cfg_ids)
                            else:
                                identity_files.append(cfg_ids)
                        if self.keyfile and os.path.exists(self.keyfile):
                            identity_files.append(self.keyfile)
                    for key_path in identity_files:
                        ssh_cmd.extend(['-i', key_path])
                        if self.key_passphrase:
                            logger.warning("Passphrase-protected keys may require additional setup")
                    cert_files: List[str] = []
                    if self.certificate:
                        cert_files.append(self.certificate)
                    cfg_cert = effective_cfg.get('certificatefile')
                    if cfg_cert:
                        if isinstance(cfg_cert, list):
                            cert_files.extend(cfg_cert)
                        else:
                            cert_files.append(cfg_cert)
                    for cert_path in cert_files:
                        ssh_cmd.extend(['-o', f'CertificateFile={cert_path}'])
            except Exception:
                pass

            # Proxy directives
            proxy_jump = self.proxy_jump or effective_cfg.get('proxyjump', '')
            if proxy_jump:
                ssh_cmd.extend(['-o', f'ProxyJump={proxy_jump}'])
            proxy_command = self.proxy_command or effective_cfg.get('proxycommand', '')
            if proxy_command:
                ssh_cmd.extend(['-o', f'ProxyCommand={proxy_command}'])

            # Port and user/host
            if resolved_port != 22:
                ssh_cmd.extend(['-p', str(resolved_port)])

            ssh_cmd.append(f"{resolved_user}@{resolved_host}" if resolved_user else resolved_host)

            # Store command for later use
            self.ssh_cmd = ssh_cmd
            self.is_connected = True
            return True
                
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
            
            # Set up askpass environment to prevent Flatpak from using system askpass
            from .askpass_utils import ensure_askpass_script, get_ssh_env_with_askpass_for_password
            ensure_askpass_script()
            env = os.environ.copy()
            env.update(get_ssh_env_with_askpass_for_password(self.host, self.username))
            
            # Start the SSH process
            logger.info(f"Starting dynamic port forwarding with command: {' '.join(ssh_cmd)}")
            self.process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
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
            
            # Set up askpass environment to prevent Flatpak from using system askpass
            from .askpass_utils import ensure_askpass_script, get_ssh_env_with_askpass_for_password
            ensure_askpass_script()
            env = os.environ.copy()
            env.update(get_ssh_env_with_askpass_for_password(self.host, self.username))
            
            # Start the SSH process
            self.process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
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

    async def start_remote_forwarding(self, listen_addr: str, listen_port: int, remote_host: str, remote_port: int):
        """Start remote port forwarding using system SSH client"""
        try:
            # Build the SSH command for remote port forwarding
            ssh_cmd = self.ssh_cmd + [
                '-N',  # No remote command
                '-R', f"{listen_addr}:{listen_port}:{remote_host}:{remote_port}"
            ]
            
            # Set up askpass environment to prevent Flatpak from using system askpass
            from .askpass_utils import ensure_askpass_script, get_ssh_env_with_askpass_for_password
            ensure_askpass_script()
            env = os.environ.copy()
            env.update(get_ssh_env_with_askpass_for_password(self.host, self.username))
            
            # Start the SSH process
            self.process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            
            # Check if the process started successfully
            if self.process.returncode is not None and self.process.returncode != 0:
                stderr = await self.process.stderr.read()
                raise Exception(f"SSH remote port forwarding failed: {stderr.decode().strip()}")
            
            logger.info(f"Remote forwarding started: {listen_addr}:{listen_port} -> {remote_host}:{remote_port}")
            
            # Store the forwarding rule
            self.forwarding_rules.append({
                'type': 'remote',
                'listen_addr': listen_addr,
                'listen_port': listen_port,
                'remote_host': remote_host,
                'remote_port': remote_port,
                'process': self.process
            })
            
            # Wait for the process to complete
            await self.process.wait()
            
        except Exception as e:
            logger.error(f"Remote forwarding failed: {e}")
            if hasattr(self, 'process') and self.process:
                self.process.terminate()
                await self.process.wait()
            raise

    def update_data(self, new_data: Dict[str, Any]):
        """Update connection data while preserving object identity"""
        self.data.update(new_data)
        self._update_properties_from_data(self.data)
    
    def _update_properties_from_data(self, data: Dict[str, Any]):
        """Update instance properties from data dictionary"""
        self.nickname = data.get('nickname', data.get('host', 'Unknown'))
        self.host = data.get('host', '')
        self.aliases = data.get('aliases', [])
        self.username = data.get('username', '')
        self.port = data.get('port', 22)
        self.keyfile = data.get('keyfile') or data.get('private_key', '') or ''


        self.certificate = data.get('certificate') or ''
        self.password = data.get('password', '')
        self.key_passphrase = data.get('key_passphrase', '')
        self.source = data.get('source', getattr(self, 'source', ''))
        self.local_command = data.get('local_command', '')
        self.remote_command = data.get('remote_command', '')
        self.proxy_command = data.get('proxy_command', '')
        self.proxy_jump = data.get('proxy_jump', '')
        # Extra SSH config parameters
        self.extra_ssh_config = data.get('extra_ssh_config', '')
        self.pubkey_auth_no = bool(data.get('pubkey_auth_no', False))

        # Authentication method: 0 = key-based, 1 = password
        # Preserve existing auth_method if not present in new data
        if 'auth_method' in data:
            try:
                self.auth_method = int(data.get('auth_method', 0))
            except Exception:
                self.auth_method = 0
            
        # X11 forwarding preference
        self.x11_forwarding = bool(data.get('x11_forwarding', False))
        
        # Key selection mode: 0 try all, 1 specific key
        try:
            self.key_select_mode = int(data.get('key_select_mode', 0) or 0)
        except Exception:
            self.key_select_mode = 0

        # Port forwarding rules
        self.forwarding_rules = data.get('forwarding_rules', [])

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
        # Store wildcard/negated host blocks (rules) separately
        self.rules: List[Dict[str, Any]] = []
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
        self.collection = None
        if secretstorage:
            try:
                self.bus = secretstorage.dbus_init()
                self.collection = secretstorage.get_default_collection(self.bus)
                logger.info("Secure storage initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize secure storage: {e}")
        else:
            # SecretStorage not available (e.g., macOS). Fall back to system keyring if present
            if keyring is not None:
                try:
                    backend = keyring.get_keyring()
                    logger.info("Using system keyring backend: %s", backend.__class__.__name__)
                except Exception:
                    logger.info("Keyring module present but no usable backend; passwords will not be stored")
            else:
                logger.info("No SecretStorage or keyring available; password storage disabled")
        return False  # run once

    def _ensure_collection(self) -> bool:
        """Ensure secretstorage collection is initialized and unlocked."""
        if not secretstorage:
            return False
        try:
            if getattr(self, 'collection', None) is None:
                try:
                    self.bus = secretstorage.dbus_init()
                    self.collection = secretstorage.get_default_collection(self.bus)
                except Exception as e:
                    logger.warning(f"Secret storage init failed: {e}")
                    self.collection = None
                    return False
            # Attempt to unlock if locked
            try:
                if hasattr(self.collection, 'is_locked') and self.collection.is_locked():
                    unlocked, _ = self.collection.unlock()
                    if not unlocked:
                        logger.warning("Secret storage collection remains locked")
                        return False
            except Exception as e:
                logger.debug(f"Could not unlock collection: {e}")
            return self.collection is not None
        except Exception:
            return False
        
    def load_ssh_config(self):
        """Load connections from SSH config file"""
        try:
            existing_by_nickname = {conn.nickname: conn for conn in self.connections}
            self.connections = []
            self.rules = []
            if not os.path.exists(self.ssh_config_path):
                logger.info("SSH config file not found, creating empty one")
                os.makedirs(os.path.dirname(self.ssh_config_path), exist_ok=True)
                with open(self.ssh_config_path, 'w') as f:
                    f.write("# SSH configuration file\n")
                return
            config_files = resolve_ssh_config_files(self.ssh_config_path)
            for cfg_file in config_files:
                current_host = None
                current_config: Dict[str, Any] = {}
                try:
                    with open(cfg_file, 'r') as f:
                        lines = f.readlines()
                except Exception as e:
                    logger.warning(f"Skipping unreadable config {cfg_file}: {e}")
                    continue
                i = 0
                while i < len(lines):
                    raw_line = lines[i]
                    line = raw_line.strip()
                    if not line or line.startswith('#'):
                        i += 1
                        continue
                    lowered = line.lower()
                    if lowered.startswith('include '):
                        i += 1
                        continue
                    if lowered.startswith('match '):
                        if current_host and current_config:
                            connection_data = self.parse_host_config(current_config, source=cfg_file)
                            if connection_data:
                                nickname = connection_data.get('nickname', '')
                                existing = existing_by_nickname.get(nickname)
                                if existing:
                                    existing.update_data(connection_data)
                                    self.connections.append(existing)
                                else:
                                    self.connections.append(Connection(connection_data))
                        current_host = None
                        current_config = {}
                        block_lines = [raw_line.rstrip('\n')]
                        i += 1
                        while i < len(lines) and not lines[i].lstrip().lower().startswith(('host ', 'match ', 'include ')):
                            block_lines.append(lines[i].rstrip('\n'))
                            i += 1
                        while block_lines and block_lines[-1].strip() == '':
                            block_lines.pop()
                        self.rules.append({'raw': '\n'.join(block_lines), 'source': cfg_file})
                        continue
                    if lowered.startswith('host '):
                        tokens = shlex.split(line[len('host '):])
                        if not tokens:
                            i += 1
                            continue
                        if current_host and current_config:
                            connection_data = self.parse_host_config(current_config, source=cfg_file)
                            if connection_data:
                                nickname = connection_data.get('nickname', '')
                                existing = existing_by_nickname.get(nickname)
                                if existing:
                                    existing.update_data(connection_data)
                                    self.connections.append(existing)
                                else:
                                    self.connections.append(Connection(connection_data))
                        current_host = tokens[0]
                        current_config = {'host': tokens[0]}
                        if len(tokens) > 1:
                            current_config['aliases'] = tokens[1:]
                        i += 1
                        continue
                    if ' ' in line:
                        key, value = line.split(maxsplit=1)
                        key = key.lower()
                        if key in current_config and key in ['localforward', 'remoteforward', 'dynamicforward']:
                            if not isinstance(current_config[key], list):
                                current_config[key] = [current_config[key]]
                            current_config[key].append(value)
                        else:
                            current_config[key] = value
                    i += 1
                if current_host and current_config:
                    connection_data = self.parse_host_config(current_config, source=cfg_file)
                    if connection_data:
                        nickname = connection_data.get('nickname', '')
                        existing = existing_by_nickname.get(nickname)
                        if existing:
                            existing.update_data(connection_data)
                            self.connections.append(existing)
                        else:
                            self.connections.append(Connection(connection_data))
            logger.info(f"Loaded {len(self.connections)} connections from SSH config")
        except Exception as e:
            logger.error(f"Failed to load SSH config: {e}", exc_info=True)

    def parse_host_config(self, config: Dict[str, Any], source: str = None) -> Optional[Dict[str, Any]]:
        """Parse host configuration from SSH config"""
        try:
            def _unwrap(val: Any) -> Any:
                if isinstance(val, str) and len(val) >= 2:
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        return val[1:-1]
                return val

            host_token = _unwrap(config.get('host', ''))
            if not host_token:
                return None

            aliases = [_unwrap(a) for a in (config.get('aliases', []) or [])]

            # Detect wildcard or negated host tokens (e.g., '*', '?', '!pattern')
            tokens = [host_token] + aliases
            if any('*' in t or '?' in t or t.startswith('!') for t in tokens):
                if not hasattr(self, 'rules'):
                    self.rules = []
                rule_block = dict(config)
                rule_block['host'] = host_token
                if aliases:
                    rule_block['aliases'] = aliases
                if source:
                    rule_block['source'] = source
                self.rules.append(rule_block)
                return None

            host = host_token

            # Extract relevant configuration
            parsed = {
                'nickname': host,
                'aliases': aliases,
                'host': _unwrap(config.get('hostname', host)),

                'port': int(_unwrap(config.get('port', 22))),
                'username': _unwrap(config.get('user', getpass.getuser())),
                # previously: 'private_key': config.get('identityfile'),

                'keyfile': os.path.expanduser(_unwrap(config.get('identityfile'))) if config.get('identityfile') else '',
                'certificate': os.path.expanduser(_unwrap(config.get('certificatefile'))) if config.get('certificatefile') else '',
                'forwarding_rules': []
            }
            if source:
                parsed['source'] = source
            

            # Map ForwardX11 yes/no → x11_forwarding boolean
            try:
                fwd_x11 = str(config.get('forwardx11', 'no')).strip().lower()
                parsed['x11_forwarding'] = fwd_x11 in ('yes', 'true', '1', 'on')
            except Exception:
                parsed['x11_forwarding'] = False
            
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
                            if rule_type == 'local':
                                parsed['forwarding_rules'].append({
                                    'type': 'local',
                                    'listen_addr': bind_addr,
                                    'listen_port': listen_port,
                                    'remote_host': remote_host,
                                    'remote_port': remote_port,
                                    'enabled': True
                                })
                            else:
                                # RemoteForward: remote host/port listens, destination is local host/port
                                parsed['forwarding_rules'].append({
                                    'type': 'remote',
                                    'listen_addr': bind_addr,   # remote host
                                    'listen_port': listen_port, # remote port
                                    'local_host': remote_host,  # destination host (local)
                                    'local_port': remote_port,  # destination port (local)
                                    'enabled': True
                                })
            
            # Handle proxy settings if any
            if 'proxycommand' in config:
                parsed['proxy_command'] = config['proxycommand']
            if 'proxyjump' in config:
                parsed['proxy_jump'] = config['proxyjump']
            
            # Commands: LocalCommand requires PermitLocalCommand
            try:
                def _unescape_cfg_value(val: str) -> str:
                    if not isinstance(val, str):
                        return val
                    v = val.strip()
                    # If the value is wrapped in double quotes, strip only the outer quotes
                    if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
                        v = v[1:-1]
                    # Convert escaped quotes back for UI
                    v = v.replace('\\"', '"').replace('\\\\', '\\')
                    return v

                if 'localcommand' in config:
                    parsed['local_command'] = _unescape_cfg_value(config.get('localcommand', ''))
                if 'remotecommand' in config:
                    parsed['remote_command'] = _unescape_cfg_value(config.get('remotecommand', ''))
                # Map RequestTTY to a boolean flag to aid terminal decisions if needed
                if 'requesttty' in config:
                    parsed['request_tty'] = str(config.get('requesttty', '')).strip().lower() in ('yes', 'force', 'true', '1', 'on')
            except Exception:
                pass

            # Key selection mode: if IdentitiesOnly is set truthy, select specific key
            try:
                ident_only = str(config.get('identitiesonly', '')).strip().lower()
                if ident_only in ('yes', 'true', '1', 'on'):
                    parsed['key_select_mode'] = 1
                else:
                    parsed['key_select_mode'] = 0
            except Exception:
                parsed['key_select_mode'] = 0

            # Determine authentication method
            try:
                prefer_auth = str(config.get('preferredauthentications', '')).strip().lower()
                pubkey_auth = str(config.get('pubkeyauthentication', '')).strip().lower()
                parsed['pubkey_auth_no'] = (pubkey_auth == 'no')
                if 'password' in prefer_auth or pubkey_auth == 'no':
                    parsed['auth_method'] = 1
                else:
                    parsed['auth_method'] = 0
            except Exception:
                parsed['auth_method'] = 0
            
            # Parse extra SSH config options (custom options not handled by standard fields)
            extra_config_lines = []
            # Only include options that are explicitly handled by the main UI fields
            standard_options = {
                'host', 'hostname', 'port', 'user', 'identityfile', 'certificatefile',
                'forwardx11', 'localforward', 'remoteforward', 'dynamicforward',
                'proxycommand', 'proxyjump', 'localcommand', 'remotecommand', 'requesttty',
                'identitiesonly', 'permitlocalcommand',
                'preferredauthentications', 'pubkeyauthentication'
            }
            
            for key, value in config.items():
                if key.lower() not in standard_options:
                    # This is a custom SSH option (including Ciphers, Compression, etc.)
                    if isinstance(value, list):
                        # Handle multiple values for the same option
                        for val in value:
                            extra_config_lines.append(f"{key} {val}")
                    else:
                        extra_config_lines.append(f"{key} {value}")
            
            if extra_config_lines:
                parsed['extra_ssh_config'] = '\n'.join(extra_config_lines)
                
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
        # Prefer SecretStorage on Linux when available
        if self._ensure_collection():
            try:
                attributes = {
                    'application': _SERVICE_NAME,
                    'host': host,
                    'username': username
                }
                # Delete existing password if any
                existing_items = list(self.collection.search_items(attributes))
                for item in existing_items:
                    item.delete()
                # Store new password
                self.collection.create_item(
                    f'{_SERVICE_NAME}: {username}@{host}',
                    attributes,
                    password.encode()
                )
                logger.debug(f"Password stored for {username}@{host} via SecretStorage")
                return True
            except Exception as e:
                logger.error(f"Failed to store password (SecretStorage): {e}")
                # Fall through to keyring attempt
        # Fallback to cross-platform keyring (macOS Keychain, etc.)
        if keyring is not None:
            try:
                keyring.set_password(_SERVICE_NAME, f"{username}@{host}", password)
                logger.debug(f"Password stored for {username}@{host} via keyring")
                return True
            except Exception as e:
                logger.error(f"Failed to store password (keyring): {e}")
        logger.warning("No secure storage backend available; password not stored")
        return False

    def get_password(self, host: str, username: str) -> Optional[str]:
        """Retrieve password from system keyring"""
        # Try SecretStorage first
        if self._ensure_collection():
            try:
                attributes = {
                    'application': _SERVICE_NAME,
                    'host': host,
                    'username': username
                }
                items = list(self.collection.search_items(attributes))
                if items:
                    password = items[0].get_secret().decode()
                    logger.debug(f"Password retrieved for {username}@{host} via SecretStorage")
                    return password
            except Exception as e:
                logger.error(f"Error retrieving password (SecretStorage) for {username}@{host}: {e}")
        # Fallback to keyring
        if keyring is not None:
            try:
                pw = keyring.get_password(_SERVICE_NAME, f"{username}@{host}")
                if pw:
                    logger.debug(f"Password retrieved for {username}@{host} via keyring")
                return pw
            except Exception as e:
                logger.error(f"Error retrieving password (keyring) for {username}@{host}: {e}")
        return None

    def delete_password(self, host: str, username: str) -> bool:
        """Delete stored password for host/user from system keyring"""
        removed_any = False
        # Try SecretStorage first
        if self._ensure_collection():
            try:
                attributes = {
                    'application': _SERVICE_NAME,
                    'host': host,
                    'username': username
                }
                items = list(self.collection.search_items(attributes))
                for item in items:
                    try:
                        item.delete()
                        removed_any = True
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Error deleting password (SecretStorage) for {username}@{host}: {e}")
        # Also attempt keyring cleanup so both stores are cleared if both were used
        if keyring is not None:
            try:
                keyring.delete_password(_SERVICE_NAME, f"{username}@{host}")
                removed_any = True or removed_any
            except Exception:
                pass
        if removed_any:
            logger.debug(f"Deleted stored password for {username}@{host}")
        return removed_any

    def store_key_passphrase(self, key_path: str, passphrase: str) -> bool:
        """Store key passphrase securely in system keyring"""
        try:
            # Use keyring on macOS, secretstorage on Linux
            if keyring and _is_macos():
                # macOS: use keyring
                keyring.set_password('sshPilot', key_path, passphrase)
                logger.debug(f"Stored key passphrase for {key_path} using keyring")
                return True
            elif self._ensure_collection():
                # Linux: use secretstorage
                key_id = f"key_passphrase_{os.path.basename(key_path)}_{hash(key_path)}"
                
                attributes = {
                    'application': 'sshPilot',
                    'type': 'key_passphrase',
                    'key_path': key_path,
                    'key_id': key_id
                }
                
                # Store the passphrase
                self.collection.create_item(
                    f"SSH Key Passphrase: {os.path.basename(key_path)}",
                    attributes,
                    passphrase.encode('utf-8'),
                    replace=True
                )
                logger.debug(f"Stored key passphrase for {key_path} using secretstorage")
                return True
            else:
                logger.warning("No keyring backend available for storing passphrase")
                return False
        except Exception as e:
            logger.error(f"Error storing key passphrase for {key_path}: {e}")
            return False

    def get_key_passphrase(self, key_path: str) -> Optional[str]:
        """Retrieve key passphrase from system keyring"""
        try:
            # Use keyring on macOS, secretstorage on Linux
            if keyring and _is_macos():
                # macOS: use keyring
                passphrase = keyring.get_password('sshPilot', key_path)
                if passphrase:
                    logger.debug(f"Retrieved key passphrase for {key_path} using keyring")
                return passphrase
            elif self._ensure_collection():
                # Linux: use secretstorage
                attributes = {
                    'application': 'sshPilot',
                    'type': 'key_passphrase',
                    'key_path': key_path
                }
                
                items = list(self.collection.search_items(attributes))
                if items:
                    passphrase = items[0].get_secret().decode('utf-8')
                    logger.debug(f"Retrieved key passphrase for {key_path} using secretstorage")
                    return passphrase
            return None
        except Exception as e:
            logger.error(f"Error retrieving key passphrase for {key_path}: {e}")
            return None

    def delete_key_passphrase(self, key_path: str) -> bool:
        """Delete stored key passphrase from system keyring"""
        try:
            # Use keyring on macOS, secretstorage on Linux
            if keyring and _is_macos():
                # macOS: use keyring
                try:
                    keyring.delete_password('sshPilot', key_path)
                    logger.debug(f"Deleted stored key passphrase for {key_path} using keyring")
                    return True
                except keyring.errors.PasswordDeleteError:
                    # Password doesn't exist, which is fine
                    return True
            elif self._ensure_collection():
                # Linux: use secretstorage
                attributes = {
                    'application': 'sshPilot',
                    'type': 'key_passphrase',
                    'key_path': key_path
                }
                
                items = list(self.collection.search_items(attributes))
                removed_any = False
                for item in items:
                    try:
                        item.delete()
                        removed_any = True
                    except Exception:
                        pass
                if removed_any:
                    logger.debug(f"Deleted stored key passphrase for {key_path} using secretstorage")
                return removed_any
            return False
        except Exception as e:
            logger.error(f"Error deleting key passphrase for {key_path}: {e}")
            return False

    def _ensure_ssh_agent(self) -> bool:
        """Ensure ssh-agent is running and export environment variables"""
        try:
            # Check if ssh-agent is already running
            if os.environ.get('SSH_AUTH_SOCK'):
                logger.debug("SSH agent already running")
                return True
            
            # Start a new ssh-agent
            logger.debug("Starting new ssh-agent")
            result = subprocess.run(
                ['ssh-agent', '-s'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to start ssh-agent: {result.stderr}")
                return False
            
            # Parse the output to extract environment variables
            for line in result.stdout.split('\n'):
                if line.startswith('export '):
                    # Extract variable name and value
                    var_part = line[7:]  # Remove 'export '
                    if '=' in var_part:
                        name, value = var_part.split('=', 1)
                        # Remove quotes if present
                        value = value.strip().strip('"\'')
                        os.environ[name] = value
            
            logger.debug("SSH agent started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error ensuring ssh-agent is running: {e}")
            return False



    def add_key_to_agent(self, key_path: str) -> bool:
        """Add SSH key to ssh-agent using secure SSH_ASKPASS script"""
        from .askpass_utils import ensure_key_in_agent
        return ensure_key_in_agent(key_path)

    def prepare_key_for_connection(self, key_path: str) -> bool:
        """Prepare SSH key for connection by adding it to ssh-agent"""
        from .askpass_utils import prepare_key_for_connection
        return prepare_key_for_connection(key_path)

    def format_ssh_config_entry(self, data: Dict[str, Any]) -> str:
        """Format connection data as SSH config entry"""
        def _quote_token(token: str) -> str:
            if not token:
                return '""'
            if any(c.isspace() for c in token):
                return f'"{token}"'
            return token

        nickname = data.get('nickname', '')
        aliases = data.get('aliases', []) or []
        host_tokens = [_quote_token(nickname)] + [_quote_token(a) for a in aliases]
        lines = ["Host " + " ".join(host_tokens)]
        
        # Add basic connection info
        lines.append(f"    HostName {data.get('host', '')}")
        lines.append(f"    User {data.get('username', '')}")
        
        # Add port if specified and not default
        port = data.get('port')
        if port and port != 22:  # Only add port if it's not the default 22
            lines.append(f"    Port {port}")
        
        # Add IdentityFile/IdentitiesOnly per selection when auth is key-based
        keyfile = data.get('keyfile') or data.get('private_key')
        auth_method = int(data.get('auth_method', 0) or 0)
        key_select_mode = int(data.get('key_select_mode', 0) or 0)  # 0=try all, 1=specific
        if auth_method == 0:
            # Only write IdentityFile if key_select_mode == 1 (specific key)
            if key_select_mode == 1 and keyfile and keyfile.strip() and not keyfile.strip().lower().startswith('select key file'):
                if ' ' in keyfile and not (keyfile.startswith('"') and keyfile.endswith('"')):
                    keyfile = f'"{keyfile}"'
                lines.append(f"    IdentityFile {keyfile}")
                lines.append("    IdentitiesOnly yes")

                # Add certificate if specified (exclude placeholder text)
                certificate = data.get('certificate')
                if certificate and certificate.strip() and not certificate.strip().lower().startswith('select certificate'):
                    if ' ' in certificate and not (certificate.startswith('"') and certificate.endswith('"')):
                        certificate = f'"{certificate}"'
                    lines.append(f"    CertificateFile {certificate}")
            # Include password-based fallback if a password is provided
            if data.get('password'):
                lines.append(
                    "    PreferredAuthentications gssapi-with-mic,hostbased,publickey,keyboard-interactive,password"
                )
        else:
            # Password-based authentication only
            lines.append("    PreferredAuthentications password")
            if data.get('pubkey_auth_no'):
                lines.append("    PubkeyAuthentication no")
        
        # Add X11 forwarding if enabled
        if data.get('x11_forwarding', False):
            lines.append("    ForwardX11 yes")

        # Add LocalCommand if specified, ensure PermitLocalCommand (write exactly as provided)
        local_cmd = (data.get('local_command') or '').strip()
        if local_cmd:
            lines.append("    PermitLocalCommand yes")
            lines.append(f"    LocalCommand {local_cmd}")

        # Add RemoteCommand and RequestTTY if specified (ensure shell stays active)
        remote_cmd = (data.get('remote_command') or '').strip()
        if remote_cmd:
            # Ensure we keep an interactive shell after the command
            remote_cmd_aug = remote_cmd if 'exec $SHELL' in remote_cmd else f"{remote_cmd} ; exec $SHELL -l"
            # Write RemoteCommand first, then RequestTTY (order for readability)
            lines.append(f"    RemoteCommand {remote_cmd_aug}")
            lines.append("    RequestTTY yes")
        
        # Add port forwarding rules if any (ensure sane defaults)
        for rule in data.get('forwarding_rules', []):
            listen_addr = rule.get('listen_addr', '') or '127.0.0.1'
            listen_port = rule.get('listen_port', '')
            if not listen_port:
                continue
            listen_spec = f"{listen_addr}:{listen_port}"
            
            if rule.get('type') == 'local':
                dest_spec = f"{rule.get('remote_host', '')}:{rule.get('remote_port', '')}"
                lines.append(f"    LocalForward {listen_spec} {dest_spec}")
            elif rule.get('type') == 'remote':
                # For RemoteForward we forward remote listen -> local destination
                dest_spec = f"{rule.get('local_host') or rule.get('remote_host', '')}:{rule.get('local_port') or rule.get('remote_port', '')}"
                lines.append(f"    RemoteForward {listen_spec} {dest_spec}")
            elif rule.get('type') == 'dynamic':
                lines.append(f"    DynamicForward {listen_spec}")
        
        # Add extra SSH config parameters if provided
        extra_config = data.get('extra_ssh_config', '').strip()
        if extra_config:
            # Split by lines and add each line as a separate config option
            for line in extra_config.split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):  # Skip empty lines and comments
                    # Ensure proper indentation
                    if not line.startswith('    '):
                        line = f"    {line}"
                    lines.append(line)

        # Remove duplicate or unwanted auth lines
        cleaned_lines = []
        seen_auth_lines = set()
        auth_keys = {
            'preferredauthentications password',
            'pubkeyauthentication no',
        }
        for line in lines:
            key = line.strip().lower()
            if key in auth_keys:
                if auth_method == 0:
                    # Skip entirely for key-based auth
                    continue
                if key in seen_auth_lines:
                    # Avoid duplicates for password auth
                    continue
                seen_auth_lines.add(key)
            cleaned_lines.append(line)

        return '\n'.join(cleaned_lines)

    def update_ssh_config_file(self, connection: Connection, new_data: Dict[str, Any], original_nickname: str = None):
        """Update SSH config file with new connection data"""
        try:
            target_path = new_data.get('source') or getattr(connection, 'source', None) or self.ssh_config_path
            if not os.path.exists(target_path):
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, 'w') as f:
                    f.write("# SSH configuration file\n")
                with open(target_path, 'a') as f:
                    updated_config = self.format_ssh_config_entry(new_data)
                    f.write('\n' + updated_config + '\n')
                return

            try:
                with open(target_path, 'r') as f:
                    lines = f.readlines()
            except IOError as e:
                logger.error(f"Failed to read SSH config: {e}")
                raise
            
            # Find and update the connection's Host block using nickname matching
            updated_lines = []
            new_name = str(new_data.get('nickname') or '')
            host_found = False
            replaced_once = False

            # For renaming, we need to find the existing block by the original nickname
            # The connection object might already have the new nickname, so we need to be smarter
            candidate_names = {new_name}
            
            # Add the original nickname to candidate names for proper matching during renames
            if original_nickname:
                candidate_names.add(original_nickname)
            
            logger.debug(f"Looking for host block with candidate names: {candidate_names}")
            logger.debug(f"Original nickname: {original_nickname}, New name: {new_name}")

            i = 0
            while i < len(lines):
                raw_line = lines[i]
                lstripped = raw_line.lstrip()
                lowered = lstripped.lower()

                if lowered.startswith('host '):
                    parts = lstripped.split(None, 1)
                    full_value = parts[1].strip() if len(parts) > 1 else ''
                    host_names = shlex.split(full_value)

                    logger.debug(
                        f"Found Host line: '{lstripped.strip()}' -> full_value='{full_value}' -> host_names={host_names}"
                    )

                    if any(host_name in candidate_names for host_name in host_names):
                        logger.debug(
                            f"MATCH FOUND! Host '{host_names}' matches candidate names {candidate_names}"
                        )
                        host_found = True
                        if not replaced_once:
                            updated_config = self.format_ssh_config_entry(new_data)
                            updated_lines.append(updated_config + '\n')
                            replaced_once = True
                        # Skip this Host line and all subsequent lines until next Host/Match block
                        i += 1
                        while i < len(lines) and not lines[i].lstrip().lower().startswith(('host ', 'match ')):
                            i += 1
                        continue
                    else:
                        # This is a different Host block, keep it
                        updated_lines.append(raw_line)
                else:
                    # Not a Host line, keep it
                    updated_lines.append(raw_line)

                i += 1
            
            # If host not found, append the new config
            if not host_found:
                updated_config = self.format_ssh_config_entry(new_data)
                updated_lines.append('\n' + updated_config + '\n')
            
            try:
                with open(target_path, 'w') as f:
                    f.writelines(updated_lines)
                logger.info(
                    "Wrote SSH config for host %s (found=%s, rules=%d) to %s",
                    new_name,
                    host_found,
                    len(new_data.get('forwarding_rules', []) or []),
                    target_path,
                )
            except IOError as e:
                logger.error(f"Failed to write SSH config: {e}")
                raise
        except Exception as e:
            logger.error(f"Error updating SSH config: {e}", exc_info=True)
            raise

    def remove_ssh_config_entry(self, host_nickname: str, source: Optional[str] = None) -> bool:
        """Remove a Host block from SSH config by nickname.

        Returns True if a block was removed, False if not found or on error.
        """
        try:
            target_path = source or self.ssh_config_path
            if not os.path.exists(target_path):
                return False
            try:
                with open(target_path, 'r') as f:
                    lines = f.readlines()
            except IOError as e:
                logger.error(f"Failed to read SSH config for delete: {e}")
                return False

            updated_lines = []
            i = 0
            removed = False
            # Alias-aware and indentation-robust deletion
            # Only match exact full value or exact alias token equal to the nickname
            candidate_names = {host_nickname}

            while i < len(lines):
                raw_line = lines[i]
                lstripped = raw_line.lstrip()
                lowered = lstripped.lower()
                if lowered.startswith('host '):
                    parts = lstripped.split(None, 1)
                    full_value = parts[1].strip() if len(parts) > 1 else ''
                    current_names = shlex.split(full_value)
                    if any(name in candidate_names for name in current_names):
                        removed = True
                        i += 1
                        while i < len(lines) and not lines[i].lstrip().lower().startswith(('host ', 'match ')):
                            i += 1
                        continue
                # Keep line
                updated_lines.append(raw_line)
                i += 1

            if removed:
                try:
                    with open(target_path, 'w') as f:
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
            target_path = new_data.get('source') or getattr(connection, 'source', self.ssh_config_path)
            logger.info(
                "Updating connection '%s' → writing to %s (rules=%d)",
                connection.nickname,
                target_path,
                len(new_data.get('forwarding_rules', []) or [])
            )
            # Capture previous identifiers for credential cleanup
            prev_host = getattr(connection, 'host', '')
            prev_user = getattr(connection, 'username', '')
            original_nickname = getattr(connection, 'nickname', '')
            
            # Update existing object IN-PLACE instead of creating new ones
            connection.update_data(new_data)
            
            # Update the SSH config file with original nickname for proper matching
            self.update_ssh_config_file(connection, new_data, original_nickname)
            
            # Handle password storage/removal
            if 'password' in new_data:
                pwd = new_data.get('password') or ''
                # Determine current identifiers after update
                curr_host = new_data.get('host') or getattr(connection, 'host', prev_host)
                curr_user = new_data.get('username') or getattr(connection, 'username', prev_user)
                if pwd:
                    self.store_password(curr_host, curr_user, pwd)
                else:
                    # Remove any stored passwords for both previous and current identifiers
                    try:
                        if prev_host and prev_user:
                            self.delete_password(prev_host, prev_user)
                    except Exception:
                        pass
                    try:
                        if curr_host and curr_user and (curr_host != prev_host or curr_user != prev_user):
                            self.delete_password(curr_host, curr_user)
                    except Exception:
                        pass
            
            # DO NOT call load_ssh_config() here - it breaks object references
            
            # Emit signal with SAME connection object
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
            
            # Remove password from secure storage
            try:
                self.delete_password(connection.host, connection.username)
            except Exception as e:
                logger.warning(f"Failed to remove password from storage: {e}")
            
            # Remove from SSH config file
            try:
                removed = self.remove_ssh_config_entry(connection.nickname, getattr(connection, 'source', None))
                logger.debug(f"SSH config entry removed={removed} for {connection.nickname}")
            except Exception as e:
                logger.warning(f"Failed to remove SSH config entry for {connection.nickname}: {e}")
            
            # Remove per-connection metadata (auth method, etc.) to avoid lingering entries
            try:
                from .config import Config
                cfg = Config()
                meta_all = cfg.get_setting('connections_meta', {}) or {}
                if isinstance(meta_all, dict) and connection.nickname in meta_all:
                    del meta_all[connection.nickname]
                    cfg.set_setting('connections_meta', meta_all)
                    logger.debug(f"Removed metadata for {connection.nickname}")
            except Exception as e:
                logger.debug(f"Could not remove metadata for {connection.nickname}: {e}")
            
            # Emit signal
            self.emit('connection-removed', connection)
            
            # Reload connections so in-memory list reflects latest file state
            try:
                self.load_ssh_config()
            except Exception:
                pass

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

    def update_connection_status(self, connection: Connection, is_connected: bool):
        """Update connection status in the manager
        
        This method is called by terminals to update the connection manager's
        tracking of connection status, especially after reconnections.
        """
        try:
            # Update the connection's status
            connection.is_connected = is_connected
            
            # For terminal-based connections (not async), we don't use active_connections
            # but we still need to emit the status change signal
            GLib.idle_add(self.emit, 'connection-status-changed', connection, is_connected)
            
            logger.debug(f"Connection manager updated status for {connection.nickname}: {'Connected' if is_connected else 'Disconnected'}")
            
        except Exception as e:
            logger.error(f"Failed to update connection status: {e}")

    def get_connections(self) -> List[Connection]:
        """Get list of all connections"""
        return self.connections.copy()

    def find_connection_by_nickname(self, nickname: str) -> Optional[Connection]:
        """Find connection by nickname"""
        for connection in self.connections:
            if connection.nickname == nickname:
                return connection
        return None