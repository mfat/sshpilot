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
class NativeAuth:
    """Resolved authentication for a native SSH-family connection.

    This is the single source of truth for how sshPilot supplies credentials
    to ssh/scp/ssh-copy-id: askpass + keyring autofill for key passphrases
    (with an optional agent bypass), or sshpass for a stored password. Callers
    apply ``env`` to the child process and add ``extra_opts`` to the command.
    """
    env: Dict[str, str]            # Environment to spawn the command with
    extra_opts: List[str]          # Extra command options to pass to ssh/scp
    use_sshpass: bool = False      # Feed ``password`` via sshpass
    password: Optional[str] = None # Stored password for sshpass
    use_askpass: bool = False      # SSH_ASKPASS is wired up
    password_mode: bool = False    # Password auth selected or a password is stored


def resolve_native_auth(
    connection: any,
    connection_manager: Optional[any] = None,
    app_config: Optional[any] = None,
) -> NativeAuth:
    """Resolve the authentication environment + options for a connection.

    Shared by the native terminal builder, SCP, and ssh-copy-id so every part
    of the app authenticates the same way. Behaviour:

    * Password auth selected: clear askpass and signal sshpass (with the stored
      password when one exists).
    * Askpass disabled in settings: let SSH prompt natively on the TTY.
    * Key-based auth — depends on what the user has saved (a saved secret is the
      opt-in for non-interactive auth):
        - saved key passphrase  -> askpass (REQUIRE=prefer) autofills it; key is
          primary, agent left intact. No sshpass.
        - else saved password   -> "combined auth": strip askpass and signal
          sshpass with the stored password, so SSH tries the key and falls back
          to the password (the terminal and SCP both wrap sshpass from this).
        - else (nothing saved)  -> no askpass, no sshpass; SSH prompts on the
          TTY for the passphrase and then the password, naturally.
    """
    auth_method = int(getattr(connection, 'auth_method', 0) or 0)
    password_auth_selected = (auth_method == 1)
    password_mode = password_auth_selected

    askpass_enabled = True
    try:
        if app_config is not None and hasattr(app_config, 'get_setting'):
            askpass_enabled = bool(app_config.get_setting('use-askpass', True))
    except Exception:
        askpass_enabled = True

    if password_mode:
        stored_password = _get_stored_password(connection, connection_manager)
        env = os.environ.copy()
        env.pop('SSH_ASKPASS', None)
        env['SSH_ASKPASS_REQUIRE'] = 'never'
        if stored_password:
            logger.debug("resolve_native_auth: stored password -> sshpass")
        return NativeAuth(
            env=env, extra_opts=[], use_sshpass=bool(stored_password),
            password=stored_password or None, use_askpass=False, password_mode=True,
        )

    if not askpass_enabled:
        env = os.environ.copy()
        env.pop('SSH_ASKPASS', None)
        env.pop('SSH_ASKPASS_REQUIRE', None)
        logger.debug("resolve_native_auth: askpass disabled by setting")
        return NativeAuth(env=env, extra_opts=[], use_askpass=False, password_mode=False)

    # Key-based auth. Decide based on what the user has saved for this host.
    # Probe is best-effort: on any error, fall back to askpass-on (never regress
    # the saved-passphrase autofill).
    has_stored_passphrase = True
    try:
        candidates = getattr(connection, 'resolved_identity_files', None)
        if not candidates and hasattr(connection, 'collect_identity_file_candidates'):
            candidates = connection.collect_identity_file_candidates()
        has_stored_passphrase = any(lookup_passphrase(p) for p in (candidates or []))
    except Exception:
        has_stored_passphrase = True

    if has_stored_passphrase:
        # Saved key passphrase -> askpass autofills it; key is primary.
        env = get_ssh_env_with_askpass(require="prefer")
        logger.debug("resolve_native_auth: key auth, askpass autofill (saved passphrase)")
        return NativeAuth(env=env, extra_opts=[], use_askpass=True, password_mode=False)

    stored_password = _get_stored_password(connection, connection_manager)
    if stored_password:
        # No saved passphrase but a saved password -> combined auth: try the key,
        # fall back to the password via sshpass. Strip askpass so it can't hijack
        # the password prompt.
        env = os.environ.copy()
        env.pop('SSH_ASKPASS', None)
        env['SSH_ASKPASS_REQUIRE'] = 'never'
        logger.debug("resolve_native_auth: key auth, no saved passphrase -> sshpass password fallback")
        return NativeAuth(
            env=env, extra_opts=[], use_sshpass=True, password=stored_password,
            use_askpass=False, password_mode=False,
        )

    # Nothing saved -> let SSH prompt naturally on the TTY (passphrase, then
    # password fallback), without our askpass intercepting.
    env = os.environ.copy()
    env.pop('SSH_ASKPASS', None)
    env.pop('SSH_ASKPASS_REQUIRE', None)
    logger.debug("resolve_native_auth: key auth, nothing saved -> native TTY prompts")
    return NativeAuth(env=env, extra_opts=[], use_askpass=False, password_mode=False)


