"""Utilities for SCP file transfers using ssh_connection_builder."""

from __future__ import annotations

import os
import logging
import subprocess
from typing import Iterable, List, Tuple, Optional, Dict, Any
import re

from .ssh_connection_builder import build_ssh_connection, ConnectionContext
from .ssh_password_exec import run_scp_with_password, assemble_scp_transfer_args

logger = logging.getLogger(__name__)


def _format_ssh_target(host: str, user: str) -> str:
    """Format SSH target as user@host."""
    host_component = host or ''
    if host_component and ':' in host_component and not (
        host_component.startswith('[') and host_component.endswith(']')
    ):
        host_component = f'[{host_component}]'
    return f'{user}@{host_component}' if user else host_component


def _create_connection_for_scp(
    host: str,
    user: str,
    port: int = 22,
    keyfile: Optional[str] = None,
    key_mode: Optional[int] = None,
    auth_method: int = 0,
    connection_manager: Optional[Any] = None,
    known_hosts_path: Optional[str] = None,
    extra_ssh_config: Optional[str] = None,
) -> Any:
    """Create a minimal connection object for SCP operations."""
    class SCPConnection:
        def __init__(self):
            self.hostname = host
            self.host = host
            self.nickname = host
            self.username = user or ''
            self.port = port
            self.keyfile = keyfile or ''
            self.key_select_mode = key_mode or 0
            self.auth_method = auth_method
            self.extra_ssh_config = extra_ssh_config or ''
            self.identity_agent_disabled = False
            
            # Try to resolve host identifier
            try:
                if hasattr(connection_manager, 'get_effective_ssh_config'):
                    effective_config = connection_manager.get_effective_ssh_config(host)
                    if effective_config:
                        # Check for IdentityAgent
                        identity_agent = effective_config.get('identityagent', '')
                        if isinstance(identity_agent, list):
                            identity_agent = identity_agent[0] if identity_agent else ''
                        if str(identity_agent).lower() == 'none':
                            self.identity_agent_disabled = True
            except Exception:
                pass
    
    return SCPConnection()


