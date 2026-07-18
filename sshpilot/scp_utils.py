"""Utilities for SCP file transfers using ssh_connection_builder."""

from __future__ import annotations

import os
import logging
import subprocess
from typing import List, Optional, Dict, Any

from .ssh_connection_builder import (
    _build_base_ssh_command,
    resolve_native_auth,
)
from .ssh_config_utils import get_effective_ssh_config
from .ssh_password_exec import assemble_scp_transfer_args, wrap_argv_with_sshpass
from .remote_path_utils import _format_ssh_target

logger = logging.getLogger(__name__)


SFTP_UNAVAILABLE_MESSAGE = (
    "Could not start an SFTP session on the remote host. The remote SSH server "
    "may not have an SFTP server enabled (the 'Subsystem sftp' line in its "
    "sshd_config) or sftp-server may not be installed. Ask the server "
    "administrator to enable or install sftp-server."
)

# Substrings (case-insensitive) that indicate the remote SFTP subsystem is
# missing or could not be started. Used to map cryptic SSH/scp errors to a
# clear, actionable message.
_SFTP_UNAVAILABLE_MARKERS = (
    "subsystem request failed",
    "sftp-server",
    "subsystem sftp",
    "received message too long",
    "eof during negotiation",
    "channel closed",
    "connection closed",
)


def classify_sftp_error(error_text: Optional[str]) -> Optional[str]:
    """Return a friendly message when ``error_text`` indicates a missing SFTP server.

    Inspects an SSH exception message or scp stderr blob for known markers of
    an unavailable/failed SFTP subsystem. Returns a single user-facing string when
    a marker is found, otherwise ``None``.
    """
    if not error_text:
        return None
    lowered = str(error_text).lower()
    if any(marker in lowered for marker in _SFTP_UNAVAILABLE_MARKERS):
        return SFTP_UNAVAILABLE_MESSAGE
    if "sftp" in lowered and "not found" in lowered:
        return SFTP_UNAVAILABLE_MESSAGE
    return None


# Markers indicating the running scp binary is too old to understand the
# legacy-protocol flag (-O). Used to avoid masking the real error on retry.
_LEGACY_FLAG_UNSUPPORTED_MARKERS = (
    "unknown option",
    "illegal option",
    "invalid option",
)


def insert_legacy_scp_flag(argv: List[str]) -> List[str]:
    """Return a copy of an scp ``argv`` with the legacy-protocol flag ``-O``.

    The flag is inserted immediately after the scp binary (the first element).
    Idempotent: if ``-O`` is already present the argv is returned unchanged.
    """
    if not argv:
        return list(argv)
    if '-O' in argv:
        return list(argv)
    return [argv[0], '-O', *argv[1:]]


def legacy_scp_flag_unsupported(error_text: Optional[str]) -> bool:
    """Return True when ``error_text`` indicates scp does not support ``-O``."""
    if not error_text:
        return False
    lowered = str(error_text).lower()
    return any(marker in lowered for marker in _LEGACY_FLAG_UNSUPPORTED_MARKERS)


def _record_download_failure(
    result_details: Optional[Dict[str, Any]],
    error_text: Optional[str],
) -> None:
    """Populate ``result_details`` (when provided) with failure context."""
    if result_details is None:
        return
    stderr = (error_text or '').strip()
    if stderr:
        result_details['stderr'] = stderr
    friendly = classify_sftp_error(stderr)
    if friendly:
        result_details['friendly'] = friendly


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


def _apply_native_auth_env(env: Dict[str, str], auth: Any) -> None:
    """Merge the shared native auth env into ``env``, honoring deletions.

    dict.update() cannot remove keys that are absent from the source, so we
    explicitly drop askpass/agent vars that the auth resolver cleared (e.g.
    SSH_ASKPASS in password mode).
    """
    env.update(auth.env)
    for key in ('SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE', 'SSH_AUTH_SOCK'):
        if key not in auth.env:
            env.pop(key, None)


