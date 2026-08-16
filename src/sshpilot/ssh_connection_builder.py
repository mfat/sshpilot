# ssh_connection_builder.py
"""
Unified SSH connection builder that uses SSH config as primary source of truth.

This module provides a single, consistent way to build SSH commands for all
components (terminal, SCP, SFTP, ssh-copy-id) that matches default SSH behavior.

GTK-free argv composition for explicit launch descriptions lives in
``sshpilot.core.ssh.build_ssh_process_spec``. This module remains the runtime
adapter that resolves askpass/secret backends for live Connection objects.
"""
import os
import logging
from typing import Dict, List, Optional, Union
from dataclasses import dataclass

from .askpass_utils import (
    get_ssh_env_with_askpass,
    ensure_key_in_agent,
    lookup_passphrase,
)
from .core.ssh import ProcessSpec, build_ssh_process_spec  # noqa: F401 — re-export


def _askpass_env_for_connection(
    connection: any,
    *,
    require: str = "prefer",
    session_password: Optional[str] = None,
    session_passphrase: Optional[str] = None,
    secret_lookup_authoritative: bool = False,
) -> Dict[str, str]:
    """Build askpass env, advertising login-password host/user when known."""
    try:
        from .credential_model import canonical_password_host, password_host_candidates
        user = (getattr(connection, 'username', None) or '').strip()
        hosts = password_host_candidates(connection) or []
        canonical = canonical_password_host(connection)
        if canonical and canonical not in hosts:
            hosts = [canonical] + list(hosts)
    except Exception:
        user = (getattr(connection, 'username', None) or '').strip()
        hosts = [
            h for h in (
                getattr(connection, 'hostname', None),
                getattr(connection, 'host', None),
                getattr(connection, 'nickname', None),
            ) if h
        ]
    # The daemon interaction broker owns secrets for production launches. Do
    # not stage a daemon-resolved secret into a file or child environment;
    # prepare_launch replaces this compatibility environment with its private
    # broker socket before starting OpenSSH.
    if secret_lookup_authoritative:
        session_password = None
        session_passphrase = None
    # Direct core helpers may still stage a one-shot value for their isolated
    # tests/legacy utility contract. Production daemon callers set the flag
    # above and never take this branch.
    if session_password and user:
        try:
            from .askpass_utils import lookup_ssh_password
            if any(lookup_ssh_password(h, user) == session_password
                   for h in hosts):
                session_password = None
        except Exception:
            pass
    return get_ssh_env_with_askpass(
        require,
        password_user=user or None,
        password_hosts=hosts or None,
        session_password=session_password,
        session_passphrase=session_passphrase,
    )