def download_file(
    host: str,
    user: str,
    remote_file: str,
    local_path: str,
    *,
    recursive: bool = False,
    port: int = 22,
    password: Optional[str] = None,
    known_hosts_path: Optional[str] = None,
    extra_ssh_opts: Optional[List[str]] = None,
    use_publickey: bool = False,
    inherit_env: Optional[Dict[str, str]] = None,
    saved_passphrase: Optional[str] = None,
    keyfile: Optional[str] = None,
    key_mode: Optional[int] = None,
    connection_manager: Optional[Any] = None,
    config: Optional[Any] = None,
) -> bool:
    """Download a remote file (or directory when ``recursive``) via SCP using ssh_connection_builder."""
    if not host or not remote_file or not local_path:
        return False

    env = (inherit_env or os.environ).copy()
    
    # Check if the inherited environment has askpass configured (e.g., when identity agent is disabled)
    has_inherited_askpass = bool(
        inherit_env
        and str(inherit_env.get('SSH_ASKPASS_REQUIRE') or '').lower() == 'force'
    )

    # Create connection object for ssh_connection_builder
    auth_method = 1 if password else 0
    connection = _create_connection_for_scp(
        host=host,
        user=user,
        port=port,
        keyfile=keyfile,
        key_mode=key_mode,
        auth_method=auth_method,
        connection_manager=connection_manager,
        known_hosts_path=known_hosts_path,
    )
    
    # If password is provided, set it on connection
    if password:
        connection.password = password
    
    # Build SSH connection command using ssh_connection_builder
    try:
        ctx = ConnectionContext(
            connection=connection,
            connection_manager=connection_manager,
            config=config,
            command_type='scp',
            extra_args=[],
            port_forwarding_rules=None,
            remote_command=None,
            local_command=None,
            extra_ssh_config=None,  # Will be handled via extra_ssh_opts
            known_hosts_path=known_hosts_path,
            native_mode=False,
            quick_connect_mode=False,
            quick_connect_command=None,
        )
        
        ssh_conn_cmd = build_ssh_connection(ctx)
        base_cmd = ssh_conn_cmd.command
        env.update(ssh_conn_cmd.env)
        
        # Get password for sshpass if needed
        password_value = None
        if ssh_conn_cmd.use_sshpass and ssh_conn_cmd.password:
            password_value = ssh_conn_cmd.password
        
    except Exception as e:
        logger.error(f'SCP: Failed to build SSH connection: {e}')
        return False

    # Handle password authentication with sshpass
    if password_value:
        try:
            target = _format_ssh_target(host, user)
            result = run_scp_with_password(
                host,
                user,
                password_value,
                [remote_file],
                local_path,
                direction='download',
                port=port,
                known_hosts_path=known_hosts_path,
                extra_ssh_opts=extra_ssh_opts or [],
                inherit_env=env,
                use_publickey=use_publickey,
            )
            return result.returncode == 0
        except Exception as exc:
            logger.error('SCP: Download failed for %s: %s', remote_file, exc)
            return False

    # Build SCP command
    target = _format_ssh_target(host, user)
    try:
        transfer_sources, transfer_destination = assemble_scp_transfer_args(
            target,
            [remote_file],
            local_path,
            'download',
        )
        
        # Start with base command from ssh_connection_builder
        # Replace 'ssh' with 'scp' and adjust arguments
        argv = ['scp']
        
        # Add port (SCP uses -P, not -p)
        if port != 22:
            argv.extend(['-P', str(port)])
        
        # Add recursive flag if needed
        if recursive:
            argv.append('-r')
        
        # Add known hosts file
        if known_hosts_path:
            argv.extend(['-o', f'UserKnownHostsFile={known_hosts_path}'])
        else:
            argv.extend(['-o', 'StrictHostKeyChecking=accept-new'])
        
        # Add extra SSH options from base command (skip -i, -o options that are already handled)
        # Extract -o options from base_cmd
        base_opts = []
        i = 0
        while i < len(base_cmd):
            if base_cmd[i] == '-o' and i + 1 < len(base_cmd):
                base_opts.append(base_cmd[i])
                base_opts.append(base_cmd[i + 1])
                i += 2
            elif base_cmd[i] == '-i' and i + 1 < len(base_cmd):
                # Identity file - add it
                base_opts.append(base_cmd[i])
                base_opts.append(base_cmd[i + 1])
                i += 2
            elif base_cmd[i] in ['-v', '-C']:
                # Verbosity and compression flags
                base_opts.append(base_cmd[i])
                i += 1
            else:
                i += 1
        
        # Add base options
        argv.extend(base_opts)
        
        # Add extra SSH options from caller
        if extra_ssh_opts:
            argv.extend(extra_ssh_opts)
        
        # Add transfer sources and destination
        argv.extend(transfer_sources)
        argv.append(transfer_destination)
        
        completed = subprocess.run(
            argv,
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or '').strip()
            if stderr:
                logger.error('SCP: Download stderr: %s', stderr)
            return False
        return True
    except Exception as exc:
        logger.error('SCP: Download failed for %s: %s', remote_file, exc)
        return False


