# ssh_connection_builder.py
"""
Unified SSH connection builder that uses SSH config as primary source of truth.

This module provides a single, consistent way to build SSH commands for all
components (terminal, SCP, SFTP, ssh-copy-id) that matches default SSH behavior.
"""
import os
import logging
import subprocess
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass

from .ssh_config_utils import get_effective_ssh_config
from .askpass_utils import (
    get_ssh_env_with_askpass,
    ensure_key_in_agent,
    lookup_passphrase,
)
from .ssh_password_exec import run_ssh_with_password, run_scp_with_password

logger = logging.getLogger(__name__)


@dataclass
class SSHConnectionCommand:
    """Represents a prepared SSH connection command."""
    command: List[str]  # The SSH command to execute
    env: Dict[str, str]  # Environment variables
    use_sshpass: bool = False  # Whether to use sshpass
    password: Optional[str] = None  # Password for sshpass (if needed)
    use_askpass: bool = False  # Whether to use SSH_ASKPASS


@dataclass
class ConnectionContext:
    """Context for building SSH connections."""
    connection: any  # Connection object
    connection_manager: Optional[any] = None  # ConnectionManager instance
    config: Optional[any] = None  # Config instance
    command_type: str = 'ssh'  # 'ssh', 'scp', 'sftp', 'ssh-copy-id'
    extra_args: List[str] = None  # Extra arguments to append


def _get_ssh_config_value(
    config: Dict[str, Union[str, List[str]]],
    key: str,
    default: Optional[str] = None
) -> Optional[str]:
    """Get a value from SSH config, handling both single values and lists."""
    value = config.get(key.lower())
    if value is None:
        return default
    if isinstance(value, list):
        return value[-1] if value else default
    return value


def _get_ssh_config_list(
    config: Dict[str, Union[str, List[str]]],
    key: str
) -> List[str]:
    """Get a list value from SSH config."""
    value = config.get(key.lower())
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value] if value else []


def _should_add_keys_to_agent(config: Dict[str, Union[str, List[str]]]) -> bool:
    """Check if AddKeysToAgent directive requires adding keys to agent."""
    add_keys = _get_ssh_config_value(config, 'addkeystoagent', '').lower()
    return add_keys in ('yes', 'ask', 'confirm')


def _is_identity_agent_disabled(config: Dict[str, Union[str, List[str]]]) -> bool:
    """Check if IdentityAgent is disabled."""
    identity_agent = _get_ssh_config_value(config, 'identityagent', '').lower()
    return identity_agent == 'none'


def _prepare_key_for_connection(
    key_path: str,
    config: Dict[str, Union[str, List[str]]],
    connection_manager: Optional[any] = None
) -> bool:
    """
    Prepare SSH key for connection.
    Only adds to agent if AddKeysToAgent is enabled in SSH config.
    """
    if not key_path or not os.path.isfile(key_path):
        return False
    
    # Check if IdentityAgent is disabled
    if _is_identity_agent_disabled(config):
        logger.debug(f"IdentityAgent disabled; skipping key preparation: {key_path}")
        return False
    
    # Check if AddKeysToAgent requires adding to agent
    if not _should_add_keys_to_agent(config):
        logger.debug(f"AddKeysToAgent not enabled; skipping key preparation (default SSH behavior): {key_path}")
        return False
    
    # Add key to agent
    try:
        if connection_manager and hasattr(connection_manager, 'prepare_key_for_connection'):
            return connection_manager.prepare_key_for_connection(key_path)
        else:
            return ensure_key_in_agent(key_path)
    except Exception as e:
        logger.warning(f"Error preparing key for connection: {e}")
        return False


def _get_stored_password(
    connection: any,
    connection_manager: Optional[any] = None
) -> Optional[str]:
    """Get stored password for connection."""
    if not connection_manager:
        return None
    
    try:
        host = getattr(connection, 'hostname', '') or getattr(connection, 'host', '')
        username = getattr(connection, 'username', '')
        if host and username:
            return connection_manager.get_password(host, username)
    except Exception:
        pass
    return None


def _get_stored_passphrase(
    key_path: str,
    connection_manager: Optional[any] = None
) -> Optional[str]:
    """Get stored passphrase for key."""
    if not key_path or not connection_manager:
        return None
    
    try:
        if hasattr(connection_manager, 'get_key_passphrase'):
            return connection_manager.get_key_passphrase(key_path)
    except Exception:
        pass
    
    # Fallback to direct lookup
    try:
        return lookup_passphrase(key_path)
    except Exception:
        pass
    
    return None


