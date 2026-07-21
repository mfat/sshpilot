"""
SSH utilities for building consistent SSH options across the application
"""

import os
import logging
from typing import Dict, List, Optional, Tuple

from .platform_utils import is_flatpak

logger = logging.getLogger(__name__)

# Markers shared by FM / SCP when classifying a failed ssh/scp run as auth.
# "permission denied" alone is NOT a marker: scp prints it for remote *file*
# permission errors ("scp: /path: Permission denied"). SSH auth failures use
# the parenthesized method list or the retry phrasing.
_SSH_AUTH_FAILURE_MARKERS = (
    'permission denied (',
    'permission denied, please try again',
    'authentication failed',
    'too many authentication failures',
)

_COMBINED_AUTH = (
    'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,'
    'keyboard-interactive,password'
)
_PASSWORD_AUTH = 'PreferredAuthentications=keyboard-interactive,password'


def is_ssh_auth_failure_text(text: str) -> bool:
    """True when *text* looks like an SSH authentication failure."""
    lowered = (text or '').lower()
    return any(marker in lowered for marker in _SSH_AUTH_FAILURE_MARKERS)


def clean_ssh_stderr(text: str) -> str:
    """Drop ``ssh -v`` ``debug`` chatter, leaving the human-meaningful lines.

    With verbose logging on, ssh floods stderr with ``debugN:`` lines; showing
    that raw log in the UI is noise. Returns the stripped, joined remainder.
    """
    return "\n".join(
        line.strip()
        for line in (text or "").splitlines()
        if line.strip() and not line.lstrip().startswith("debug")
    ).strip()


def ensure_writable_ssh_home(env: Dict[str, str]) -> None:
    """Ensure ssh-copy-id has a writable HOME when running in Flatpak."""
    if is_flatpak():
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        alt_home = os.path.join(runtime_dir, "sshcopyid-home")
        os.makedirs(os.path.join(alt_home, ".ssh"), exist_ok=True)
        env["HOME"] = alt_home
        logger.debug(f"Using temporary HOME for ssh-copy-id: {alt_home}")


def _coerce_positive_int(value, default=None):
    try:
        coerced = int(str(value))
        if coerced <= 0:
            return default
        return coerced
    except (TypeError, ValueError):
        return default


def _load_ssh_cfg(config) -> dict:
    try:
        if config is None:
            from .config import Config
            config = Config()
        if hasattr(config, 'get_ssh_config'):
            return config.get_ssh_config() or {}
    except Exception:
        pass
    return {}


def _auth_flags(connection) -> Tuple[bool, bool, bool]:
    """Return (password_auth_selected, has_saved_password, using_password)."""
    try:
        # In our UI: 0 = key-based, 1 = password
        password_auth_selected = getattr(connection, 'auth_method', 0) == 1
        has_saved_password = bool(getattr(connection, 'password', None))
    except Exception:
        return False, False, False
    using_password = password_auth_selected or has_saved_password
    return password_auth_selected, has_saved_password, using_password


def _append_cfg_int_options(options: List[str], ssh_cfg: dict, for_ssh_copy_id: bool) -> None:
    """Append timeout / keepalive / host-key options from app SSH preferences."""
    connect_timeout = _coerce_positive_int(ssh_cfg.get('connection_timeout'))
    connection_attempts = _coerce_positive_int(ssh_cfg.get('connection_attempts'))
    keepalive_interval = _coerce_positive_int(ssh_cfg.get('keepalive_interval'))
    keepalive_count = _coerce_positive_int(ssh_cfg.get('keepalive_count_max'))
    strict_host = str(ssh_cfg.get('strict_host_key_checking', '') or '').strip()
    auto_add_host_keys = bool(ssh_cfg.get('auto_add_host_keys', True))
    compression = bool(ssh_cfg.get('compression', False))

    if connect_timeout is not None:
        options.extend(['-o', f'ConnectTimeout={connect_timeout}'])
    if connection_attempts is not None:
        options.extend(['-o', f'ConnectionAttempts={connection_attempts}'])
    if for_ssh_copy_id:
        options.extend(['-o', 'NumberOfPasswordPrompts=1'])
    if keepalive_interval is not None:
        options.extend(['-o', f'ServerAliveInterval={keepalive_interval}'])
    if keepalive_count is not None:
        options.extend(['-o', f'ServerAliveCountMax={keepalive_count}'])
    if strict_host:
        options.extend(['-o', f'StrictHostKeyChecking={strict_host}'])
    elif auto_add_host_keys:
        # Default to accepting new host keys non-interactively on fresh installs
        options.extend(['-o', 'StrictHostKeyChecking=accept-new'])
    if compression and not for_ssh_copy_id:
        options.append('-C')


