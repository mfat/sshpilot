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
import re
from typing import Dict, List, Optional, Any, Tuple, Union

from .ssh_config_utils import resolve_ssh_config_files, get_effective_ssh_config
from .platform_utils import is_macos, get_config_dir, get_ssh_dir

try:
    from gi.repository import Secret
except Exception:

    Secret = None
try:
    import keyring
except Exception:
    keyring = None
import socket
import time
from gi.repository import GObject, GLib
from .askpass_utils import (
    clear_passphrase,
    get_secret_schema,
    lookup_passphrase,
    store_passphrase,
)

if Secret is not None:
    _SECRET_SCHEMA = get_secret_schema()
else:
    _SECRET_SCHEMA = None

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

class Connection:
    """Represents an SSH connection"""
    
    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.is_connected = False
        self.connection = None
        self.forwarders: List[asyncio.Task] = []
        self.listeners: List[asyncio.Server] = []

        hostname_value = data.get('hostname')
        host_value = data.get('host', '')

        nickname_value = data.get('nickname')
        if nickname_value:
            self.nickname = nickname_value
        else:
            fallback_nickname = hostname_value if hostname_value else host_value
            self.nickname = fallback_nickname or 'Unknown'

        if 'aliases' in data:
            self.aliases = data.get('aliases', [])
        self.hostname = hostname_value or ''
        host_alias = data.get('host')
        if host_alias is None:
            host_alias = self.nickname
        self.host = host_alias or ''


        self.username = data.get('username', '')
        self.port = data.get('port', 22)
        # previously: self.keyfile = data.get('keyfile', '')
        self.keyfile = data.get('keyfile') or data.get('private_key', '') or ''
        self.certificate = data.get('certificate') or ''
        self.password = data.get('password', '')
        self.key_passphrase = data.get('key_passphrase', '')
        # Source file of this configuration block
        self.source = data.get('source', '')
        # Provide friendly accessor for UI components that wish to display
        # the originating config file for this connection.
        
        # Proxy settings
        self.proxy_command = data.get('proxy_command', '')
        pj = data.get('proxy_jump', [])
        if isinstance(pj, str):
            pj = [h.strip() for h in re.split(r'[\s,]+', pj) if h.strip()]
        self.proxy_jump = pj
        self.forward_agent = bool(data.get('forward_agent', False))
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
        
        # Key selection mode: 0 try all, 1 specific key (IdentitiesOnly), 2 specific key (no IdentitiesOnly)
        try:
            self.key_select_mode = int(data.get('key_select_mode', 0) or 0)
        except Exception:
            self.key_select_mode = 0

        # Port forwarding rules
        self.forwarding_rules = data.get('forwarding_rules', [])
        
        # Asyncio event loop
        self.loop = asyncio.get_event_loop()

    def __str__(self):
        return f"{self.nickname} ({self.username}@{self.hostname})"

    def get_effective_host(self) -> str:
        """Return the hostname used for operations, falling back to aliases."""

        if self.hostname:
            return self.hostname
        if getattr(self, 'host', ''):
            return self.host
        return self.nickname


    @property
    def source_file(self) -> str:
        """Return path to the config file where this host is defined."""
        return self.source
        
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
            target_alias = self.nickname or self.hostname
            stored_alias = ""
            if isinstance(self.data, dict):
                stored_alias = str(self.data.get('host') or '')
            alias_fallback = (
                stored_alias
                or self.nickname
                or self.host
                or self.hostname
            )
            if target_alias:
                effective_cfg = get_effective_ssh_config(target_alias)

            # Determine final parameters, falling back to resolved config when needed
            existing_hostname = self.hostname or ''

            resolved_host_cfg = effective_cfg.get('hostname')
            if isinstance(resolved_host_cfg, str):
                resolved_host_cfg = resolved_host_cfg.strip()
            effective_hostname = str(resolved_host_cfg) if resolved_host_cfg else ''
            if effective_hostname:
                alias_candidates = {
                    alias_fallback or '',
                    self.host or '',
                    target_alias or '',
                }
                if existing_hostname or effective_hostname not in alias_candidates:
                    self.hostname = effective_hostname

            alias_value = alias_fallback or self.host or ''
            if alias_value:
                self.host = alias_value

            resolved_user = self.username or str(effective_cfg.get('user', ''))
            try:
                resolved_port = int(effective_cfg.get('port', self.port))
            except Exception:
                resolved_port = self.port
            self.port = resolved_port

            try:
                resolved_host = (
                    self.hostname
                    or alias_fallback
                    or target_alias
                    or self.get_effective_host()
                )
            except Exception:
                resolved_host = self.hostname or alias_fallback or target_alias or ''

            if resolved_user:
                self.username = resolved_user

            # Add key and certificate options
            try:
                if int(getattr(self, 'auth_method', 0) or 0) == 0:
                    identity_files: List[str] = []
                    key_mode = int(getattr(self, 'key_select_mode', 0) or 0)
                    if key_mode in (1, 2):
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
                        if key_mode == 1:
                            ssh_cmd.extend(['-o', 'IdentitiesOnly=yes'])
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
            proxy_jump = self.proxy_jump or effective_cfg.get('proxyjump', [])
            if isinstance(proxy_jump, str):
                proxy_jump = [h.strip() for h in re.split(r'[\s,]+', proxy_jump) if h.strip()]
            if proxy_jump:
                ssh_cmd.extend(['-o', f"ProxyJump={','.join(proxy_jump)}"])
            proxy_command = self.proxy_command or effective_cfg.get('proxycommand', '')
            if proxy_command:
                ssh_cmd.extend(['-o', f'ProxyCommand={proxy_command}'])

            forward_agent = self.forward_agent or str(effective_cfg.get('forwardagent', '')).strip().lower() in ('yes', 'true', '1', 'on')
            if forward_agent:
                ssh_cmd.append('-A')
                ssh_cmd.extend(['-o', 'ForwardAgent=yes'])

            # Port and user/host
            if resolved_port != 22:
                ssh_cmd.extend(['-p', str(resolved_port)])

            host_for_cmd = resolved_host or alias_fallback or target_alias or self.resolve_host_identifier()

            ssh_cmd.append(f"{resolved_user}@{host_for_cmd}" if resolved_user else host_for_cmd)

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
            logger.debug(f"Starting dynamic port forwarding setup for {self.hostname} on {listen_addr}:{listen_port}")
            
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
            target = f"{self.username}@{self.hostname}" if self.username else self.hostname
            ssh_cmd.append(target)
            
            # Log the full command (without sensitive data)
            logger.debug(f"SSH command: {' '.join(ssh_cmd[:10])}...")
            
            # Ensure ssh can prompt interactively by removing any askpass settings
            env = os.environ.copy()
            env.pop("SSH_ASKPASS", None)
            env.pop("SSH_ASKPASS_REQUIRE", None)
            
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
            
            # Ensure ssh can prompt interactively by removing any askpass settings
            env = os.environ.copy()
            env.pop("SSH_ASKPASS", None)
            env.pop("SSH_ASKPASS_REQUIRE", None)
            
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
            
            # Ensure ssh can prompt interactively by removing any askpass settings
            env = os.environ.copy()
            env.pop("SSH_ASKPASS", None)
            env.pop("SSH_ASKPASS_REQUIRE", None)
            
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
        hostname_value = data.get('hostname')
        host_value = data.get('host', '')

        nickname_value = data.get('nickname')
        if nickname_value:
            self.nickname = nickname_value
        else:
            fallback_nickname = hostname_value if hostname_value else host_value
            self.nickname = fallback_nickname or getattr(self, 'nickname', 'Unknown')

        if 'aliases' in data:
            self.aliases = data.get('aliases', getattr(self, 'aliases', []))

        if hostname_value in (None, ''):
            resolved_host = host_value or getattr(self, 'host', '')
        else:
            resolved_host = hostname_value
        self.host = resolved_host

        if hostname_value is None:
            self.hostname = resolved_host
        elif hostname_value == '':
            self.hostname = ''
        else:
            self.hostname = hostname_value


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
        pj = data.get('proxy_jump', [])
        if isinstance(pj, str):
            pj = [h.strip() for h in re.split(r'[\s,]+', pj) if h.strip()]
        self.proxy_jump = pj
        self.forward_agent = bool(data.get('forward_agent', False))
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

    def __init__(self, config, isolated_mode: bool = False):
        super().__init__()
        self.config = config
        self.connections: List[Connection] = []
        # Store wildcard/negated host blocks (rules) separately
        self.rules: List[Dict[str, Any]] = []
        self.ssh_config = {}
        self.loop = asyncio.get_event_loop()
        self.active_connections: Dict[str, asyncio.Task] = {}
        self._active_connection_keys: Dict[int, str] = {}
        self.ssh_config_path = ''
        self.known_hosts_path = ''

        # Initialize SSH config paths
        self.set_isolated_mode(isolated_mode)

        # Defer slower operations to idle to avoid blocking startup
        GLib.idle_add(self._post_init_slow_path)

    def _get_active_connection_key(self, connection: Connection) -> str:
        identifier = connection.resolve_host_identifier()
        if identifier:
            return identifier
        return f"connection-{id(connection)}"

    def set_isolated_mode(self, isolated: bool):
        """Switch between standard and isolated SSH configuration"""
        self.isolated_mode = bool(isolated)
        if self.isolated_mode:
            base = get_config_dir()
            self.ssh_config_path = os.path.join(base, 'ssh_config')
            self.known_hosts_path = os.path.join(base, 'known_hosts')
            os.makedirs(base, exist_ok=True)
            for path in (self.ssh_config_path, self.known_hosts_path):
                if not os.path.exists(path):
                    open(path, 'a').close()
        else:
            ssh_dir = get_ssh_dir()
            self.ssh_config_path = os.path.join(ssh_dir, 'config')
            self.known_hosts_path = os.path.join(ssh_dir, 'known_hosts')

        # Reload SSH config to reflect new paths
        self.load_ssh_config()

    def _post_init_slow_path(self):
        """Run slower initialization steps after UI is responsive."""
        try:
            # Key scan
            self.load_ssh_keys()
        except Exception as e:
            logger.debug(f"SSH key scan skipped/failed: {e}")
        
        # Initialize secure storage (can be slow)
        self.libsecret_available = False
        if not is_macos() and Secret is not None:
            try:
                Secret.Service.get_sync(Secret.ServiceFlags.NONE)
                self.libsecret_available = True

                logger.info("Secure storage initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize secure storage: {e}")
        elif keyring is not None:
            try:
                backend = keyring.get_keyring()
                logger.info("Using system keyring backend: %s", backend.__class__.__name__)
            except Exception:
                logger.info("Keyring module present but no usable backend; passwords will not be stored")
        else:
            logger.info("No secure storage backend available; password storage disabled")
        return False  # run once

    # No _ensure_collection needed with libsecret's high-level API

    def _get_active_connection_key(self, connection: Connection, *, prefer_stored: bool = True) -> str:
        """Return the key used to track a connection's keepalive task."""

        conn_id = id(connection)
        if prefer_stored:
            stored = self._active_connection_keys.get(conn_id)
            if stored:
                return stored

        try:
            effective_host = connection.get_effective_host()
        except AttributeError:
            effective_host = getattr(connection, 'hostname', '') or getattr(connection, 'host', '') or ''

        username = getattr(connection, 'username', '') or ''
        key = effective_host or ''
        if username:
            key = f"{key}::{username}" if key else username
        if not key:
            key = connection.nickname or str(conn_id)

        if prefer_stored:
            self._active_connection_keys[conn_id] = key
        return key

        
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
                current_hosts: List[str] = []
                current_config: Dict[str, Any] = {}

                def process_host_block(hosts: List[str], config: Dict[str, Any]):
                    cleaned_hosts = [token.strip() for token in hosts if token and token.strip()]
                    if not cleaned_hosts:
                        return

                    if any('*' in token or '?' in token or token.startswith('!') for token in cleaned_hosts):
                        host_cfg = dict(config)
                        host_cfg['host'] = cleaned_hosts[0]
                        self.parse_host_config(host_cfg, source=cfg_file)
                        return

                    for token in cleaned_hosts:
                        host_cfg = dict(config)
                        host_cfg['host'] = token
                        connection_data = self.parse_host_config(host_cfg, source=cfg_file)
                        if connection_data:
                            connection_data['source'] = cfg_file
                            nickname = connection_data.get('nickname', '')
                            existing = existing_by_nickname.get(nickname)
                            if existing:
                                existing.update_data(connection_data)
                                self.connections.append(existing)
                            else:
                                self.connections.append(Connection(connection_data))
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
                        if current_hosts and current_config:
                            tokens = current_hosts
                            if any('*' in t or '?' in t or t.startswith('!') for t in tokens):
                                host_cfg = dict(current_config)
                                host_cfg['host'] = tokens[0]
                                host_cfg['__host_tokens'] = list(tokens)
                                self.parse_host_config(host_cfg, source=cfg_file)
                            else:
                                for token in tokens:
                                    host_cfg = dict(current_config)
                                    host_cfg['host'] = token
                                    host_cfg['__host_tokens'] = list(tokens)
                                    connection_data = self.parse_host_config(host_cfg, source=cfg_file)
                                    if connection_data:
                                        connection_data['source'] = cfg_file
                                        nickname = connection_data.get('nickname', '')
                                        existing = existing_by_nickname.get(nickname)
                                        if existing:
                                            existing.update_data(connection_data)
                                            self.connections.append(existing)
                                        else:
                                            self.connections.append(Connection(connection_data))
                        current_hosts = []
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
                        if current_hosts and current_config:
                            prev_tokens = current_hosts
                            if any('*' in t or '?' in t or t.startswith('!') for t in prev_tokens):
                                host_cfg = dict(current_config)
                                host_cfg['host'] = prev_tokens[0]
                                host_cfg['__host_tokens'] = list(prev_tokens)
                                self.parse_host_config(host_cfg, source=cfg_file)
                            else:
                                for token in prev_tokens:
                                    host_cfg = dict(current_config)
                                    host_cfg['host'] = token
                                    host_cfg['__host_tokens'] = list(prev_tokens)
                                    connection_data = self.parse_host_config(host_cfg, source=cfg_file)
                                    if connection_data:
                                        connection_data['source'] = cfg_file
                                        nickname = connection_data.get('nickname', '')
                                        existing = existing_by_nickname.get(nickname)
                                        if existing:
                                            existing.update_data(connection_data)
                                            self.connections.append(existing)
                                        else:
                                            self.connections.append(Connection(connection_data))
                        current_hosts = tokens
                        current_config = {}
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
                if current_hosts and current_config:
                    tokens = current_hosts
                    if any('*' in t or '?' in t or t.startswith('!') for t in tokens):
                        host_cfg = dict(current_config)
                        host_cfg['host'] = tokens[0]
                        host_cfg['__host_tokens'] = list(tokens)
                        self.parse_host_config(host_cfg, source=cfg_file)
                    else:
                        for token in tokens:
                            host_cfg = dict(current_config)
                            host_cfg['host'] = token
                            host_cfg['__host_tokens'] = list(tokens)
                            connection_data = self.parse_host_config(host_cfg, source=cfg_file)
                            if connection_data:
                                connection_data['source'] = cfg_file
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

            raw_tokens = config.get('__host_tokens')
            tokens = [_unwrap(t) for t in raw_tokens] if raw_tokens else [host_token]

            config = dict(config)
            config.pop('__host_tokens', None)
            config.pop('aliases', None)

            # Detect wildcard or negated host tokens (e.g., '*', '?', '!pattern')
            if any('*' in t or '?' in t or t.startswith('!') for t in tokens):
                if not hasattr(self, 'rules'):
                    self.rules = []
                rule_block = dict(config)
                rule_block['host'] = host_token
                if source:
                    rule_block['source'] = source
                self.rules.append(rule_block)
                return None

            host = host_token

            # Determine whether the config explicitly defined a HostName value.
            has_explicit_hostname = 'hostname' in config and str(config['hostname']).strip() != ''
            hostname_value = config['hostname'] if has_explicit_hostname else None
            parsed_host = _unwrap(hostname_value) if has_explicit_hostname else ''

            # Extract relevant configuration
            parsed = {
                'nickname': host,
                # Keep HostName empty when it was omitted in the original
                # configuration but record the label separately via ``host`` so
                # consumers can fall back to the alias when needed.
                'hostname': parsed_host,
                'host': host,

                'port': int(_unwrap(config.get('port', 22))),
                'username': _unwrap(config.get('user', getpass.getuser())),
                # previously: 'private_key': config.get('identityfile'),

                'keyfile': os.path.expanduser(_unwrap(config.get('identityfile'))) if config.get('identityfile') else '',
                'certificate': os.path.expanduser(_unwrap(config.get('certificatefile'))) if config.get('certificatefile') else '',
                'forwarding_rules': []
            }
            if has_explicit_hostname:
                parsed['aliases'] = []
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
                pj = config['proxyjump']
                if isinstance(pj, list):
                    parsed['proxy_jump'] = [p.strip() for p in pj]
                else:
                    parsed['proxy_jump'] = [p.strip() for p in re.split(r'[\s,]+', pj)]
            if 'forwardagent' in config:
                fa = str(config.get('forwardagent', '')).strip().lower()
                parsed['forward_agent'] = fa in ('yes', 'true', '1', 'on')
            
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

            # Key selection mode defaults: prefer "specific key" when IdentityFile is explicit
            keyfile_value = parsed.get('keyfile', '')
            keyfile_path = keyfile_value.strip() if isinstance(keyfile_value, str) else ''
            has_specific_key = bool(keyfile_path and not keyfile_path.lower().startswith('select key file'))
            try:
                ident_only_raw = config.get('identitiesonly')
                ident_only = ''
                if isinstance(ident_only_raw, str):
                    ident_only = ident_only_raw.strip().lower()

                if ident_only in ('yes', 'true', '1', 'on'):
                    parsed['key_select_mode'] = 1
                elif ident_only in ('no', 'false', '0', 'off'):
                    parsed['key_select_mode'] = 2 if has_specific_key else 0
                elif ident_only_raw is None or (isinstance(ident_only_raw, str) and not ident_only_raw.strip()):
                    parsed['key_select_mode'] = 2 if has_specific_key else 0
                else:
                    parsed['key_select_mode'] = 0
            except Exception:
                parsed['key_select_mode'] = 2 if has_specific_key else 0

            # Determine authentication method
            try:
                prefer_auth_raw = str(config.get('preferredauthentications', '')).strip()
                # Split into an ordered list while normalizing case
                prefer_auth_list = [p.strip().lower() for p in prefer_auth_raw.split(',') if p.strip()]
                parsed['preferred_authentications'] = prefer_auth_list

                pubkey_auth = str(config.get('pubkeyauthentication', '')).strip().lower()
                parsed['pubkey_auth_no'] = (pubkey_auth == 'no')

                if pubkey_auth == 'no':
                    parsed['auth_method'] = 1
                else:
                    # Determine based on first occurrence of publickey or password
                    idx_pubkey = prefer_auth_list.index('publickey') if 'publickey' in prefer_auth_list else None
                    idx_password = prefer_auth_list.index('password') if 'password' in prefer_auth_list else None

                    if idx_pubkey is not None and (idx_password is None or idx_pubkey < idx_password):
                        parsed['auth_method'] = 0
                    elif idx_password is not None and (idx_pubkey is None or idx_password < idx_pubkey):
                        parsed['auth_method'] = 1
                    else:
                        parsed['auth_method'] = 0
            except Exception:
                parsed['auth_method'] = 0
            
            # Parse extra SSH config options (custom options not handled by standard fields)
            extra_config_lines = []
            # Only include options that are explicitly handled by the main UI fields
            standard_options = {
                'host', 'hostname', 'aliases', 'port', 'user', 'identityfile', 'certificatefile',
                'forwardx11', 'localforward', 'remoteforward', 'dynamicforward',
                'proxycommand', 'proxyjump', 'forwardagent', 'localcommand', 'remotecommand', 'requesttty',
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
        """Auto-detect SSH keys in configured SSH directories."""
        search_dirs = []
        if getattr(self, 'isolated_mode', False):
            search_dirs.append(get_config_dir())
        search_dirs.append(get_ssh_dir())

        keys: List[str] = []
        seen = set()
        for ssh_dir in search_dirs:
            if not os.path.exists(ssh_dir):
                continue
            try:
                for filename in os.listdir(ssh_dir):
                    if filename.endswith('.pub'):
                        private_key = os.path.join(ssh_dir, filename[:-4])
                        if os.path.exists(private_key) and private_key not in seen:
                            keys.append(private_key)
                            seen.add(private_key)
            except Exception as e:
                logger.error(f"Failed to load SSH keys from {ssh_dir}: {e}")

        logger.info(f"Found {len(keys)} SSH keys: {keys}")
        return keys

    def store_password(self, host: str, username: str, password: str):
        """Store password securely in system keyring"""
        # Prefer libsecret on Linux when available
        if not is_macos() and self.libsecret_available and _SECRET_SCHEMA is not None:
            try:
                attributes = {
                    'application': _SERVICE_NAME,
                    'type': 'ssh_password',
                    'host': host,
                    'username': username,
                }
                Secret.password_store_sync(
                    _SECRET_SCHEMA,
                    attributes,
                    Secret.COLLECTION_DEFAULT,
                    f'{_SERVICE_NAME}: {username}@{host}',
                    password,
                    None,
                )
                logger.debug(f"Password stored for {username}@{host} via libsecret")
                return True
            except Exception as e:
                logger.error(f"Failed to store password (libsecret): {e}")

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
        # Try libsecret first
        if not is_macos() and self.libsecret_available and _SECRET_SCHEMA is not None:
            try:
                attributes = {
                    'application': _SERVICE_NAME,
                    'type': 'ssh_password',
                    'host': host,
                    'username': username,
                }
                password = Secret.password_lookup_sync(_SECRET_SCHEMA, attributes, None)
                if password is not None:
                    logger.debug(f"Password retrieved for {username}@{host} via libsecret")
                    return password
            except Exception as e:
                logger.error(f"Error retrieving password (libsecret) for {username}@{host}: {e}")

        # Fallback to keyring
        if keyring is not None:
            try:
                pw = keyring.get_password(_SERVICE_NAME, f"{username}@{host}")
                if pw:
                    logger.debug(f"Password retrieved for {username}@{host} via keyring")
                return pw
            except Exception as e:
                logger.error(
                    f"Error retrieving password (keyring) for {username}@{host}: {e}"
                )
        return None

    def delete_password(self, host: str, username: str) -> bool:
        """Delete stored password for host/user from system keyring"""
        removed_any = False
        # Try libsecret first
        if not is_macos() and self.libsecret_available and _SECRET_SCHEMA is not None:
            try:
                attributes = {
                    'application': _SERVICE_NAME,
                    'type': 'ssh_password',
                    'host': host,
                    'username': username,
                }
                removed_any = Secret.password_clear_sync(_SECRET_SCHEMA, attributes, None) or removed_any
            except Exception as e:
                logger.error(f"Error deleting password (libsecret) for {username}@{host}: {e}")

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
        # Use the unified passphrase storage from askpass_utils
        return store_passphrase(key_path, passphrase)

    def get_key_passphrase(self, key_path: str) -> Optional[str]:
        """Retrieve key passphrase from system keyring"""
        # Use the unified passphrase lookup from askpass_utils
        passphrase = lookup_passphrase(key_path)
        return passphrase if passphrase else None

    def delete_key_passphrase(self, key_path: str) -> bool:
        """Delete stored key passphrase from system keyring"""
        # Use the unified passphrase clearing from askpass_utils
        return clear_passphrase(key_path)

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

        host = data.get('hostname') or data.get('host', '')
        nickname = data.get('nickname') or host
        primary_token = _quote_token(nickname)
        lines = [f"Host {primary_token}"]

        # Add basic connection info
        if host and host != nickname:
            lines.append(f"    HostName {host}")
        lines.append(f"    User {data.get('username', '')}")
        
        # Add port if specified and not default
        port = data.get('port')
        if port and port != 22:  # Only add port if it's not the default 22
            lines.append(f"    Port {port}")

        # Proxy settings
        proxy_jump = data.get('proxy_jump') or []
        if isinstance(proxy_jump, str):
            proxy_jump = [h.strip() for h in re.split(r'[\s,]+', proxy_jump) if h.strip()]
        if proxy_jump:
            lines.append(f"    ProxyJump {','.join(proxy_jump)}")
        if data.get('forward_agent'):
            lines.append("    ForwardAgent yes")

        # Add IdentityFile/IdentitiesOnly per selection when auth is key-based
        keyfile = data.get('keyfile') or data.get('private_key')
        auth_method = int(data.get('auth_method', 0) or 0)
        key_select_mode = int(data.get('key_select_mode', 0) or 0)
        dedicated_key = key_select_mode in (1, 2)
        if auth_method == 0:
            # Only write IdentityFile when using a dedicated key mode
            if dedicated_key and keyfile and keyfile.strip() and not keyfile.strip().lower().startswith('select key file'):
                if ' ' in keyfile and not (keyfile.startswith('"') and keyfile.endswith('"')):
                    keyfile = f'"{keyfile}"'
                lines.append(f"    IdentityFile {keyfile}")

                if key_select_mode == 1:
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
        cleaned_lines: List[str] = []
        seen_auth_lines = set()
        auth_keys = {
            "preferredauthentications password",
            "pubkeyauthentication no",
        }
        for line in lines:
            key = line.strip().lower()
            if auth_method == 0 and key in auth_keys:
                # Strip password-only directives when using key-based auth
                continue
            if auth_method != 0 and key in auth_keys:
                if key in seen_auth_lines:
                    # Avoid duplicates for password auth
                    continue
                seen_auth_lines.add(key)
            cleaned_lines.append(line)

        return '\n'.join(cleaned_lines)

    def get_host_block_details(self, host_identifier: str, source: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return details for the first Host block matching *host_identifier*."""
        try:
            host_identifier = (host_identifier or '').strip()
            if not host_identifier:
                return None

            target_path = source or self.ssh_config_path
            if not target_path or not os.path.exists(target_path):
                return None

            with open(target_path, 'r') as f:
                lines = f.readlines()

            i = 0
            while i < len(lines):
                raw_line = lines[i]
                lstripped = raw_line.lstrip()
                lowered = lstripped.lower()
                if lowered.startswith('host '):
                    parts = lstripped.split(None, 1)
                    full_value = parts[1].strip() if len(parts) > 1 else ''
                    try:
                        host_names = shlex.split(full_value)
                    except ValueError:
                        host_names = [h for h in full_value.split() if h]

                    if host_identifier in host_names:
                        start_index = i
                        i += 1
                        while i < len(lines) and not lines[i].lstrip().lower().startswith(('host ', 'match ')):
                            i += 1
                        end_index = i
                        block_lines = [lines[j].rstrip('\n') for j in range(start_index, end_index)]
                        return {
                            'source': target_path,
                            'hosts': host_names,
                            'start': start_index,
                            'end': end_index,
                            'lines': block_lines,
                        }
                i += 1
        except Exception as e:
            logger.debug(f"Failed to inspect host block for '{host_identifier}': {e}")
        return None

    def _split_host_block(self, original_host: str, new_data: Dict[str, Any], target_path: str) -> bool:
        """Remove *original_host* from its group and append a new block."""
        try:
            if not target_path:
                target_path = self.ssh_config_path
            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            try:
                with open(target_path, 'r') as f:
                    lines = f.readlines()
            except FileNotFoundError:
                lines = []

            updated_lines: List[str] = []
            i = 0
            found = False
            while i < len(lines):
                raw_line = lines[i]
                lstripped = raw_line.lstrip()
                lowered = lstripped.lower()
                if lowered.startswith('host '):
                    parts = lstripped.split(None, 1)
                    full_value = parts[1].strip() if len(parts) > 1 else ''
                    try:
                        host_names = shlex.split(full_value)
                    except ValueError:
                        host_names = [h for h in full_value.split() if h]

                    if not found and original_host in host_names:
                        found = True
                        remaining_hosts = [h for h in host_names if h != original_host]
                        indent_len = len(raw_line) - len(lstripped)
                        indent = raw_line[:indent_len]
                        if remaining_hosts:
                            updated_lines.append(f"{indent}Host {' '.join(remaining_hosts)}\n")
                            i += 1
                            while i < len(lines) and not lines[i].lstrip().lower().startswith(('host ', 'match ')):
                                updated_lines.append(lines[i])
                                i += 1
                        else:
                            i += 1
                            while i < len(lines) and not lines[i].lstrip().lower().startswith(('host ', 'match ')):
                                i += 1
                        continue
                updated_lines.append(raw_line)
                i += 1

            formatted_block = self.format_ssh_config_entry(new_data).rstrip('\n')
            if updated_lines:
                if not updated_lines[-1].endswith('\n'):
                    updated_lines[-1] = updated_lines[-1] + '\n'
                if updated_lines[-1].strip():
                    updated_lines.append('\n')

            updated_lines.append(formatted_block + '\n')

            with open(target_path, 'w') as f:
                f.writelines(updated_lines)

            logger.info(
                "Split host block for '%s' (found=%s) and appended dedicated entry to %s",
                original_host,
                found,
                target_path,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to split host block for '{original_host}': {e}")
            return False

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
        """Remove a host label from SSH config, or entire block if it's the only label.

        Returns True if a modification was made, False if not found or on error.
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
            modified = False

            while i < len(lines):
                raw_line = lines[i]
                lstripped = raw_line.lstrip()
                lowered = lstripped.lower()
                
                if lowered.startswith('host '):
                    parts = lstripped.split(None, 1)
                    full_value = parts[1].strip() if len(parts) > 1 else ''
                    try:
                        current_names = shlex.split(full_value) if full_value else []
                    except ValueError:
                        # Fallback to simple split if shlex fails
                        current_names = [h for h in full_value.split() if h]
                    
                    # Check if our target host is in this Host directive
                    if host_nickname in current_names:
                        modified = True
                        # Remove the target hostname from the list
                        remaining_names = [name for name in current_names if name != host_nickname]
                        
                        if remaining_names:
                            # Update the Host line with remaining names
                            indent = raw_line[:len(raw_line) - len(lstripped)]
                            # Use shlex.join if available (Python 3.8+), otherwise manual quoting
                            try:
                                remaining_hosts_str = shlex.join(remaining_names)
                            except AttributeError:
                                # Fallback for older Python versions
                                remaining_hosts_str = ' '.join(
                                    f'"{name}"' if ' ' in name or '"' in name else name 
                                    for name in remaining_names
                                )
                            updated_line = f"{indent}Host {remaining_hosts_str}\n"
                            updated_lines.append(updated_line)
                            logger.info(f"Updated Host line: removed '{host_nickname}', remaining: {remaining_names}")
                            
                            # Keep the rest of the block
                            i += 1
                            while i < len(lines) and not lines[i].lstrip().lower().startswith(('host ', 'match ')):
                                updated_lines.append(lines[i])
                                i += 1
                        else:
                            # No remaining names, delete the entire block
                            logger.info(f"Deleting entire Host block for '{host_nickname}' (was the only host)")
                            i += 1
                            # Skip the entire block
                            while i < len(lines) and not lines[i].lstrip().lower().startswith(('host ', 'match ')):
                                i += 1
                        continue
                
                # Keep line as-is
                updated_lines.append(raw_line)
                i += 1

            if modified:
                try:
                    with open(target_path, 'w') as f:
                        f.writelines(updated_lines)
                    logger.info(f"SSH config updated: {'removed' if host_nickname else 'modified'} entry for '{host_nickname}'")
                except IOError as e:
                    logger.error(f"Failed to write SSH config after delete: {e}")
                    return False
            return modified
        except Exception as e:
            logger.error(f"Error removing SSH config entry: {e}", exc_info=True)
            return False

    def update_connection(self, connection: Connection, new_data: Dict[str, Any]) -> bool:
        """Update an existing connection"""
        try:
            split_from_group = bool(new_data.pop('__split_from_group', False))
            split_source_override = new_data.pop('__split_source', None)
            split_original_host = new_data.pop('__split_original_nickname', None)

            target_path = split_source_override or new_data.get('source') or getattr(connection, 'source', self.ssh_config_path)
            logger.info(
                "Updating connection '%s' → writing to %s (rules=%d)",
                connection.nickname,
                target_path,
                len(new_data.get('forwarding_rules', []) or [])
            )
            # Capture previous identifiers for credential cleanup
            prev_host = (
                getattr(connection, 'hostname', '')
                or getattr(connection, 'host', '')
                or getattr(connection, 'nickname', '')
            )
            prev_user = getattr(connection, 'username', '')
            original_nickname = getattr(connection, 'nickname', '')

            # Update existing object IN-PLACE instead of creating new ones
            connection.update_data(new_data)

            # Update the SSH config file with original nickname for proper matching
            if split_from_group:
                original_token = split_original_host or original_nickname
                if not self._split_host_block(original_token, new_data, target_path):
                    logger.error("Failed to split host block for %s", original_token)
                    return False
            else:
                self.update_ssh_config_file(connection, new_data, original_nickname)

            # Handle password storage/removal
            if 'password' in new_data:
                pwd = new_data.get('password') or ''
                # Determine current identifiers after update
                curr_host = (
                    new_data.get('hostname')
                    or new_data.get('host')
                    or getattr(connection, 'hostname', '')
                    or getattr(connection, 'host', '')
                    or getattr(connection, 'nickname', '')
                )
                curr_user = new_data.get('username') or getattr(connection, 'username', prev_user)
                if pwd and curr_host:
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
                host_identifier = (
                    connection.get_effective_host()
                    if hasattr(connection, 'get_effective_host')
                    else connection.hostname
                )
                self.delete_password(host_identifier, connection.username)

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

    def _get_active_connection_key(self, connection: Connection) -> str:
        """Return the dictionary key used to track active connection tasks."""
        try:
            key = connection.get_connection_key()
        except AttributeError:
            host = getattr(connection, 'hostname', '') or getattr(connection, 'host', '')
            username = getattr(connection, 'username', '')
            if username and host:
                key = f"{username}@{host}"
            elif host:
                key = host
            elif username:
                key = username
            else:
                key = ''
        if not key:
            key = f"connection-{id(connection)}"
        return key

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
            
            # Determine the tracking key used for keepalive management
            key = self._get_active_connection_key(connection, prefer_stored=False)
            existing_key = self._active_connection_keys.get(id(connection))
            if existing_key and existing_key != key:
                old_task = self.active_connections.pop(existing_key, None)
                if old_task:
                    old_task.cancel()
            self._active_connection_keys[id(connection)] = key

            # Store the connection task
            if key in self.active_connections:
                self.active_connections[key].cancel()

            
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
            self.active_connections[key] = task

            
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
            key = self._get_active_connection_key(connection)
            if key in self.active_connections:
                self.active_connections[key].cancel()
                try:
                    await self.active_connections[key]
                except asyncio.CancelledError:
                    pass
                del self.active_connections[key]
            self._active_connection_keys.pop(id(connection), None)

            
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