def build_native_command(
    connection: any,
    app_config: Optional[any] = None,
    command_type: str = 'ssh',
    remote_command: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
) -> List[str]:
    """Build the plain native SSH-family command, with NO authentication applied.

    Produces ``<binary> -F <config> [ssh_overrides] [extra_args] host [remote_command]``
    and relies on ~/.ssh/config for per-host settings. Use this for callers that
    hand the command to an external process (e.g. the system terminal), which
    supplies its own TTY/agent and must not inherit sshPilot's in-app askpass or
    agent bypass. In-app connections instead use build_ssh_connection (native),
    which layers resolve_native_auth on top of this same shape.
    """
    binary = {'scp': 'scp', 'sftp': 'sftp', 'ssh-copy-id': 'ssh-copy-id'}.get(command_type, 'ssh')
    cmd = [binary]

    config_override = None
    if hasattr(connection, '_resolve_config_override_path'):
        try:
            config_override = connection._resolve_config_override_path()
        except Exception:
            config_override = None
    if config_override:
        cmd.extend(['-F', config_override])

    if app_config:
        try:
            app_ssh_config = app_config.get_ssh_config() if hasattr(app_config, 'get_ssh_config') else {}
        except Exception:
            app_ssh_config = {}
        overrides = app_ssh_config.get('ssh_overrides', [])
        if isinstance(overrides, (list, tuple)):
            for entry in overrides:
                if entry:
                    cmd.append(str(entry))

    if extra_args:
        cmd.extend(extra_args)

    target = (
        getattr(connection, 'nickname', '')
        or getattr(connection, 'host', '')
        or getattr(connection, 'hostname', '')
    )
    if hasattr(connection, 'resolve_host_identifier'):
        try:
            resolved = connection.resolve_host_identifier()
            if resolved:
                target = resolved
        except Exception:
            pass
    cmd.append(target)

    if remote_command:
        cmd.append(remote_command)

    return cmd


@dataclass
class ConnectionContext:
    """Context for building SSH connections."""
    connection: any  # Connection object
    connection_manager: Optional[any] = None  # ConnectionManager instance
    config: Optional[any] = None  # Config instance
    command_type: str = 'ssh'  # 'ssh', 'scp', 'sftp', 'ssh-copy-id'
    extra_args: List[str] = None  # Extra arguments to append
    # Advanced features
    port_forwarding_rules: Optional[List[Dict]] = None  # Port forwarding rules
    remote_command: Optional[str] = None  # Remote command to execute
    local_command: Optional[str] = None  # Local command to execute
    extra_ssh_config: Optional[str] = None  # Extra SSH config options (from advanced tab)
    known_hosts_path: Optional[str] = None  # Custom known hosts file
    native_mode: bool = False  # Use native SSH mode (minimal command)


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
    """Get stored password for connection.
    
    Checks in this order:
    1. Connection object's password attribute (in-memory, from dialog)
    2. Connection manager's stored password (libsecret/keyring/keychain)
    """
    # First check connection object for in-memory password
    try:
        in_memory_password = getattr(connection, 'password', None)
        if in_memory_password:
            return in_memory_password
    except Exception:
        pass
    
    # Then check connection manager storage
    if not connection_manager:
        return None
    
    try:
        host = getattr(connection, 'hostname', '') or getattr(connection, 'host', '')
        username = getattr(connection, 'username', '')
        if host and username:
            stored = connection_manager.get_password(host, username)
            if stored:
                return stored
    except Exception:
        pass
    return None