def _append_verbosity_options(options: List[str], ssh_cfg: dict, for_ssh_copy_id: bool) -> None:
    """Map app verbosity/debug prefs to ``-v`` / ``LogLevel`` (ssh only)."""
    try:
        verbosity = int(ssh_cfg.get('verbosity', 0))
        debug_enabled = bool(ssh_cfg.get('debug_enabled', False))
        v = max(0, min(3, verbosity))
        if not for_ssh_copy_id:
            options.extend(['-v'] * v)
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


def _key_select_mode(connection) -> int:
    try:
        return int(getattr(connection, 'key_select_mode', 0) or 0)
    except Exception:
        return 0


def _usable_keyfile(connection) -> Optional[str]:
    keyfile = getattr(connection, 'keyfile', None)
    if not keyfile or not os.path.isfile(keyfile):
        return None
    if keyfile.startswith('Select key file'):
        return None
    return keyfile


def _append_key_auth_options(
    options: List[str],
    connection,
    *,
    has_saved_password: bool,
    for_ssh_copy_id: bool,
) -> None:
    """Identity / certificate options for key-based auth."""
    key_select_mode = _key_select_mode(connection)
    keyfile = _usable_keyfile(connection) if key_select_mode in (1, 2) else None

    if keyfile:
        # ssh-copy-id already specifies the key with -i
        if not for_ssh_copy_id:
            options.extend(['-i', keyfile])
        if key_select_mode == 1:
            options.extend(['-o', 'IdentitiesOnly=yes'])

        certificate = getattr(connection, 'certificate', None)
        if certificate and os.path.isfile(certificate):
            options.extend(['-o', f'CertificateFile={certificate}'])

    if has_saved_password:
        options.extend(['-o', _COMBINED_AUTH])


def _append_password_auth_options(
    options: List[str],
    connection,
    *,
    for_ssh_copy_id: bool,
) -> None:
    options.extend(['-o', _PASSWORD_AUTH])
    # Don't disable pubkey auth for ssh-copy-id — we're installing a key
    if not for_ssh_copy_id and getattr(connection, 'pubkey_auth_no', False):
        options.extend(['-o', 'PubkeyAuthentication=no'])


def _append_extra_ssh_config(options: List[str], connection) -> None:
    extra_ssh_config = getattr(connection, 'extra_ssh_config', '').strip()
    if not extra_ssh_config:
        return
    logger.debug(f"Adding extra SSH config options: {extra_ssh_config}")
    for line in extra_ssh_config.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(' ', 1)
        option = parts[0]
        value = parts[1] if len(parts) == 2 else 'yes'
        options.extend(['-o', f"{option}={value}"])
        logger.debug(f"Added SSH option: {option}={value}")


def build_connection_ssh_options(connection, config=None, for_ssh_copy_id=False):
    """Build SSH options that match the exact connection settings used for SSH.
    
    This function replicates the SSH option building logic from terminal.py
    to ensure ssh-copy-id and scp use the same settings as SSH connections.
    
    Args:
        connection: The connection object
        config: Optional config object
        for_ssh_copy_id: If True, filter out options not supported by ssh-copy-id
    """
    options: List[str] = []
    ssh_cfg = _load_ssh_cfg(config)
    password_auth_selected, has_saved_password, using_password = _auth_flags(connection)

    # Only enable BatchMode when NOT doing password auth (BatchMode disables prompts)
    if bool(ssh_cfg.get('batch_mode', False)) and not using_password:
        options.extend(['-o', 'BatchMode=yes'])

    _append_cfg_int_options(options, ssh_cfg, for_ssh_copy_id)
    # Ensure SSH exits immediately on failure rather than waiting in background
    options.extend(['-o', 'ExitOnForwardFailure=yes'])
    _append_verbosity_options(options, ssh_cfg, for_ssh_copy_id)

    if password_auth_selected:
        _append_password_auth_options(options, connection, for_ssh_copy_id=for_ssh_copy_id)
    else:
        _append_key_auth_options(
            options, connection,
            has_saved_password=has_saved_password,
            for_ssh_copy_id=for_ssh_copy_id,
        )

    _append_extra_ssh_config(options, connection)

    if getattr(connection, 'x11_forwarding', False) and not for_ssh_copy_id:
        options.append('-X')

    return options