def upload_file(
    host: str,
    user: str,
    local_file: str,
    remote_path: str,
    *,
    recursive: bool = False,
    port: int = 22,
    password: Optional[str] = None,
    known_hosts_path: Optional[str] = None,
    extra_ssh_opts: Optional[List[str]] = None,
    use_publickey: bool = False,
    inherit_env: Optional[Dict[str, str]] = None,
    saved_passphrase: Optional[str] = None,
    keyfile: Optional[str] = None,
    key_mode: Optional[int] = None,
    connection_manager: Optional[Any] = None,
    config: Optional[Any] = None,
) -> bool:
    """Upload a local file (or directory when ``recursive``) via SCP using ssh_connection_builder."""
    if not host or not local_file or not remote_path:
        return False

    env = (inherit_env or os.environ).copy()
    
    # Check if the inherited environment has askpass configured
    has_inherited_askpass = bool(
        inherit_env
        and str(inherit_env.get('SSH_ASKPASS_REQUIRE') or '').lower() == 'force'
    )

    # Create connection object for ssh_connection_builder
    auth_method = 1 if password else 0
    connection = _create_connection_for_scp(
        host=host,
        user=user,
        port=port,
        keyfile=keyfile,
        key_mode=key_mode,
        auth_method=auth_method,
        connection_manager=connection_manager,
        known_hosts_path=known_hosts_path,
    )
    
    # If password is provided, set it on connection
    if password:
        connection.password = password
    
    # Build SSH connection command using ssh_connection_builder
    try:
        ctx = ConnectionContext(
            connection=connection,
            connection_manager=connection_manager,
            config=config,
            command_type='scp',
            extra_args=[],
            port_forwarding_rules=None,
            remote_command=None,
            local_command=None,
            extra_ssh_config=None,
            known_hosts_path=known_hosts_path,
            native_mode=False,
            quick_connect_mode=False,
            quick_connect_command=None,
        )
        
        ssh_conn_cmd = build_ssh_connection(ctx)
        base_cmd = ssh_conn_cmd.command
        env.update(ssh_conn_cmd.env)
        
        # Get password for sshpass if needed
        password_value = None
        if ssh_conn_cmd.use_sshpass and ssh_conn_cmd.password:
            password_value = ssh_conn_cmd.password
        
    except Exception as e:
        logger.error(f'SCP: Failed to build SSH connection: {e}')
        return False

    # Handle password authentication with sshpass
    if password_value:
        try:
            target = _format_ssh_target(host, user)
            result = run_scp_with_password(
                host,
                user,
                password_value,
                [local_file],
                remote_path,
                direction='upload',
                port=port,
                known_hosts_path=known_hosts_path,
                extra_ssh_opts=extra_ssh_opts or [],
                inherit_env=env,
                use_publickey=use_publickey,
            )
            return result.returncode == 0
        except Exception as exc:
            logger.error('SCP: Upload failed for %s: %s', local_file, exc)
            return False

    # Build SCP command
    target = _format_ssh_target(host, user)
    try:
        transfer_sources, transfer_destination = assemble_scp_transfer_args(
            target,
            [local_file],
            remote_path,
            'upload',
        )
        
        # Start with base command from ssh_connection_builder
        # Replace 'ssh' with 'scp' and adjust arguments
        argv = ['scp']
        
        # Add port (SCP uses -P, not -p)
        if port != 22:
            argv.extend(['-P', str(port)])
        
        # Add recursive flag if needed
        if recursive:
            argv.append('-r')
        
        # Add known hosts file
        if known_hosts_path:
            argv.extend(['-o', f'UserKnownHostsFile={known_hosts_path}'])
        else:
            argv.extend(['-o', 'StrictHostKeyChecking=accept-new'])
        
        # Add extra SSH options from base command
        base_opts = []
        i = 0
        while i < len(base_cmd):
            if base_cmd[i] == '-o' and i + 1 < len(base_cmd):
                base_opts.append(base_cmd[i])
                base_opts.append(base_cmd[i + 1])
                i += 2
            elif base_cmd[i] == '-i' and i + 1 < len(base_cmd):
                # Identity file - add it
                base_opts.append(base_cmd[i])
                base_opts.append(base_cmd[i + 1])
                i += 2
            elif base_cmd[i] in ['-v', '-C']:
                # Verbosity and compression flags
                base_opts.append(base_cmd[i])
                i += 1
            else:
                i += 1
        
        # Add base options
        argv.extend(base_opts)
        
        # Add extra SSH options from caller
        if extra_ssh_opts:
            argv.extend(extra_ssh_opts)
        
        # Add transfer sources and destination
        argv.extend(transfer_sources)
        argv.append(transfer_destination)
        
        completed = subprocess.run(
            argv,
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or '').strip()
            if stderr:
                logger.error('SCP: Upload stderr: %s', stderr)
            return False
        return True
    except Exception as exc:
        logger.error('SCP: Upload failed for %s: %s', local_file, exc)
        return False


__all__ = ['assemble_scp_transfer_args', 'download_file', 'upload_file']
