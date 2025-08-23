"""
SSH utilities for building consistent SSH options across the application
"""

import os
import logging
import subprocess
import tempfile
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

def build_connection_ssh_options(connection, config=None, for_ssh_copy_id=False):
    """Build SSH options that match the exact connection settings used for SSH.
    
    This function replicates the SSH option building logic from terminal.py
    to ensure ssh-copy-id and scp use the same settings as SSH connections.
    
    Args:
        connection: The connection object
        config: Optional config object
        for_ssh_copy_id: If True, filter out options not supported by ssh-copy-id
    """
    options = []
    
    # Read SSH behavior from config with sane defaults (same as terminal.py)
    try:
        if config is None:
            from .config import Config
            config = Config()
        ssh_cfg = config.get_ssh_config() if hasattr(config, 'get_ssh_config') else {}
    except Exception:
        ssh_cfg = {}
    
    apply_adv = bool(ssh_cfg.get('apply_advanced', False))
    connect_timeout = int(ssh_cfg.get('connection_timeout', 10)) if apply_adv else None
    connection_attempts = int(ssh_cfg.get('connection_attempts', 1)) if apply_adv else None
    keepalive_interval = int(ssh_cfg.get('keepalive_interval', 30)) if apply_adv else None
    keepalive_count = int(ssh_cfg.get('keepalive_count_max', 3)) if apply_adv else None
    strict_host = str(ssh_cfg.get('strict_host_key_checking', '')) if apply_adv else ''
    auto_add_host_keys = bool(ssh_cfg.get('auto_add_host_keys', True))
    batch_mode = bool(ssh_cfg.get('batch_mode', False)) if apply_adv else False
    compression = bool(ssh_cfg.get('compression', True)) if apply_adv else False

    # Determine auth method from connection (same as terminal.py)
    password_auth_selected = False
    try:
        # In our UI: 0 = key-based, 1 = password
        password_auth_selected = (getattr(connection, 'auth_method', 0) == 1)
    except Exception:
        password_auth_selected = False

    # Apply advanced args only when user explicitly enabled them (same as terminal.py)
    if apply_adv:
        # Only enable BatchMode when NOT doing password auth (BatchMode disables prompts)
        if batch_mode and not password_auth_selected:
            options.extend(['-o', 'BatchMode=yes'])
        if connect_timeout is not None:
            options.extend(['-o', f'ConnectTimeout={connect_timeout}'])
        if connection_attempts is not None:
            options.extend(['-o', f'ConnectionAttempts={connection_attempts}'])
        if keepalive_interval is not None:
            options.extend(['-o', f'ServerAliveInterval={keepalive_interval}'])
        if keepalive_count is not None:
            options.extend(['-o', f'ServerAliveCountMax={keepalive_count}'])
        if strict_host:
            options.extend(['-o', f'StrictHostKeyChecking={strict_host}'])
        if compression and not for_ssh_copy_id:
            options.append('-C')

    # Apply auto-add host keys policy even when advanced block is off (same as terminal.py)
    try:
        if (not strict_host) and auto_add_host_keys:
            options.extend(['-o', 'StrictHostKeyChecking=accept-new'])
    except Exception:
        pass

    # Ensure SSH exits immediately on failure rather than waiting in background (same as terminal.py)
    options.extend(['-o', 'ExitOnForwardFailure=yes'])
    
    # Default to accepting new host keys non-interactively on fresh installs (same as terminal.py)
    try:
        if (not strict_host) and auto_add_host_keys:
            options.extend(['-o', 'StrictHostKeyChecking=accept-new'])
    except Exception:
        pass
    
    # Only add verbose flag if explicitly enabled in config (same as terminal.py)
    # Note: ssh-copy-id doesn't support -v flags, only -x for debug
    try:
        verbosity = int(ssh_cfg.get('verbosity', 0))
        debug_enabled = bool(ssh_cfg.get('debug_enabled', False))
        v = max(0, min(3, verbosity))
        if not for_ssh_copy_id:
            for _ in range(v):
                options.append('-v')
        # Map verbosity to LogLevel to ensure messages are not suppressed by defaults
        if v == 1:
            options.extend(['-o', 'LogLevel=VERBOSE'])
        elif v == 2:
            options.extend(['-o', 'LogLevel=DEBUG2'])
        elif v >= 3:
            options.extend(['-o', 'LogLevel=DEBUG3'])
        elif debug_enabled:
            options.extend(['-o', 'LogLevel=DEBUG'])
    except Exception as e:
        logger.warning(f"Could not check SSH verbosity/debug settings: {e}")
    
    # Add key file/options only for key-based auth (same as terminal.py)
    # Note: ssh-copy-id already specifies the key with -i, so we don't add it again
    if not password_auth_selected:
        if hasattr(connection, 'keyfile') and connection.keyfile and \
           os.path.isfile(connection.keyfile) and \
           not connection.keyfile.startswith('Select key file'):
            
            if not for_ssh_copy_id:
                options.extend(['-i', connection.keyfile])
            # Enforce using only the specified key when key_select_mode == 1
            try:
                if int(getattr(connection, 'key_select_mode', 0) or 0) == 1:
                    options.extend(['-o', 'IdentitiesOnly=yes'])
            except Exception:
                pass
    else:
        # Prefer password/interactive methods when user chose password auth (same as terminal.py)
        # But don't disable pubkey auth for ssh-copy-id since we're installing a key
        options.extend(['-o', 'PreferredAuthentications=password,keyboard-interactive'])
        if not for_ssh_copy_id:
            options.extend(['-o', 'PubkeyAuthentication=no'])
    
    # Add X11 forwarding if enabled (same as terminal.py) - not supported by ssh-copy-id
    if hasattr(connection, 'x11_forwarding') and connection.x11_forwarding and not for_ssh_copy_id:
        options.append('-X')
    
    return options