def _apply_askpass_env(
    prepared_env: Optional[Dict[str, str]],
    connection,
    *,
    require: str,
    session_password: Optional[str] = None,
    base_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Merge *prepared_env* and ensure ``SSH_ASKPASS`` with the given *require*."""
    prepared = dict(prepared_env or {})
    env = {**(base_env if base_env is not None else os.environ), **prepared}
    for key in ('SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE', 'SSH_AUTH_SOCK'):
        if key not in prepared:
            env.pop(key, None)
    password = session_password
    if password is None and connection is not None:
        password = getattr(connection, 'password', None) or None
    if not env.get('SSH_ASKPASS'):
        env.update(
            _askpass_env_for_connection(
                connection, require=require, session_password=password,
            )
        )
    elif env.get('SSH_ASKPASS_REQUIRE') != require:
        env['SSH_ASKPASS_REQUIRE'] = require
    return env


def apply_headless_askpass_env(
    prepared_env: Optional[Dict[str, str]],
    connection,
    *,
    session_password: Optional[str] = None,
    base_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Process env for an SSH child with no user-visible TTY.

    Merges *base_env* (default ``os.environ``) with the auth resolver's
    *prepared_env*, honors askpass/agent deletions, then forces
    ``SSH_ASKPASS`` + ``REQUIRE=prefer`` so password / passphrase / PIN /
    OTP / FIDO presence (``SSH_ASKPASS_PROMPT=none``) use graphical askpass
    instead of hanging on a missing TTY.

    Use for every pipe/capture/``stdin=DEVNULL`` SSH spawn. Do **not** use for
    VTE/system-terminal spawns (those have a real TTY) — except ssh-copy-id,
    which uses :func:`apply_forced_askpass_env`.
    """
    return _apply_askpass_env(
        prepared_env, connection, require='prefer',
        session_password=session_password, base_env=base_env,
    )


def apply_forced_askpass_env(
    prepared_env: Optional[Dict[str, str]],
    connection,
    *,
    session_password: Optional[str] = None,
    base_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Like :func:`apply_headless_askpass_env` but ``SSH_ASKPASS_REQUIRE=force``.

    OpenSSH will not fall back to the TTY when askpass declines — use for
    ssh-copy-id so passphrase/password/MFA prompts stay on the graphical
    askpass dialog even though the command runs inside a VTE.
    """
    return _apply_askpass_env(
        prepared_env, connection, require='force',
        session_password=session_password, base_env=base_env,
    )


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
    to ssh/scp/ssh-copy-id: **askpass** autofills both key passphrases and
    stored login passwords (``SSH_ASKPASS_REQUIRE=prefer`` so MFA/OTP prompts
    declined by the helper fall back to the TTY). ``use_sshpass`` is always
    False. ``password`` is retained for UI messaging / askpass session staging
    only — delivery goes through askpass env. Callers apply ``env`` to the
    child process and add ``extra_opts`` to the command.
    """
    env: Dict[str, str]            # Environment to spawn the command with
    extra_opts: List[str]          # Extra command options to pass to ssh/scp
    use_sshpass: bool = False      # Deprecated: always False (askpass replaced sshpass)
    password: Optional[str] = None # Stored password (askpass context / UI messaging)
    use_askpass: bool = False      # SSH_ASKPASS is wired up
    password_mode: bool = False    # Password auth method selected (auth_method == 1)


def resolve_native_auth(
    connection: any,
    connection_manager: Optional[any] = None,
    app_config: Optional[any] = None,
    *,
    interaction_policy: str = "normal",
) -> NativeAuth:
    """Resolve the authentication environment + options for a connection.

    Shared by the native terminal builder, SCP, and ssh-copy-id so every part
    of the app authenticates the same way. Behaviour:

    * **Askpass** (when enabled) autofills **key passphrases and login
      passwords** from the secret backend. REQUIRE=prefer so interactive MFA
      (OTP/PIN) is declined by the helper and answered on the real TTY.
    * Password method + stored password → askpass with password host/user
      context (and a one-shot session file for in-memory secrets).
    * Key-based + saved passphrase → askpass passphrase autofill; if a
      password is also saved and the key loads into the agent, askpass also
      carries password context (combined auth).
    * Key-based + saved password only → askpass password context (key tried
      first by ssh, then password prompt autofilled).
    * Askpass disabled / nothing saved → no askpass; SSH prompts on the TTY.
    * ``use_sshpass`` is never set (sshpass removed from the native path).
    """
    if interaction_policy not in {"normal", "none", "broker"}:
        raise ValueError("unsupported SSH interaction policy")
    if interaction_policy in {"none", "broker"}:
        allowed = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "SSH_AUTH_SOCK",
        }
        env = {
            key: value
            for key, value in os.environ.items()
            if key in allowed or key.startswith("LC_")
        }
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        return NativeAuth(env=env, extra_opts=[], use_askpass=False)

    raw_auth_method = getattr(connection, 'auth_method', None)
    if raw_auth_method is None:
        raw_auth_method = getattr(connection, 'authentication_method', 0)
        raw_auth_method = getattr(raw_auth_method, 'value', raw_auth_method)
        raw_auth_method = 1 if raw_auth_method == 'password' else 0
    auth_method = int(raw_auth_method or 0)
    password_mode = (auth_method == 1)

    askpass_enabled = True
    try:
        if app_config is not None and hasattr(app_config, 'get_setting'):
            askpass_enabled = bool(app_config.get_setting('use-askpass', True))
    except Exception:
        askpass_enabled = True

    def _tty_only(*, password=None, mode=False) -> NativeAuth:
        env = os.environ.copy()
        env.pop('SSH_ASKPASS', None)
        env.pop('SSH_ASKPASS_REQUIRE', None)
        return NativeAuth(
            env=env, extra_opts=[], use_sshpass=False,
            password=password, use_askpass=False, password_mode=mode,
        )

    if not askpass_enabled:
        stored = _get_stored_password(connection, connection_manager) if password_mode else None
        logger.debug("resolve_native_auth: askpass disabled by setting")
        return _tty_only(password=stored if password_mode else None, mode=password_mode)

    if password_mode:
        stored_password = _get_stored_password(connection, connection_manager)
        if stored_password:
            # In-memory password may not be in the backend yet — stage a one-shot
            # file; keyring-backed passwords are looked up by host/user in askpass.
            env = _askpass_env_for_connection(
                connection,
                session_password=stored_password,
                secret_lookup_authoritative=bool(
                    getattr(connection_manager, "secret_lookup_authoritative", False)
                ),
            )
            logger.debug("resolve_native_auth: password method -> askpass")
            return NativeAuth(
                env=env, extra_opts=[], use_sshpass=False,
                password=stored_password, use_askpass=True, password_mode=True,
            )
        logger.debug("resolve_native_auth: password method, no stored password -> TTY")
        return _tty_only(mode=True)

    # From here: key-based (auth_method == 0, auto or specific key).

    # Discover the identity candidates once (the same set used by the
    # passphrase probe and the agent preload). Probe is best-effort: on any
    # error, fall back to askpass-on (never regress the saved-passphrase autofill).
    candidates = getattr(connection, 'resolved_identity_files', None)
    if not candidates:
        candidates = getattr(connection, 'identity_files', None)
    if not candidates and hasattr(connection, 'collect_identity_file_candidates'):
        try:
            candidates = connection.collect_identity_file_candidates()
        except Exception:
            candidates = None
    candidates = list(candidates or [])
    probe_failed = False
    passphrase_values = {}
    try:
        passphrase_values = {
            p: _get_stored_passphrase(
                p, connection_manager, suppress_errors=False
            )
            for p in candidates
        }
        passphrase_keys = [p for p, value in passphrase_values.items() if value]
    except Exception:
        passphrase_keys, probe_failed = [], True
    has_stored_passphrase = bool(passphrase_keys) or probe_failed
    session_passphrase = (
        passphrase_values.get(passphrase_keys[0]) if passphrase_keys else None
    )
    stored_password = _get_stored_password(connection, connection_manager)

    if has_stored_passphrase:
        # Combined auth: load key into agent when possible, then askpass covers
        # both passphrase re-prompts and the login password (REQUIRE=prefer so
        # MFA stays on the TTY).
        if (stored_password
                and _agent_preload_active(connection, app_config)
                and any(_load_key_into_agent(p, connection_manager)
                        for p in passphrase_keys)):
            env = _askpass_env_for_connection(
                connection,
                session_password=stored_password,
                session_passphrase=session_passphrase,
                secret_lookup_authoritative=bool(
                    getattr(connection_manager, "secret_lookup_authoritative", False)
                ),
            )
            logger.debug(
                "resolve_native_auth: combined auth -> agent key + askpass "
                "(passphrase/password; MFA on TTY)"
            )
            return NativeAuth(
                env=env, extra_opts=[], use_sshpass=False,
                password=stored_password, use_askpass=True, password_mode=False,
            )
        if stored_password:
            env = _askpass_env_for_connection(
                connection,
                session_password=stored_password,
                session_passphrase=session_passphrase,
                secret_lookup_authoritative=bool(
                    getattr(connection_manager, "secret_lookup_authoritative", False)
                ),
            )
        else:
            env = get_ssh_env_with_askpass(
                require="prefer",
                session_passphrase=(
                    None
                    if getattr(connection_manager, "secret_lookup_authoritative", False)
                    else session_passphrase
                ),
            )
        logger.debug("resolve_native_auth: key auth, askpass autofill (saved passphrase)")
        return NativeAuth(
            env=env, extra_opts=[], use_sshpass=False,
            password=stored_password, use_askpass=True, password_mode=False,
        )

    if stored_password:
        env = _askpass_env_for_connection(
            connection,
            session_password=stored_password,
            secret_lookup_authoritative=bool(
                getattr(connection_manager, "secret_lookup_authoritative", False)
            ),
        )
        logger.debug(
            "resolve_native_auth: key auth, saved password -> askpass "
            "(password autofill; MFA on TTY)"
        )
        return NativeAuth(
            env=env, extra_opts=[], use_sshpass=False,
            password=stored_password, use_askpass=True, password_mode=False,
        )

    # Nothing saved -> let SSH prompt naturally on the TTY.
    logger.debug("resolve_native_auth: key auth, nothing saved -> native TTY prompts")
    return _tty_only()


def build_native_command(
    connection: any,
    app_config: Optional[any] = None,
    command_type: str = 'ssh',
    remote_command: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    config_file: Optional[str] = None,
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

    # The daemon launch view resolves the authoritative config override.  A
    # frontend Config object is never consulted for an SSH filesystem path.
    config_override = config_file
    if config_override is None and hasattr(connection, '_resolve_config_override_path'):
        try:
            config_override = connection._resolve_config_override_path()
        except Exception:
            pass
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
    interaction_policy: str = "normal"
    target_override: Optional[str] = None
    force_tty: bool = False  # Force a remote TTY allocation (-t)


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
        if connection_manager and hasattr(connection_manager, 'get_connection_password'):
            stored = connection_manager.get_connection_password(connection)
            if stored:
                return stored
        host = getattr(connection, 'hostname', '') or getattr(connection, 'host', '')
        username = getattr(connection, 'username', '')
        if host and username:
            stored = connection_manager.get_password(host, username)
            if stored:
                return stored
    except Exception:
        pass
    return None


def _agent_preload_active(connection: any, app_config: Optional[any] = None) -> bool:
    """Whether this host's keys get preloaded into ssh-agent.

    Mirrors the gating in ``ConnectionManager._preload_keys_into_agent``. Combined
    auth (publickey AND password) relies on the key already being in the agent so
    ssh can complete pubkey without a passphrase prompt while askpass answers the
    login password. If preloading won't happen, we must NOT take the combined
    path (it would leave an encrypted key unusable); fall back to askpass-only
    autofill instead.
    """
    try:
        if getattr(connection, 'identity_agent_disabled', False):
            return False
        if (getattr(connection, 'identity_agent_directive', '') or '').strip():
            return False
        if app_config is not None and hasattr(app_config, 'get_setting'):
            return bool(app_config.get_setting('ssh.agent_preload_keys', True))
        return True
    except Exception:
        return True


def _load_key_into_agent(key_path: str, connection_manager: Optional[any] = None) -> bool:
    """Best-effort: load (and unlock) *key_path* in ssh-agent; True on success.

    Prefers the connection manager's preparer so every caller shares one
    agent-loading path (and tests can observe it); falls back to
    ``ensure_key_in_agent(force=True)``. Used by combined auth, which must know
    the key is actually in the agent before disabling askpass.
    """
    try:
        if connection_manager is not None and hasattr(connection_manager, 'prepare_key_for_connection'):
            return bool(connection_manager.prepare_key_for_connection(key_path))
        return bool(ensure_key_in_agent(key_path, force=True))
    except Exception:
        return False


def _get_stored_passphrase(
    key_path: str,
    connection_manager: Optional[any] = None,
    *,
    suppress_errors: bool = True,
) -> Optional[str]:
    """Get stored passphrase for key."""
    if not key_path:
        return None
    
    # Try connection_manager first if available
    if connection_manager:
        try:
            getter = getattr(connection_manager, 'get_key_passphrase', None)
            if getter is None:
                getter = getattr(connection_manager, 'lookup_key_passphrase', None)
            if callable(getter):
                result = getter(key_path)
                if result:
                    return result
                if getattr(
                    connection_manager, 'secret_lookup_authoritative', False
                ):
                    return None
        except Exception:
            if not suppress_errors:
                raise
    
    # Fallback to direct lookup (works even without connection_manager)
    try:
        result = lookup_passphrase(key_path)
        if result:
            return result
    except Exception:
        if not suppress_errors:
            raise
    
    return None


# App-preference keys → OpenSSH ``-o`` option names for positive integers.
_APP_POSITIVE_INT_OPTIONS = (
    ('connection_timeout', 'ConnectTimeout'),
    ('connection_attempts', 'ConnectionAttempts'),
    ('keepalive_interval', 'ServerAliveInterval'),
    ('keepalive_count_max', 'ServerAliveCountMax'),
)


def _append_positive_int_option(cmd: List[str], raw_value, option_name: str) -> None:
    """Append ``-o Option=N`` when *raw_value* coerces to a positive int."""
    if not raw_value:
        return
    try:
        value = int(raw_value)
    except (ValueError, TypeError):
        return
    if value > 0:
        cmd.extend(['-o', f'{option_name}={value}'])


def _append_app_ssh_overrides(cmd: List[str], app_ssh_config: dict, *, is_copy_id: bool) -> None:
    """Apply app-level SSH prefs (verbosity, compression, timeouts, host keys)."""
    verbosity = int(app_ssh_config.get('verbosity', 0) or 0)
    if verbosity > 0 and not is_copy_id:
        cmd.extend(['-v'] * min(verbosity, 3))

    if bool(app_ssh_config.get('compression', False)) and not is_copy_id:
        cmd.append('-C')

    if bool(app_ssh_config.get('batch_mode', False)) and not is_copy_id:
        cmd.extend(['-o', 'BatchMode=yes'])

    for cfg_key, option_name in _APP_POSITIVE_INT_OPTIONS:
        _append_positive_int_option(cmd, app_ssh_config.get(cfg_key), option_name)

    strict_host = str(app_ssh_config.get('strict_host_key_checking', '') or '').strip()
    auto_add = bool(app_ssh_config.get('auto_add_host_keys', True))
    if strict_host:
        cmd.extend(['-o', f'StrictHostKeyChecking={strict_host}'])
    elif auto_add:
        cmd.extend(['-o', 'StrictHostKeyChecking=accept-new'])


def _append_identity_and_proxy(
    cmd: List[str],
    config: Dict[str, Union[str, List[str]]],
    *,
    is_copy_id: bool,
) -> None:
    """Append IdentityFile / CertificateFile / Proxy* from resolved SSH config."""
    # Never for ssh-copy-id: there -i selects the key to install, and the
    # operation must authenticate with the key being copied.
    if not is_copy_id:
        identity_files = _get_ssh_config_list(config, 'identityfile')
        for identity_file in identity_files:
            if identity_file and os.path.isfile(os.path.expanduser(identity_file)):
                cmd.extend(['-i', os.path.expanduser(identity_file)])
                # If IdentitiesOnly is set, only use this key
                if _get_ssh_config_value(config, 'identitiesonly', '').lower() in ('yes', 'true', '1'):
                    cmd.extend(['-o', 'IdentitiesOnly=yes'])
                    break  # Only first key when IdentitiesOnly is set

    cert_file = _get_ssh_config_value(config, 'certificatefile')
    if cert_file and cert_file.strip() and os.path.isfile(os.path.expanduser(cert_file)):
        cmd.extend(['-o', f'CertificateFile={cert_file}'])

    proxy_command = _get_ssh_config_value(config, 'proxycommand')
    if proxy_command and proxy_command.strip():
        cmd.extend(['-o', f'ProxyCommand={proxy_command}'])

    proxy_jump = [j for j in _get_ssh_config_list(config, 'proxyjump') if j and j.strip()]
    if proxy_jump:
        cmd.extend(['-o', f'ProxyJump={",".join(proxy_jump)}'])


def _build_base_ssh_command(
    connection: any,
    config: Dict[str, Union[str, List[str]]],
    app_config: Optional[any] = None,
    command_type: str = 'ssh',
    *,
    config_file: Optional[str] = None,
) -> List[str]:
    """
    Build base SSH command from SSH config and connection settings.
    This matches default SSH behavior as closely as possible.
    """
    if command_type == 'scp':
        cmd = ['scp']
    elif command_type == 'sftp':
        cmd = ['sftp']
    elif command_type == 'ssh-copy-id':
        cmd = ['ssh-copy-id']
    else:
        cmd = ['ssh']

    if config_file:
        cmd.extend(['-F', config_file])

    # ssh-copy-id is a shell script with a restricted option set (-i/-p/-o/-f/
    # -n/-s/-x): it rejects flags ssh/scp/sftp share (-v, -C, -A), its -i names
    # the key being COPIED rather than an authentication identity, and BatchMode
    # would defeat its interactive purpose (first login usually needs a prompt).
    is_copy_id = (command_type == 'ssh-copy-id')

    app_ssh_config = {}
    if app_config:
        try:
            app_ssh_config = app_config.get_ssh_config() if hasattr(app_config, 'get_ssh_config') else {}
        except Exception:
            pass

    _append_app_ssh_overrides(cmd, app_ssh_config, is_copy_id=is_copy_id)

    # Apply port if specified and not default
    port = getattr(connection, 'port', None)
    if port and port != 22:
        cmd.extend(['-P' if command_type == 'scp' else '-p', str(port)])

    _append_identity_and_proxy(cmd, config, is_copy_id=is_copy_id)

    # X11 forwarding only for ssh: scp -X selects transfer protocol and sftp -X
    # sets an sftp option, so a bare -X there would swallow the next argument.
    forward_x11 = _get_ssh_config_value(config, 'forwardx11', '').lower()
    if forward_x11 in ('yes', 'true', '1') and command_type == 'ssh':
        cmd.append('-X')

    forward_agent = _get_ssh_config_value(config, 'forwardagent', '').lower()
    if forward_agent in ('yes', 'true', '1') and not is_copy_id:
        cmd.append('-A')
        cmd.extend(['-o', 'ForwardAgent=yes'])

    # Port forwards are useless for file-transfer operations and harmful when
    # ExitOnForwardFailure=yes is set (a port already in use by an active
    # terminal session would kill the transfer). ClearAllForwardings only
    # cancels Local/Remote/Dynamic forwards; X11 and agent forwarding are unaffected.
    if command_type in ('scp', 'ssh-copy-id'):
        cmd.extend(['-o', 'ClearAllForwardings=yes'])
        # One askpass cancel should end the operation (not OpenSSH's default 3).
        # Interactive terminals keep the OpenSSH default so MFA can retry.
        _append_single_password_prompt(cmd)

    return cmd


def _append_single_password_prompt(cmd: List[str]) -> None:
    """Force NumberOfPasswordPrompts=1 for non-interactive transfer tools."""
    for entry in cmd:
        if entry and 'NumberOfPasswordPrompts' in str(entry):
            return
    cmd.extend(['-o', 'NumberOfPasswordPrompts=1'])


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
        # Resolve auth first — env + extra_opts are runtime concerns owned by
        # this adapter; argv composition is delegated to core.ssh.
        from .core.ssh import (
            AuthMethod,
            HostKeyMode,
            LaunchMode,
            SSHLaunchRequest,
            build_ssh_process_spec,
        )

        config_override = None
        if hasattr(connection, '_resolve_config_override_path'):
            try:
                config_override = connection._resolve_config_override_path()
            except Exception:
                config_override = None

        app_ssh_config = {}
        overrides: List[str] = []
        if app_config:
            try:
                app_ssh_config = app_config.get_ssh_config() if hasattr(app_config, 'get_ssh_config') else {}
            except Exception:
                app_ssh_config = {}
            raw_overrides = app_ssh_config.get('ssh_overrides', [])
            if isinstance(raw_overrides, (list, tuple)):
                overrides = [str(entry) for entry in raw_overrides if entry]

        # Default keepalive injection into the overrides list (core builder
        # appends ssh_overrides verbatim).
        keepalive_cmd: List[str] = []
        _maybe_append_default_keepalive(keepalive_cmd, overrides, app_ssh_config)
        overrides = list(overrides) + keepalive_cmd

        auth = resolve_native_auth(
            connection,
            connection_manager,
            app_config,
            interaction_policy=ctx.interaction_policy,
        )

        extra_options: List[str] = list(auth.extra_opts or [])
        if ctx.command_type in ('scp', 'ssh-copy-id', 'sftp'):
            if not any('NumberOfPasswordPrompts' in str(e) for e in extra_options + overrides):
                extra_options.extend(['-o', 'NumberOfPasswordPrompts=1'])
        if extra_args:
            extra_options.extend(str(a) for a in extra_args if a)

        batch_mode = False
        host_key_mode = None
        if ctx.interaction_policy == "none":
            batch_mode = True
            host_key_mode = HostKeyMode.YES
        elif (
            bool(app_ssh_config.get('batch_mode', False))
            and not auth.password_mode
            and not auth.use_askpass
            and not auth.password
        ):
            batch_mode = True

        native_target = ctx.target_override or host_label
        if ctx.target_override is None and hasattr(connection, 'resolve_host_identifier'):
            try:
                resolved = connection.resolve_host_identifier()
                if resolved:
                    native_target = resolved
            except Exception:
                pass

        launch_mode = LaunchMode.INTERACTIVE
        if ctx.command_type == 'scp':
            launch_mode = LaunchMode.SCP
        elif ctx.command_type == 'sftp':
            launch_mode = LaunchMode.SFTP
        elif ctx.command_type == 'ssh-copy-id':
            launch_mode = LaunchMode.COPY_ID
        if ctx.interaction_policy == "none":
            launch_mode = LaunchMode.BATCH

        # LaunchMode only tunes validation/options; it never picks the binary.
        # scp transfers must exec scp (ssh would treat the first source path as
        # the hostname). sftp deliberately keeps ssh: the daemon rides
        # ``ssh -s <alias> sftp`` (subsystem request), and ssh-copy-id composes
        # its own argv via _build_base_ssh_command.
        executable = 'scp' if ctx.command_type == 'scp' else 'ssh'

        req = SSHLaunchRequest(
            destination=native_target,
            executable=executable,
            config_file=config_override,
            ssh_overrides=overrides,
            extra_options=extra_options,
            batch_mode=batch_mode,
            host_key_mode=host_key_mode,
            remote_command=ctx.remote_command,
            askpass_required=bool(auth.use_askpass),
            env=dict(auth.env or {}),
            auth_method=(
                AuthMethod.PASSWORD if auth.password_mode else AuthMethod.PUBLIC_KEY
            ),
            launch_mode=launch_mode,
            force_tty=bool(getattr(ctx, "force_tty", False)),
        )
        spec = build_ssh_process_spec(req)
        env = dict(spec.env)
        env.update(dict(auth.env or {}))

        return SSHConnectionCommand(
            command=list(spec.argv),
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