def _get_stored_passphrase(
    key_path: str,
    connection_manager: Optional[any] = None
) -> Optional[str]:
    """Get stored passphrase for key."""
    if not key_path:
        return None
    
    # Try connection_manager first if available
    if connection_manager:
        try:
            if hasattr(connection_manager, 'get_key_passphrase'):
                result = connection_manager.get_key_passphrase(key_path)
                if result:
                    return result
        except Exception:
            pass
    
    # Fallback to direct lookup (works even without connection_manager)
    try:
        result = lookup_passphrase(key_path)
        if result:
            return result
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

    if bool(app_ssh_config.get('batch_mode', False)):
        cmd.extend(['-o', 'BatchMode=yes'])

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

    forward_agent = _get_ssh_config_value(config, 'forwardagent', '').lower()
    if forward_agent in ('yes', 'true', '1'):
        cmd.append('-A')
        cmd.extend(['-o', 'ForwardAgent=yes'])
    
    # Apply other SSH config options that should be passed through
    # (SSH config is primary source, so we trust it)
    # Note: We don't override PreferredAuthentications here - let SSH config handle it
    
    return cmd


def _maybe_append_default_keepalive(cmd, overrides, app_ssh_config):
    """Append default ServerAlive* options to *cmd* unless keepalive is already set.

    Keepalive is left untouched when the user already configured it — either via
    Preferences (the explicit value lands in ``ssh_overrides`` and/or the
    ``keepalive_interval`` app key) — so user values always win. The global
    opt-out (``ssh.apply_default_keepalive`` = False) disables injection
    entirely; that is also the escape hatch for anyone who set their own
    ServerAliveInterval only in ~/.ssh/config (we intentionally don't run an
    extra ``ssh -G`` here — that would add a second probe to every connect — and
    overriding with a default keepalive is harmless, just a probe cadence).
    """
    try:
        if not bool(app_ssh_config.get('apply_default_keepalive', True)):
            return

        # Already set by the user via Preferences. The explicit value is composed
        # into ssh_overrides; the raw key is also honored directly so we never
        # double-apply or override an explicit choice.
        try:
            explicit = app_ssh_config.get('keepalive_interval')
            if explicit not in (None, '') and int(explicit) > 0:
                return
        except (TypeError, ValueError):
            pass
        if isinstance(overrides, (list, tuple)):
            for entry in overrides:
                if entry and 'ServerAliveInterval' in str(entry):
                    return

        interval = int(app_ssh_config.get('default_keepalive_interval', 15) or 15)
        count = int(app_ssh_config.get('default_keepalive_count', 3) or 3)
        if interval > 0:
            cmd.extend(['-o', f'ServerAliveInterval={interval}'])
            if count > 0:
                cmd.extend(['-o', f'ServerAliveCountMax={count}'])
    except Exception as exc:  # never let keepalive injection break connecting
        logger.debug("Default keepalive injection skipped: %s", exc)