def custom_ssh_copy_id(connection, public_key_path: str, config=None, connection_manager=None) -> Tuple[bool, str]:
    """
    Custom ssh-copy-id implementation that tries all keys and then password, like default SSH behavior.
    
    Args:
        connection: The connection object
        public_key_path: Path to the public key to copy
        config: Optional config object
        connection_manager: Optional connection manager for password retrieval
        
    Returns:
        Tuple of (success: bool, message: str)
    """
    logger.info(f"Custom ssh-copy-id: Copying {public_key_path} to {connection.username}@{connection.host}")
    
    try:
        # Verify public key file exists
        if not os.path.exists(public_key_path):
            return False, f"Public key file not found: {public_key_path}"
        
        # Read the public key content
        with open(public_key_path, 'r') as f:
            public_key_content = f.read().strip()
        
        if not public_key_content:
            return False, f"Public key file is empty: {public_key_path}"
        
        # Build SSH options for the connection
        ssh_options = build_connection_ssh_options(connection, config, for_ssh_copy_id=True)
        
        # Add port if specified
        if hasattr(connection, 'port') and connection.port != 22:
            ssh_options.extend(['-p', str(connection.port)])
        
        # Build the target string
        target = f"{connection.username}@{connection.host}"
        
        # Step 1: Try to connect and check if the key is already installed
        logger.debug("Custom ssh-copy-id: Checking if key is already installed")
        
        # Build SSH command for checking
        check_cmd = ['ssh'] + ssh_options + [target, 'cat ~/.ssh/authorized_keys']
        
        try:
            result = subprocess.run(
                check_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                # Successfully read authorized_keys, check if our key is already there
                authorized_keys = result.stdout
                if public_key_content in authorized_keys:
                    return True, "Public key is already installed on the server"
        except subprocess.TimeoutExpired:
            logger.debug("Custom ssh-copy-id: Timeout checking existing keys, continuing with installation")
        except Exception as e:
            logger.debug(f"Custom ssh-copy-id: Could not check existing keys: {e}")
        
        # Step 2: Try to copy the key using SSH with multiple authentication methods
        logger.debug("Custom ssh-copy-id: Attempting to copy key with multiple auth methods")
        
        # Create a temporary script to copy the key
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as temp_script:
            script_content = f"""#!/bin/bash
set -e

# Create .ssh directory if it doesn't exist
mkdir -p ~/.ssh

# Set proper permissions
chmod 700 ~/.ssh

# Append the public key to authorized_keys
echo '{public_key_content}' >> ~/.ssh/authorized_keys

# Set proper permissions on authorized_keys
chmod 600 ~/.ssh/authorized_keys

echo "Public key successfully installed"
"""
            temp_script.write(script_content)
            temp_script_path = temp_script.name
        
        try:
            # Make the script executable
            os.chmod(temp_script_path, 0o755)
            
            # Build SSH command to execute the script
            # Use the same authentication strategy as default SSH: try all keys, then password
            ssh_cmd = ['ssh'] + ssh_options + [
                '-o', 'PreferredAuthentications=publickey,password,keyboard-interactive',
                '-o', 'PubkeyAuthentication=yes',
                '-o', 'PasswordAuthentication=yes',
                '-o', 'KbdInteractiveAuthentication=yes',
                '-o', 'NumberOfPasswordPrompts=1',
                target,
                f'bash < {temp_script_path}'
            ]
            
            logger.debug(f"Custom ssh-copy-id: Executing command: {' '.join(ssh_cmd)}")
            
            # Execute the command
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                logger.info("Custom ssh-copy-id: Successfully copied public key")
                return True, "Public key successfully installed on the server"
            else:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                logger.error(f"Custom ssh-copy-id: Failed to copy key: {error_msg}")
                return False, f"Failed to copy public key: {error_msg}"
                
        finally:
            # Clean up temporary script
            try:
                os.unlink(temp_script_path)
            except Exception:
                pass
                
    except Exception as e:
        logger.error(f"Custom ssh-copy-id: Unexpected error: {e}")
        return False, f"Unexpected error: {str(e)}"