def _build_base_ssh_command(
    connection: any,
    config: Dict[str, Union[str, List[str]]],
    app_config: Optional[any] = None,
    command_type: str = 'ssh'
) -> List[str]:
    """
    Build base SSH command from SSH config and connection settings.
    This matches default SSH behavior as closely as possible.
    """
    # Determine the SSH binary
    if command_type == 'scp':
        cmd = ['scp']
    elif command_type == 'sftp':
        cmd = ['sftp']
    elif command_type == 'ssh-copy-id':
        cmd = ['ssh-copy-id']
    else:
        cmd = ['ssh']
    
    # Get app-level SSH settings
    app_ssh_config = {}
    if app_config:
        try:
            app_ssh_config = app_config.get_ssh_config() if hasattr(app_config, 'get_ssh_config') else {}
        except Exception:
            pass
    
    # Apply app-level overrides (verbosity, compression, etc.)
    verbosity = int(app_ssh_config.get('verbosity', 0) or 0)
    if verbosity > 0:
        for _ in range(min(verbosity, 3)):
            cmd.append('-v')
    
    compression = bool(app_ssh_config.get('compression', False))
    if compression:
        cmd.append('-C')
    
    # Apply connection timeout if specified
    connect_timeout = app_ssh_config.get('connection_timeout')
    if connect_timeout:
        try:
            timeout = int(connect_timeout)
            if timeout > 0:
                cmd.extend(['-o', f'ConnectTimeout={timeout}'])
        except (ValueError, TypeError):
            pass
    
    # Apply connection attempts if specified
    connection_attempts = app_ssh_config.get('connection_attempts')
    if connection_attempts:
        try:
            attempts = int(connection_attempts)
            if attempts > 0:
                cmd.extend(['-o', f'ConnectionAttempts={attempts}'])
        except (ValueError, TypeError):
            pass
    
    # Apply keepalive settings if specified
    keepalive_interval = app_ssh_config.get('keepalive_interval')
    if keepalive_interval:
        try:
            interval = int(keepalive_interval)
            if interval > 0:
                cmd.extend(['-o', f'ServerAliveInterval={interval}'])
        except (ValueError, TypeError):
            pass
    
    keepalive_count = app_ssh_config.get('keepalive_count_max')
    if keepalive_count:
        try:
            count = int(keepalive_count)
            if count > 0:
                cmd.extend(['-o', f'ServerAliveCountMax={count}'])
        except (ValueError, TypeError):
            pass
    
    # Apply strict host key checking
    strict_host = str(app_ssh_config.get('strict_host_key_checking', '') or '').strip()
    auto_add = bool(app_ssh_config.get('auto_add_host_keys', True))
    
    if strict_host:
        cmd.extend(['-o', f'StrictHostKeyChecking={strict_host}'])
    elif auto_add:
        cmd.extend(['-o', 'StrictHostKeyChecking=accept-new'])
    
    # Apply port if specified and not default
    port = getattr(connection, 'port', None)
    if port and port != 22:
        if command_type == 'scp':
            cmd.extend(['-P', str(port)])
        else:
            cmd.extend(['-p', str(port)])
    
    # Apply IdentityFile from SSH config (SSH config is primary source)
    identity_files = _get_ssh_config_list(config, 'identityfile')
    for identity_file in identity_files:
        if identity_file and os.path.isfile(os.path.expanduser(identity_file)):
            cmd.extend(['-i', os.path.expanduser(identity_file)])
            # If IdentitiesOnly is set, only use this key
            if _get_ssh_config_value(config, 'identitiesonly', '').lower() in ('yes', 'true', '1'):
                cmd.extend(['-o', 'IdentitiesOnly=yes'])
                break  # Only first key when IdentitiesOnly is set
    
    # Apply CertificateFile if specified
    cert_file = _get_ssh_config_value(config, 'certificatefile')
    if cert_file and cert_file.strip() and os.path.isfile(os.path.expanduser(cert_file)):
        cmd.extend(['-o', f'CertificateFile={cert_file}'])
    
    # Apply ProxyCommand/ProxyJump if specified
    proxy_command = _get_ssh_config_value(config, 'proxycommand')
    if proxy_command and proxy_command.strip():
        cmd.extend(['-o', f'ProxyCommand={proxy_command}'])
    
    proxy_jump = _get_ssh_config_list(config, 'proxyjump')
    if proxy_jump:
        # Filter out empty values
        proxy_jump_filtered = [j for j in proxy_jump if j and j.strip()]
        if proxy_jump_filtered:
            cmd.extend(['-o', f'ProxyJump={",".join(proxy_jump_filtered)}'])
    
    # Apply X11 forwarding if enabled
    forward_x11 = _get_ssh_config_value(config, 'forwardx11', '').lower()
    if forward_x11 in ('yes', 'true', '1'):
        cmd.append('-X')
    
    # Apply other SSH config options that should be passed through
    # (SSH config is primary source, so we trust it)
    # Note: We don't override PreferredAuthentications here - let SSH config handle it
    
    return cmd