def _build_scp_argv_prefix(
    connection: Any,
    config: Any,
    recursive: bool,
    known_hosts_path: Optional[str],
    extra_ssh_opts: Optional[List[str]],
    auth: Any,
) -> List[str]:
    """Build the scp argv up to (but not including) the transfer sources/dest.

    SCP runs against explicit parameters (raw host, explicit keyfile/port), not
    a saved ~/.ssh/config alias, so it builds an explicit command via the shared
    option builder (_build_base_ssh_command) plus the explicit key and the shared
    authentication options (resolve_native_auth) — the same auth the terminal and
    ssh-copy-id use.
    """
    host_label = (
        getattr(connection, 'nickname', '')
        or getattr(connection, 'host', '')
        or getattr(connection, 'hostname', '')
    )
    try:
        effective_config = get_effective_ssh_config(host_label) if host_label else {}
    except Exception:
        effective_config = {}

    argv = _build_base_ssh_command(connection, effective_config, config, 'scp')

    if recursive and '-r' not in argv:
        argv.insert(1, '-r')

    if known_hosts_path:
        argv.extend(['-o', f'UserKnownHostsFile={known_hosts_path}'])

    # Explicit keyfile (SCP connections are not config aliases).
    keyfile_value = getattr(connection, 'keyfile', '') or ''
    key_select_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
    if (
        keyfile_value
        and not str(keyfile_value).startswith('Select key file')
        and key_select_mode in (1, 2)
        and os.path.isfile(os.path.expanduser(keyfile_value))
    ):
        expanded = os.path.expanduser(keyfile_value)
        if expanded not in argv:
            argv.extend(['-i', expanded])
        if key_select_mode == 1 and 'IdentitiesOnly=yes' not in argv:
            argv.extend(['-o', 'IdentitiesOnly=yes'])

    # Shared authentication options from resolve_native_auth.
    argv.extend(auth.extra_opts)

    if extra_ssh_opts:
        argv.extend(extra_ssh_opts)

    return argv


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
    result_details: Optional[Dict[str, Any]] = None,
) -> bool:
    """Download a remote file (or directory when ``recursive``) via SCP using ssh_connection_builder.

    When ``result_details`` is provided, it is populated on failure with the
    captured ``stderr`` and, when recognizable, a ``friendly`` message (for
    example when the remote SFTP server is missing).
    """
    if not host or not remote_file or not local_path:
        return False

    env = (inherit_env or os.environ).copy()
    
    # Create connection object for ssh_connection_builder.
    # use_publickey means key-based method (auto/specific key): never force
    # auth_method=1 just because a password is present — that would enable
    # sshpass via resolve_native_auth.
    auth_method = 0 if use_publickey else (1 if password else 0)
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
    
    # Resolve shared authentication (askpass + keyring + agent bypass, or sshpass).
    try:
        auth = resolve_native_auth(connection, connection_manager, config)
        _apply_native_auth_env(env, auth)
    except Exception as e:
        logger.error(f'SCP: Failed to resolve authentication: {e}')
        return False

    # Inject sshpass-specific auth-steering options that resolve_native_auth does
    # not place in auth.extra_opts. These ensure SSH hands the password prompt to
    # sshpass rather than trying keys first (which would either succeed silently
    # or prompt interactively in a way sshpass cannot intercept).
    # Only for password-method auth (use_sshpass); never for key-based.
    effective_extra = list(extra_ssh_opts or [])
    if auth.use_sshpass and auth.password:
        pref = 'keyboard-interactive,password'
        effective_extra = [
            '-o', f'PreferredAuthentications={pref}',
            '-o', 'NumberOfPasswordPrompts=1',
        ] + effective_extra

    # Build SCP command via the unified prefix builder (handles -r, app SSH
    # settings, ClearAllForwardings, strict-host policy, keyfile, etc.).
    target = _format_ssh_target(host, user)
    try:
        transfer_sources, transfer_destination = assemble_scp_transfer_args(
            target,
            [remote_file],
            local_path,
            'download',
        )

        argv = _build_scp_argv_prefix(
            connection, config, recursive, known_hosts_path, effective_extra, auth
        )
        argv.extend(transfer_sources)
        argv.append(transfer_destination)

        def _run_attempt(scp_argv: List[str]) -> subprocess.CompletedProcess:
            if auth.use_sshpass and auth.password:
                wrapped, cleanup = wrap_argv_with_sshpass(scp_argv, auth.password)
                try:
                    return subprocess.run(
                        wrapped, check=False, text=True, capture_output=True, env=env
                    )
                finally:
                    cleanup()
            return subprocess.run(
                scp_argv, check=False, text=True, capture_output=True, env=env
            )

        completed = _run_attempt(argv)
        if completed.returncode != 0:
            stderr = (completed.stderr or '').strip()
            if stderr:
                logger.error('SCP: Download stderr: %s', stderr)
            # If the remote SFTP subsystem is unavailable, retry once using the
            # legacy SCP protocol (-O), which does not require sftp-server.
            if classify_sftp_error(stderr):
                logger.info('SCP: Retrying download with legacy protocol (-O) for %s', remote_file)
                legacy_completed = _run_attempt(insert_legacy_scp_flag(argv))
                if legacy_completed.returncode == 0:
                    return True
                legacy_stderr = (legacy_completed.stderr or '').strip()
                if legacy_stderr:
                    logger.error('SCP: Legacy download stderr: %s', legacy_stderr)
                # Keep the original failure when scp does not support -O.
                if not legacy_scp_flag_unsupported(legacy_stderr):
                    stderr = legacy_stderr or stderr
            _record_download_failure(result_details, stderr)
            return False
        return True
    except Exception as exc:
        logger.error('SCP: Download failed for %s: %s', remote_file, exc)
        _record_download_failure(result_details, str(exc))
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

    # use_publickey => key-based method: never force auth_method=1 for a
    # stored password (that would enable sshpass via resolve_native_auth).
    auth_method = 0 if use_publickey else (1 if password else 0)
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
    
    # Resolve shared authentication (askpass + keyring + agent bypass, or sshpass).
    try:
        auth = resolve_native_auth(connection, connection_manager, config)
        _apply_native_auth_env(env, auth)
    except Exception as e:
        logger.error(f'SCP: Failed to resolve authentication: {e}')
        return False

    # sshpass steering options only for password-method auth.
    effective_extra = list(extra_ssh_opts or [])
    if auth.use_sshpass and auth.password:
        pref = 'keyboard-interactive,password'
        effective_extra = [
            '-o', f'PreferredAuthentications={pref}',
            '-o', 'NumberOfPasswordPrompts=1',
        ] + effective_extra

    # Build SCP command via the unified prefix builder.
    target = _format_ssh_target(host, user)
    try:
        transfer_sources, transfer_destination = assemble_scp_transfer_args(
            target,
            [local_file],
            remote_path,
            'upload',
        )

        argv = _build_scp_argv_prefix(
            connection, config, recursive, known_hosts_path, effective_extra, auth
        )
        argv.extend(transfer_sources)
        argv.append(transfer_destination)

        def _run_attempt(scp_argv: List[str]) -> subprocess.CompletedProcess:
            if auth.use_sshpass and auth.password:
                wrapped, cleanup = wrap_argv_with_sshpass(scp_argv, auth.password)
                try:
                    return subprocess.run(
                        wrapped, check=False, text=True, capture_output=True, env=env
                    )
                finally:
                    cleanup()
            return subprocess.run(
                scp_argv, check=False, text=True, capture_output=True, env=env
            )

        completed = _run_attempt(argv)
        if completed.returncode != 0:
            stderr = (completed.stderr or '').strip()
            if stderr:
                logger.error('SCP: Upload stderr: %s', stderr)
            # If the remote SFTP subsystem is unavailable, retry once using the
            # legacy SCP protocol (-O), which does not require sftp-server.
            if classify_sftp_error(stderr):
                logger.info('SCP: Retrying upload with legacy protocol (-O) for %s', local_file)
                legacy_completed = _run_attempt(insert_legacy_scp_flag(argv))
                if legacy_completed.returncode == 0:
                    return True
                legacy_stderr = (legacy_completed.stderr or '').strip()
                if legacy_stderr:
                    logger.error('SCP: Legacy upload stderr: %s', legacy_stderr)
                if not legacy_scp_flag_unsupported(legacy_stderr):
                    stderr = legacy_stderr or stderr
            return False
        return True
    except Exception as exc:
        logger.error('SCP: Upload failed for %s: %s', local_file, exc)
        return False


__all__ = ['assemble_scp_transfer_args', 'download_file', 'upload_file']