def build_ssh_connection(
    ctx: ConnectionContext
) -> SSHConnectionCommand:
    """
    Build a complete SSH connection command following the `ssh Host` pattern.
    
    This is the unified entry point for all SSH operations:
    - Terminal connections
    - SCP transfers
    - SFTP connections
    - ssh-copy-id operations
    
    Features:
    - Gets effective config from the right SSH config file (supports isolated mode)
    - Builds SSH command following `ssh Host` pattern (uses host identifier from SSH config)
    - Supports password and key-based authentication
    - Retrieves passwords from libsecret/keyring/keychain
    - Retrieves passphrases from libsecret/keyring/keychain
    - Automatic password authentication with sshpass
    - Automatic key-based authentication with askpass for passphrase-protected keys
    - Supports appending extra SSH options
    - Shows graphical prompts for passwords and passphrases via askpass
    
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
    
    logger.debug(f"build_ssh_connection called: native_mode={ctx.native_mode}, connection_manager={'present' if connection_manager else 'None'}")
    
    # 1. Get effective SSH config from the right SSH config file
    # Try to get host identifier from connection
    host_label = getattr(connection, 'nickname', '') or \
                 getattr(connection, 'host', '') or \
                 getattr(connection, 'hostname', '')
    
    # If connection has resolve_host_identifier method, use it
    if not host_label and hasattr(connection, 'resolve_host_identifier'):
        try:
            host_label = connection.resolve_host_identifier()
        except Exception:
            pass
    
    if not host_label:
        raise ValueError("Connection must have a host identifier (nickname, host, or hostname)")
    
    # Check for config override (isolated mode, etc.)
    config_override = None
    if hasattr(connection, '_resolve_config_override_path'):
        try:
            config_override = connection._resolve_config_override_path()
        except Exception:
            pass
    
    # Handle native mode - SSH config is the source of truth for per-host
    # settings (IdentityFile, port forwarding, X11, RemoteCommand, ...), so the
    # command stays minimal: `ssh -F <config> host`. This builder owns only the
    # runtime concerns that do NOT live in ~/.ssh/config:
    #   * app-level ssh_overrides (verbosity/timeouts/keepalive written by the app)
    #   * batch mode preference
    #   * the authentication environment: askpass + keyring autofill for key
    #     passphrases (with an optional agent bypass), or sshpass for a stored
    #     password.
    # This makes build_ssh_connection(native) a complete, ready-to-spawn result
    # that every caller (terminal, scp, ssh-copy-id) can use without rebuilding
    # the command or re-deriving the auth env.
    if ctx.native_mode:
        base_cmd = ['ssh']
        config_override = None
        if hasattr(connection, '_resolve_config_override_path'):
            try:
                config_override = connection._resolve_config_override_path()
            except Exception:
                config_override = None
        if config_override:
            base_cmd.extend(['-F', config_override])

        # App-level SSH settings. ssh_overrides carries the user's global SSH
        # options (verbosity, ConnectTimeout, ServerAlive*, etc.); append verbatim.
        app_ssh_config = {}
        if app_config:
            try:
                app_ssh_config = app_config.get_ssh_config() if hasattr(app_config, 'get_ssh_config') else {}
            except Exception:
                app_ssh_config = {}
            overrides = app_ssh_config.get('ssh_overrides', [])
            if isinstance(overrides, (list, tuple)):
                for entry in overrides:
                    if entry:
                        base_cmd.append(str(entry))

            # Default keepalive: when neither the user's app settings nor their
            # ~/.ssh/config define ServerAliveInterval for this host, inject a
            # sane default so a dead link (laptop sleep, VPN drop, cable pull)
            # is detected instead of the connection lingering "green" forever.
            # Honors CLAUDE.md: this is a runtime option that doesn't live in
            # ~/.ssh/config, applied via -o like the rest of ssh_overrides, and
            # any explicit user/per-host value wins.
            _maybe_append_default_keepalive(base_cmd, overrides, app_ssh_config)

        # Authentication is resolved by the single shared helper so the terminal,
        # SCP, and ssh-copy-id all authenticate identically.
        auth = resolve_native_auth(connection, connection_manager, app_config)

        # BatchMode preference (never for password auth, which needs to prompt).
        if bool(app_ssh_config.get('batch_mode', False)) and not auth.password_mode:
            if 'BatchMode=yes' not in base_cmd:
                base_cmd.extend(['-o', 'BatchMode=yes'])

        base_cmd.extend(auth.extra_opts)
        # Extra CLI flags (rare; before the host).
        if extra_args:
            base_cmd.extend(extra_args)
        env = auth.env

        native_target = host_label
        if hasattr(connection, 'resolve_host_identifier'):
            try:
                resolved = connection.resolve_host_identifier()
                if resolved:
                    native_target = resolved
            except Exception:
                pass
        base_cmd.append(native_target)

        # A raw one-shot remote command (e.g. reading the remote working dir for
        # follow-mode) is appended after the host. Interactive saved connections
        # don't use this — their remote command lives in ~/.ssh/config.
        if ctx.remote_command:
            base_cmd.append(ctx.remote_command)

        return SSHConnectionCommand(
            command=base_cmd,
            env=env,
            use_sshpass=auth.use_sshpass,
            password=auth.password,
            use_askpass=auth.use_askpass,
        )

    # Native mode is the only supported connection mode. Any legacy caller
    # that left native_mode unset is coerced to native rather than taking a
    # (now removed) non-native path.
    ctx.native_mode = True
    return build_ssh_connection(ctx)