def build_ssh_connection(
    ctx: ConnectionContext
) -> SSHConnectionCommand:
    """
    Build a complete SSH connection command.
    
    This is the unified entry point for all SSH operations:
    - Terminal connections
    - SCP transfers
    - SFTP connections
    - ssh-copy-id operations
    
    Args:
        ctx: Connection context with connection, manager, config, etc.
    
    Returns:
        SSHConnectionCommand with command, env, and authentication info
    """
    connection = ctx.connection
    connection_manager = ctx.connection_manager
    app_config = ctx.config
    command_type = ctx.command_type
    extra_args = ctx.extra_args or []
    
    # Get effective SSH config for this connection (primary source of truth)
    host_label = getattr(connection, 'nickname', '') or \
                 getattr(connection, 'host', '') or \
                 getattr(connection, 'hostname', '')
    
    if not host_label:
        raise ValueError("Connection must have a host identifier")
    
    # Check for config override (isolated mode, etc.)
    config_override = None
    if hasattr(connection, '_resolve_config_override_path'):
        try:
            config_override = connection._resolve_config_override_path()
        except Exception:
            pass
    
    # Get effective SSH config
    try:
        if config_override:
            effective_config = get_effective_ssh_config(host_label, config_file=config_override)
        else:
            effective_config = get_effective_ssh_config(host_label)
    except Exception as e:
        logger.warning(f"Failed to get effective SSH config: {e}")
        effective_config = {}
    
    # Build base command
    base_cmd = _build_base_ssh_command(connection, effective_config, app_config, command_type)
    
    # Determine authentication method
    auth_method = int(getattr(connection, 'auth_method', 0) or 0)
    password_auth_selected = (auth_method == 1)
    
    # Check for stored password
    stored_password = _get_stored_password(connection, connection_manager)
    has_stored_password = bool(stored_password)
    
    # Determine if we need password authentication
    use_password_auth = password_auth_selected or (has_stored_password and not password_auth_selected)
    
    # Handle key-based authentication
    use_askpass = False
    if not password_auth_selected:
        # Get key file from connection (if explicitly set) or SSH config
        key_file = getattr(connection, 'keyfile', '') or ''
        key_select_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
        
        # Determine if we need askpass based on IdentityAgent and key preparation
        identity_agent_disabled = _is_identity_agent_disabled(effective_config)
        key_prepared = False
        key_path = None
        
        # If connection has explicit key file and key_select_mode is set, use it
        if key_file and not key_file.startswith('Select key file') and key_select_mode in (1, 2):
            # Connection explicitly specifies a key - add it to command if not already there
            key_path = os.path.expanduser(key_file)
            if os.path.isfile(key_path) and f'-i {key_path}' not in ' '.join(base_cmd):
                # Key will be added by _build_base_ssh_command from SSH config
                # But if it's not in SSH config, we need to add it here
                identity_files = _get_ssh_config_list(effective_config, 'identityfile')
                if not any(os.path.samefile(os.path.expanduser(f), key_path) if os.path.exists(os.path.expanduser(f)) else False for f in identity_files):
                    base_cmd.extend(['-i', key_path])
                    if key_select_mode == 1:
                        base_cmd.extend(['-o', 'IdentitiesOnly=yes'])
            
            # Prepare key (add to agent if AddKeysToAgent is set)
            if os.path.isfile(key_path):
                key_prepared = _prepare_key_for_connection(key_path, effective_config, connection_manager)
        else:
            # Use SSH config keys or default behavior
            identity_files = _get_ssh_config_list(effective_config, 'identityfile')
            if identity_files:
                # Try to prepare first identity file
                first_key = os.path.expanduser(identity_files[0])
                if os.path.isfile(first_key):
                    key_path = first_key
                    key_prepared = _prepare_key_for_connection(first_key, effective_config, connection_manager)
        
        # Set up askpass based on whether IdentityAgent is disabled or key needs passphrase
        # If IdentityAgent is disabled, we MUST use askpass (force mode)
        # If key was prepared (added to agent), we can use prefer mode (SSH will use agent first)
        # If key was not prepared but exists, use prefer mode (SSH will ask if needed)
        if identity_agent_disabled:
            # IdentityAgent disabled - must use askpass to supply passphrase
            env = get_ssh_env_with_askpass(require="force")
            use_askpass = True
        elif key_path and os.path.isfile(key_path):
            # Key exists - use askpass in prefer mode (SSH will use agent if key is there, askpass if not)
            # This allows SSH to use the agent if the key was added, or askpass if it needs the passphrase
            env = get_ssh_env_with_askpass(require="prefer")
            use_askpass = True
        else:
            # No specific key - use default SSH behavior
            env = os.environ.copy()
    else:
        # Password authentication selected
        env = os.environ.copy()
        # Remove askpass so SSH can prompt for password if needed
        env.pop('SSH_ASKPASS', None)
        env.pop('SSH_ASKPASS_REQUIRE', None)
    
    # Add host to command
    username = getattr(connection, 'username', '')
    hostname = getattr(connection, 'hostname', '') or getattr(connection, 'host', '')
    
    if command_type == 'scp':
        # SCP command format is handled by caller
        pass
    elif command_type == 'ssh-copy-id':
        # ssh-copy-id format: ssh-copy-id [options] user@host
        if username and hostname:
            base_cmd.append(f"{username}@{hostname}")
        else:
            base_cmd.append(host_label)
    else:
        # SSH/SFTP format: ssh [options] user@host [command]
        if username and hostname:
            base_cmd.append(f"{username}@{hostname}")
        else:
            base_cmd.append(host_label)
    
    # Add extra arguments
    if extra_args:
        base_cmd.extend(extra_args)
    
    # Determine if we need sshpass
    use_sshpass = False
    password_for_sshpass = None
    
    if use_password_auth and stored_password:
        use_sshpass = True
        password_for_sshpass = stored_password
    
    return SSHConnectionCommand(
        command=base_cmd,
        env=env,
        use_sshpass=use_sshpass,
        password=password_for_sshpass,
        use_askpass=use_askpass
    )


