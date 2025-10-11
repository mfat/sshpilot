"""
SSH utilities for building consistent SSH options across the application
"""

import os
import logging
from typing import List, Dict

from .platform_utils import is_flatpak

logger = logging.getLogger(__name__)


def _is_batchmode_yes_token(token: str) -> bool:
    """Return True when the provided SSH option token enables BatchMode."""

    if not isinstance(token, str):
        return False

    normalized = token.strip().lower()
    if not normalized:
        return False

    if normalized in {"batchmode=yes", "batchmode yes"}:
        return True

    if normalized == "batchmode":
        # Caller should inspect the following token for the value, handled separately
        return False

    return False


def remove_batchmode_yes_options(argv: List[str]) -> List[str]:
    """Return a copy of *argv* with any ``BatchMode=yes`` directives removed."""

    cleaned: List[str] = []
    idx = 0

    while idx < len(argv):
        arg = argv[idx]

        if arg == "-o" and idx + 1 < len(argv):
            option = argv[idx + 1]
            option_norm = option.strip().lower() if isinstance(option, str) else ""

            if _is_batchmode_yes_token(option):
                idx += 2
                continue

            if option_norm == "batchmode" and idx + 2 < len(argv):
                value = argv[idx + 2]
                if isinstance(value, str) and value.strip().lower() == "yes":
                    idx += 3
                    continue

            cleaned.extend(["-o", option])
            idx += 2
            continue

        if isinstance(arg, str):
            stripped = arg.strip().lower()

            if arg.startswith("-o") and _is_batchmode_yes_token(arg[2:]):
                idx += 1
                continue

            if stripped == "batchmode" and idx + 1 < len(argv):
                next_value = argv[idx + 1]
                if isinstance(next_value, str) and next_value.strip().lower() == "yes":
                    idx += 2
                    continue

            if stripped == "yes" and cleaned:
                last_token = cleaned[-1]
                if isinstance(last_token, str) and last_token.strip().lower() == "batchmode":
                    cleaned.pop()
                    idx += 1
                    continue

            if _is_batchmode_yes_token(arg):
                idx += 1
                continue

        cleaned.append(arg)
        idx += 1

    return cleaned


def ensure_writable_ssh_home(env: Dict[str, str]) -> None:
    """Ensure ssh-copy-id has a writable HOME when running in Flatpak."""
    if is_flatpak():
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        alt_home = os.path.join(runtime_dir, "sshcopyid-home")
        os.makedirs(os.path.join(alt_home, ".ssh"), exist_ok=True)
        env["HOME"] = alt_home
        logger.debug(f"Using temporary HOME for ssh-copy-id: {alt_home}")

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

    def _coerce_int(value, default=None):
        try:
            coerced = int(str(value))
            if coerced <= 0:
                return default
            return coerced
        except (TypeError, ValueError):
            return default

    connect_timeout = _coerce_int(ssh_cfg.get('connection_timeout'), None)
    connection_attempts = _coerce_int(ssh_cfg.get('connection_attempts'), None)
    keepalive_interval = _coerce_int(ssh_cfg.get('keepalive_interval'), None)
    keepalive_count = _coerce_int(ssh_cfg.get('keepalive_count_max'), None)
    strict_host = str(ssh_cfg.get('strict_host_key_checking', '') or '').strip()
    auto_add_host_keys = bool(ssh_cfg.get('auto_add_host_keys', True))
    batch_mode = bool(ssh_cfg.get('batch_mode', False))
    compression = bool(ssh_cfg.get('compression', False))

    # Determine auth method from connection and whether a password is available
    password_auth_selected = False
    has_saved_password = False
    try:
        # In our UI: 0 = key-based, 1 = password
        auth_method = getattr(connection, 'auth_method', 0)
        password_auth_selected = (auth_method == 1)
        has_saved_password = bool(getattr(connection, 'password', None))
    except Exception:
        password_auth_selected = False
        has_saved_password = False
    using_password = password_auth_selected or (not password_auth_selected and has_saved_password)
    identity_agent_disabled = bool(getattr(connection, 'identity_agent_disabled', False))

    # Apply advanced args according to stored preferences (same as terminal.py)
    # Only enable BatchMode when NOT doing password auth (BatchMode disables prompts)
    if batch_mode and not using_password and not identity_agent_disabled:
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
        
        # Only add specific key when a dedicated key mode is selected
        if key_select_mode in (1, 2) and hasattr(connection, 'keyfile') and connection.keyfile and \
           os.path.isfile(connection.keyfile) and \
           not connection.keyfile.startswith('Select key file'):

            if not for_ssh_copy_id:
                options.extend(['-i', connection.keyfile])
            if key_select_mode == 1:
                options.extend(['-o', 'IdentitiesOnly=yes'])
            
            # Add certificate if specified
            if hasattr(connection, 'certificate') and connection.certificate and \
               os.path.isfile(connection.certificate):
                options.extend(['-o', f'CertificateFile={connection.certificate}'])

            # If a password is available, allow all standard authentication methods
            if has_saved_password:
                options.extend([
                    '-o',
                    'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password'
                ])
        else:
            # If no specific key or certificate, still append combined auth if password exists
            if has_saved_password:
                options.extend([
                    '-o',
                    'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password'
                ])
    else:
        # Force password authentication when user chose password auth (same as terminal.py)
        # But don't disable pubkey auth for ssh-copy-id since we're installing a key
        options.extend(['-o', 'PreferredAuthentications=password'])
        if not for_ssh_copy_id and getattr(connection, 'pubkey_auth_no', False):
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
    
    if identity_agent_disabled:
        options = remove_batchmode_yes_options(options)

    return options
