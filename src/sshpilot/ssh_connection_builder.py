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
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass, field

from .core.ssh import ProcessSpec, build_ssh_process_spec  # noqa: F401 — re-export
from .core.settings.ssh_overrides import (
    coerce_yes_no_unset,
    keepalive_fallback_args,
    select_override_args,
)
from .platform.windows.environment import WINDOWS_PROCESS_ENV


# askpass_utils pulls in secret storage and GLib; it is only needed to build
# a real SSH command, not to import this module (the CLI pre-parse does that
# before GTK loads). These stay module attributes, so tests can patch them.
def get_ssh_env_with_askpass(*args, **kwargs):
    from .askpass_utils import get_ssh_env_with_askpass as implementation

    return implementation(*args, **kwargs)


def ensure_key_in_agent(*args, **kwargs):
    from .askpass_utils import ensure_key_in_agent as implementation

    return implementation(*args, **kwargs)


def lookup_passphrase(*args, **kwargs):
    from .askpass_utils import lookup_passphrase as implementation

    return implementation(*args, **kwargs)


def effective_username(connection) -> str:
    """The account OpenSSH will actually log in as.

    ``connection.username`` holds only what the Host block authored, so it is
    empty whenever the account is inherited (from a ``Host *`` block, or from
    OpenSSH's own local-user default). Secrets and any user-facing label must
    follow the *effective* account instead, or a stored password keyed on the
    real account stops being found.

    Providers that can resolve the account expose ``effective_username``; this
    falls back to the authored value so plain ``Connection`` projections and
    tests keep working.
    """
    resolved = str(getattr(connection, 'effective_username', '') or '').strip()
    if resolved:
        return resolved
    return str(getattr(connection, 'username', '') or '').strip()


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
        user = effective_username(connection)
        hosts = password_host_candidates(connection) or []
        canonical = canonical_password_host(connection)
        if canonical and canonical not in hosts:
            hosts = [canonical] + list(hosts)
    except Exception:
        user = effective_username(connection)
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
    password: Optional[str] = field(default=None, repr=False)  # Password for sshpass (if needed)
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
    password: Optional[str] = field(default=None, repr=False) # Stored password (askpass context / UI messaging)
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
        windows = os.name == "nt"
        # Windows environment names are case-insensitive; POSIX ones are not.
        env = {
            key: value
            for key, value in os.environ.items()
            if (key.upper() if windows else key) in allowed
            or key.startswith("LC_")
            or (windows and key.upper() in WINDOWS_PROCESS_ENV)
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
        app_ssh_config = _app_ssh_config(app_config)
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
    known_hosts_path: Optional[str] = None  # Custom known hosts file
    native_mode: bool = False  # Use native SSH mode (minimal command)
    interaction_policy: str = "normal"
    target_override: Optional[str] = None
    force_tty: bool = False  # Force a remote TTY allocation (-t)


def _app_ssh_config(app_config: Any) -> Dict[str, Any]:
    """App-level ``ssh.*`` preferences as a dict, whatever ``app_config`` is.

    Always a mapping: callers index it directly, and a config shim that returns
    ``None`` (or anything else) must not take the whole command build down."""
    if not app_config or not hasattr(app_config, 'get_ssh_config'):
        return {}
    try:
        values = app_config.get_ssh_config()
    except Exception:
        return {}
    return values if isinstance(values, dict) else {}


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
        username = effective_username(connection)
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


# Options that decide what a launch *is*. On anything other than a plain
# terminal session the app chooses them (a forward runs ``-N``, the file
# manager runs the sftp subsystem, Host Info runs one command), so a Settings
# override for one of them is left out of that launch. Reordering is not
# enough: OpenSSH refuses to start on several of the combinations
# (``RemoteCommand`` with a command, ``SessionType`` with ``-N``/``-s``), and
# ``ClearAllForwardings=yes`` drops the very ``-L`` a forward was started for.
OPERATION_OWNED_KEYWORDS = frozenset({
    'remotecommand',
    'sessiontype',
    'requesttty',
    'forkafterauthentication',
    'stdinnull',
    'localcommand',
    'permitlocalcommand',
    'clearallforwardings',
})

# ssh-copy-id additionally has to stay interactive and must not leave a
# multiplex master behind for a one-off key install.
_COPY_ID_DROPPED_KEYWORDS = OPERATION_OWNED_KEYWORDS | frozenset({
    'batchmode', 'controlmaster', 'controlpath', 'controlpersist',
})

_HELPER_FLAGS = frozenset({'-N', '-s', '-T'})


def _is_helper_launch(command_type: str, remote_command, extra_args) -> bool:
    """Whether this launch is an app operation rather than a terminal session."""
    if command_type != 'ssh' or remote_command:
        return True
    return any(str(arg) in _HELPER_FLAGS for arg in (extra_args or ()))


def _append_app_ssh_overrides(cmd: List[str], app_ssh_config: dict, *, is_copy_id: bool) -> None:
    """Apply app-level SSH prefs (verbosity, compression, timeouts, host keys)."""
    verbosity = int(app_ssh_config.get('verbosity', 0) or 0)
    if verbosity > 0 and not is_copy_id:
        cmd.extend(['-v'] * min(verbosity, 3))

    if not is_copy_id:
        try:
            compression = coerce_yes_no_unset(app_ssh_config.get('compression'))
        except (TypeError, ValueError):
            compression = ''
        if compression:
            # Prefer ``-o Compression=`` over ``-C`` so force-no works and a
            # connection's own Compression can still win (first-value-wins).
            cmd.extend(['-o', f'Compression={compression}'])

    for cfg_key, option_name in _APP_POSITIVE_INT_OPTIONS:
        _append_positive_int_option(cmd, app_ssh_config.get(cfg_key), option_name)

    # Empty means "do not configure": send nothing and let OpenSSH resolve
    # the option from the user's SSH configuration.
    strict_host = str(app_ssh_config.get('strict_host_key_checking', '') or '').strip()
    if strict_host:
        cmd.extend(['-o', f'StrictHostKeyChecking={strict_host}'])


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
    proxy_jump = [j for j in _get_ssh_config_list(config, 'proxyjump') if j and j.strip()]
    if proxy_command and proxy_command.strip() and proxy_jump:
        # Both resolve: OpenSSH applies the first line in the file, so no
        # -o ordering here may impose a precedence — the -F config already
        # on argv resolves exactly what plain `ssh host` would do.
        pass
    else:
        if proxy_command and proxy_command.strip():
            cmd.extend(['-o', f'ProxyCommand={proxy_command}'])
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

    app_ssh_config = _app_ssh_config(app_config)

    # Settings ▸ SSH Options first: they override the connection's own values,
    # the same rule the session command follows. The composed fragment is the
    # whole page (additional options, address family, legacy algorithms); the
    # typed subset below only stands in when a caller supplies raw settings.
    composed = select_override_args(
        app_ssh_config.get('ssh_overrides'),
        drop=_COPY_ID_DROPPED_KEYWORDS if is_copy_id else OPERATION_OWNED_KEYWORDS,
        flags=not is_copy_id,
    )
    if composed:
        cmd.extend(composed)
    else:
        _append_app_ssh_overrides(cmd, app_ssh_config, is_copy_id=is_copy_id)
    # *config* is what OpenSSH resolved for this host, so it says whether the
    # user's configuration already sets a keepalive.
    cmd.extend(
        _keepalive_fallback(app_ssh_config, config, _configured_for(connection))
    )

    # Nothing from the Host block is repeated here. The destination is the
    # Host alias and the configuration file is the one a session uses, so
    # OpenSSH resolves user, port, jump host and every other option exactly
    # as `ssh <alias>` does -- including its first-value-wins precedence
    # between the Host block and any ``Host *`` block before it.

    _append_identity_and_proxy(cmd, config, is_copy_id=is_copy_id)

    # X11 forwarding only for ssh: scp -X selects transfer protocol and sftp -X
    # sets an sftp option, so a bare -X there would swallow the next argument.
    # Windows has no xauth, so OpenSSH refuses untrusted (-X) forwarding there
    # ("xauth key data not generated"); trusted (-Y) is the only mode that
    # works, and is what PuTTY does with Xming/VcXsrv.
    forward_x11 = _get_ssh_config_value(config, 'forwardx11', '').lower()
    if forward_x11 in ('yes', 'true', '1') and command_type == 'ssh':
        cmd.append('-Y' if os.name == 'nt' else '-X')

    forward_agent = _get_ssh_config_value(config, 'forwardagent', '').lower()
    if forward_agent in ('yes', 'true', '1') and not is_copy_id:
        cmd.append('-A')
        cmd.extend(['-o', 'ForwardAgent=yes'])

    # Port forwards are useless for file-transfer operations and harmful when
    # ExitOnForwardFailure=yes is set (a port already in use by an active
    # terminal session would kill the transfer). ClearAllForwardings only
    # cancels Local/Remote/Dynamic forwards; X11 and agent forwarding are unaffected.
    if is_copy_id:
        # The destination is the Host alias, so the block's RemoteCommand
        # would apply -- and ssh refuses to run ssh-copy-id's own remote
        # script next to one. A key install is no login: the block's
        # LocalCommand is not for it.
        cmd.extend(['-o', 'RemoteCommand=none', '-o', 'PermitLocalCommand=no'])
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


def _keepalive_fallback(app_ssh_config, effective, configured=None) -> List[str]:
    """The app's ServerAlive* options, when the configuration sets none.

    *effective* is what OpenSSH resolved for the host (``ssh -G``) and
    *configured* the options a configuration line sets for it. See
    :func:`sshpilot.core.settings.ssh_overrides.keepalive_fallback_args`.
    """
    try:
        return list(keepalive_fallback_args(app_ssh_config, effective, configured))
    except Exception as exc:  # never let keepalive injection break connecting
        logger.debug("Default keepalive injection skipped: %s", exc)
        return []


def _configured_for(connection) -> Dict[str, Any]:
    """The options a configuration line sets for *connection*, if known."""
    resolve = getattr(connection, 'configured_ssh_directives', None)
    if not callable(resolve):
        return {}
    try:
        return dict(resolve() or {})
    except Exception:
        logger.debug("Could not read configured directives", exc_info=True)
        return {}


def _effective_config_for(connection) -> Dict[str, Any]:
    """What OpenSSH resolves for *connection*, for launch-time decisions.

    The daemon's launch view resolves it (``ssh -G`` with the launch's own
    configuration file and Settings options). A connection object that cannot
    answer yields nothing, and the fallbacks that depend on it stay off.
    """
    resolve = getattr(connection, 'effective_ssh_config', None)
    if not callable(resolve):
        return {}
    try:
        return dict(resolve() or {})
    except Exception:
        logger.debug("Could not resolve effective config", exc_info=True)
        return {}


def _connection_is_isolated(connection) -> bool:
    """Whether this connection was declared in the isolated SSH root.

    The flag is stamped by the config loader from the root it parsed, so it
    is true for a Host declared in the isolated root itself *and* for one
    pulled into it through an Include.
    """
    value = getattr(connection, "isolated_mode", None)
    if value is None:
        data = getattr(connection, "data", None)
        if isinstance(data, dict):
            value = data.get("isolated_mode")
    return bool(value)


def _config_sets_known_hosts_file(effective) -> bool:
    """Whether the resolved configuration names its own ``UserKnownHostsFile``.

    ``ssh -G`` always prints the option; left unset it is OpenSSH's default
    pair, ``~/.ssh/known_hosts`` and ``~/.ssh/known_hosts2``.
    """
    value = (effective or {}).get('userknownhostsfile')
    if not value:
        return False
    if isinstance(value, (list, tuple)):
        value = ' '.join(str(entry) for entry in value)
    try:
        from .platform.paths import openssh_user_dir

        default_dir = os.path.normcase(os.path.abspath(str(openssh_user_dir())))
    except Exception:
        return False
    for entry in str(value).split():
        path = os.path.abspath(os.path.expanduser(entry))
        if (
            os.path.normcase(os.path.dirname(path)) != default_dir
            or os.path.basename(path) not in ('known_hosts', 'known_hosts2')
        ):
            return True
    return False


def _isolated_known_hosts_override(connection, effective=None) -> List[str]:
    """``-o UserKnownHostsFile=`` for an isolated-mode connection, else ``[]``.

    Default Mode deliberately emits nothing: ``~/.ssh/known_hosts`` is shared
    TOFU state for every SSH tool on the machine, so pinning it would break
    anyone who set ``UserKnownHostsFile`` themselves and would make sshPilot
    the owner of a file it does not own.

    A command-line ``-o`` beats every config file, so a configuration that
    names its own ``UserKnownHostsFile`` -- in the Host block, or in a
    ``Host *`` block of the isolated config -- would be overridden by this,
    the opposite of the intent. *effective* is what OpenSSH resolved for the
    host; when it shows a file other than OpenSSH's default, nothing is
    emitted and OpenSSH uses the configured one. Without *effective* only
    the Host block's own directive can be seen.
    """
    if not _connection_is_isolated(connection):
        return []
    if _config_sets_known_hosts_file(effective):
        return []
    try:
        from .ssh_config_formatter import _authored_directives

        data = getattr(connection, "data", None)
        if "userknownhostsfile" in _authored_directives(
            data if isinstance(data, dict) else {}
        ):
            return []
    except Exception:
        logger.debug("Could not read authored directives", exc_info=True)
    try:
        from .platform.paths import known_hosts_path_for

        return ["-o", f"UserKnownHostsFile={known_hosts_path_for(True)}"]
    except Exception:
        logger.debug("Could not resolve the isolated known_hosts path", exc_info=True)
        return []


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
    #   * app-level ssh_overrides (Settings ▸ SSH Options, keepalive
    #     included), emitted ahead of the connection's own options so they
    #     override them
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

        app_ssh_config = _app_ssh_config(app_config)
        overrides: List[str] = []
        raw_overrides = app_ssh_config.get('ssh_overrides', [])
        if isinstance(raw_overrides, (list, tuple)):
            overrides = [str(entry) for entry in raw_overrides if entry]

        # Nothing from the Host block is repeated on the command line: the
        # configuration file is the single source of truth, and OpenSSH
        # resolves it with its own precedence, as `ssh <alias>` would. What
        # OpenSSH resolved is read back only to decide the two fallbacks
        # below, which apply when the configuration says nothing.
        effective = _effective_config_for(connection)

        # Isolated Mode owns its own host keys, unless the isolated
        # configuration names a known_hosts file itself. Emitted last.
        isolated_known_hosts = _isolated_known_hosts_override(connection, effective)

        # The app's keepalive, only when the configuration sets none.
        overrides = list(overrides) + _keepalive_fallback(
            app_ssh_config, effective, _configured_for(connection)
        )

        auth = resolve_native_auth(
            connection,
            connection_manager,
            app_config,
            interaction_policy=ctx.interaction_policy,
        )

        # What the app itself needs for this launch: emitted before the
        # Settings overrides, so a caller's forced option (ControlMaster=no on
        # a forwarding-only tab, say) cannot be outranked.
        priority_options: List[str] = list(auth.extra_opts or [])
        if ctx.command_type in ('scp', 'ssh-copy-id', 'sftp'):
            if not any('NumberOfPasswordPrompts' in str(e) for e in priority_options + overrides):
                priority_options.extend(['-o', 'NumberOfPasswordPrompts=1'])
        if extra_args:
            priority_options.extend(str(a) for a in extra_args if a)

        if _is_helper_launch(ctx.command_type, ctx.remote_command, extra_args):
            overrides = select_override_args(
                overrides, drop=OPERATION_OWNED_KEYWORDS
            )
            # Host Info, dashboard probes, the file manager and dedicated
            # forwards are no login: the Host block's LocalCommand (a
            # "connected" notification, say) would fire on every probe. scp
            # passes the same option to its own ssh.
            if ctx.command_type != 'scp' and not any(
                'permitlocalcommand=' in str(o).lower() for o in priority_options
            ):
                priority_options.extend(['-o', 'PermitLocalCommand=no'])
        if (
            ctx.command_type != 'scp'
            and (ctx.remote_command or '-s' in priority_options)
            and not any('remotecommand=' in o.lower() for o in priority_options)
        ):
            # ssh refuses a command (or subsystem) on the command line next to
            # a RemoteCommand from any source, the config file included.
            priority_options.extend(['-o', 'RemoteCommand=none'])

        batch_mode = False
        host_key_mode = None
        if ctx.interaction_policy == "none":
            batch_mode = True
            host_key_mode = HostKeyMode.YES

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
        if ctx.interaction_policy == "none" and launch_mode is LaunchMode.INTERACTIVE:
            # BatchMode / StrictHostKeyChecking come from batch_mode and
            # host_key_mode above. Do not overwrite SCP/SFTP/COPY_ID modes:
            # those pair with the binary for scp-safe flags (-P, -o User=)
            # and LocalCommand validation. Overwriting them with BATCH while
            # executable stays scp would revive the -p/-l misparse.
            launch_mode = LaunchMode.BATCH

        # LaunchMode only tunes validation/options; it never picks the binary.
        # scp transfers must exec scp (ssh would treat the first source path as
        # the hostname). sftp deliberately keeps ssh: the daemon rides
        # ``ssh -s <alias> sftp`` (subsystem request), and ssh-copy-id composes
        # its own argv via _build_base_ssh_command.
        executable = 'scp' if ctx.command_type == 'scp' else 'ssh'

        # Windows has no xauth, so OpenSSH refuses untrusted X11 forwarding
        # there ("xauth key data not generated"). ForwardX11 itself comes from
        # the config file, so ask for trusted forwarding here; it only takes
        # effect when forwarding is on (issue #1365).
        platform_options: List[str] = []
        if (
            os.name == 'nt'
            and executable == 'ssh'
            and launch_mode is LaunchMode.INTERACTIVE
        ):
            platform_options = ['-o', 'ForwardX11Trusted=yes']

        # The isolated known_hosts fallback is last of all.
        req = SSHLaunchRequest(
            destination=native_target,
            executable=executable,
            config_file=config_override,
            priority_options=priority_options,
            ssh_overrides=overrides,
            extra_options=platform_options + isolated_known_hosts,
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