def execute_ssh_connection(
    cmd: SSHConnectionCommand,
    **subprocess_kwargs
) -> subprocess.CompletedProcess:
    """
    Execute an SSH connection command.
    
    Handles sshpass wrapping and environment setup automatically.
    """
    if cmd.use_sshpass and cmd.password:
        # Use sshpass for password authentication
        host = None
        user = None
        port = 22
        
        # Extract host, user, port from command
        for i, arg in enumerate(cmd.command):
            if '@' in arg and not arg.startswith('-'):
                parts = arg.split('@')
                if len(parts) == 2:
                    user = parts[0]
                    host_part = parts[1]
                    if ':' in host_part:
                        host, port_str = host_part.rsplit(':', 1)
                        try:
                            port = int(port_str)
                        except ValueError:
                            pass
                    else:
                        host = host_part
                break
        
        if not host:
            # Fallback: try to find host in command
            for arg in cmd.command:
                if not arg.startswith('-') and '@' not in arg and arg not in ['ssh', 'scp', 'sftp']:
                    host = arg
                    break
        
        if host:
            # Extract SSH options
            ssh_opts = []
            i = 0
            while i < len(cmd.command):
                if cmd.command[i] == '-o' and i + 1 < len(cmd.command):
                    ssh_opts.append('-o')
                    ssh_opts.append(cmd.command[i + 1])
                    i += 2
                elif cmd.command[i].startswith('-'):
                    ssh_opts.append(cmd.command[i])
                    i += 1
                else:
                    i += 1
            
            # Get extra args (command to run, etc.)
            extra_args = []
            found_host = False
            for arg in cmd.command:
                if '@' in arg or (not arg.startswith('-') and arg not in ['ssh', 'scp', 'sftp'] and not found_host):
                    found_host = True
                    continue
                if found_host:
                    extra_args.append(arg)
            
            if cmd.command[0] == 'scp':
                # Use SCP-specific password execution
                sources = [a for a in extra_args if not a.endswith(':')]
                destination = next((a for a in extra_args if a.endswith(':')), '')
                return run_scp_with_password(
                    host, user or '', cmd.password,
                    sources, destination,
                    port=port,
                    extra_ssh_opts=ssh_opts,
                    inherit_env=cmd.env
                )
            else:
                # Use SSH password execution
                return run_ssh_with_password(
                    host, user or '', cmd.password,
                    port=port,
                    argv_tail=extra_args,
                    extra_ssh_opts=ssh_opts,
                    inherit_env=cmd.env,
                    use_publickey=not getattr(cmd, 'force_password_only', False)
                )
    
    # Default: execute command directly
    return subprocess.run(
        cmd.command,
        env=cmd.env,
        **subprocess_kwargs
    )

