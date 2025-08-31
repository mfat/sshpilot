"""
SSH utilities for building consistent SSH options across the application
"""

import os
import logging
from typing import List

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

    # Default to accepting new host keys non-interactively on fresh installs (same as terminal.py)
    try:
        if (not strict_host) and auto_add_host_keys:
            options.extend(['-o', 'StrictHostKeyChecking=accept-new'])
    except Exception:
        pass

    # Ensure SSH exits immediately on failure rather than waiting in background (same as terminal.py)
    options.extend(['-o', 'ExitOnForwardFailure=yes'])
    
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
        # Get key selection mode
        key_select_mode = 0
        try:
            key_select_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
        except Exception:
            pass
        
        # Only add specific key if key_select_mode == 1 (specific key)
        if key_select_mode == 1 and hasattr(connection, 'keyfile') and connection.keyfile and \
           os.path.isfile(connection.keyfile) and \
           not connection.keyfile.startswith('Select key file'):
            
            if not for_ssh_copy_id:
                options.extend(['-i', connection.keyfile])
            options.extend(['-o', 'IdentitiesOnly=yes'])
            
            # Add certificate if specified
            if hasattr(connection, 'certificate') and connection.certificate and \
               os.path.isfile(connection.certificate):
                options.extend(['-o', f'CertificateFile={connection.certificate}'])
    else:
        # Force password authentication when user chose password auth (same as terminal.py)
        # But don't disable pubkey auth for ssh-copy-id since we're installing a key
        options.extend(['-o', 'PreferredAuthentications=password'])
        if not for_ssh_copy_id:
            options.extend(['-o', 'PubkeyAuthentication=no'])
    
    # Add extra SSH config options from advanced tab (same as terminal.py)
    extra_ssh_config = getattr(connection, 'extra_ssh_config', '').strip()
    if extra_ssh_config:
        logger.debug(f"Adding extra SSH config options: {extra_ssh_config}")
        # Parse and add each extra SSH config option
        for line in extra_ssh_config.split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):  # Skip empty lines and comments
                # Split on first space to separate option and value
                parts = line.split(' ', 1)
                if len(parts) == 2:
                    option, value = parts
                    options.extend(['-o', f"{option}={value}"])
                    logger.debug(f"Added SSH option: {option}={value}")
                elif len(parts) == 1:
                    # Option without value (e.g., "Compression yes" becomes "Compression=yes")
                    option = parts[0]
                    options.extend(['-o', f"{option}=yes"])
                    logger.debug(f"Added SSH option: {option}=yes")
    
    # Add X11 forwarding if enabled (same as terminal.py) - not supported by ssh-copy-id
    if hasattr(connection, 'x11_forwarding') and connection.x11_forwarding and not for_ssh_copy_id:
        options.append('-X')
    
    return options
