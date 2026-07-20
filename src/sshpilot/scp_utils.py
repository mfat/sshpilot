"""Shared helpers for the SCP UI (VTE transfer path in ``scp_window``).

Headless ``download_file`` / ``upload_file`` helpers were removed; transfers
run in a terminal via ``ScpWindowController._start_scp_transfer``.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from gettext import gettext as _
from typing import Iterable, List, Optional, Dict, Any, Tuple

from .ssh_connection_builder import _build_base_ssh_command
from .ssh_config_utils import get_effective_ssh_config

logger = logging.getLogger(__name__)

__all__ = [
    '_apply_native_auth_env',
    '_build_scp_argv_prefix',
    '_summarize_listing_error',
    'assemble_scp_transfer_args',
    'classify_sftp_error',
    'insert_legacy_scp_flag',
    'legacy_scp_flag_unsupported',
    'list_remote_files',
]


_REMOTE_SPEC_RE = re.compile(r"^[^@]+@(?:\[[^\]]+\]|[^:]+):.+$")


def _strip_brackets(value: str) -> str:
    if value.startswith('[') and value.endswith(']'):
        return value[1:-1]
    return value


def _extract_host(target: str) -> str:
    if '@' in target:
        host = target.split('@', 1)[1]
    else:
        host = target
    return _strip_brackets(host)


def _normalize_remote_sources(target: str, sources: Iterable[str]) -> List[str]:
    host = _extract_host(target)
    host_variants = [host] if host else []
    if host and ':' in host:
        bracketed = f"[{host}]"
        if bracketed not in host_variants:
            host_variants.append(bracketed)
    normalized: List[str] = []
    for item in sources:
        path = (item or '').strip()
        if not path:
            continue
        if path.startswith(f"{target}:"):
            normalized.append(path)
            continue
        if host_variants:
            matched_host_variant = False
            for host_variant in host_variants:
                if path.startswith(f"{host_variant}:"):
                    normalized.append(path)
                    matched_host_variant = True
                    break
            if matched_host_variant:
                continue
        if _REMOTE_SPEC_RE.match(path):
            normalized.append(path)
            continue
        normalized.append(f"{target}:{path}")
    return normalized


def assemble_scp_transfer_args(
    target: str,
    sources: Iterable[str],
    destination: str,
    direction: str,
) -> Tuple[List[str], str]:
    """Return normalized scp sources and destination arguments for a transfer.

    Parameters
    ----------
    target:
        The ``user@host`` string for the connection (user may be omitted).
    sources:
        Iterable of source paths supplied by the caller.
    destination:
        Destination path (remote directory for uploads or local path for downloads).
    direction:
        Either ``"upload"`` or ``"download"``.
    """
    direction_value = (direction or '').lower()
    if direction_value not in {'upload', 'download'}:
        raise ValueError(f"Unsupported scp direction: {direction}")

    if direction_value == 'upload':
        cleaned_sources = [s for s in sources if s]
        return cleaned_sources, f"{target}:{destination}"

    remote_sources = _normalize_remote_sources(target, sources)
    return remote_sources, destination


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


def _apply_native_auth_env(env: Dict[str, str], auth: Any) -> None:
    """Merge the shared native auth env into ``env``, honoring deletions.

    dict.update() cannot remove keys that are absent from the source, so we
    explicitly drop askpass/agent/session-password vars that the auth resolver
    cleared (e.g. SSH_ASKPASS in password mode, a consumed session id).
    """
    env.update(auth.env)
    for key in (
        'SSH_ASKPASS',
        'SSH_ASKPASS_REQUIRE',
        'SSH_AUTH_SOCK',
        'SSHPILOT_SESSION_PASSWORD_ID',
        'SSHPILOT_SESSION_PASSWORD_FILE',
        'SSHPILOT_PASSWORD_USER',
        'SSHPILOT_PASSWORD_HOSTS',
    ):
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


def _summarize_listing_error(raw_stderr: str, fallback: str) -> str:
    """Turn raw ssh stderr into a concise message for the browse UI.

    Strips ``ssh -v`` debug chatter (so the verbose log is never dumped into
    the SCP browse window) and, when the remainder is an auth failure — e.g.
    the user cancelled the password/OTP prompt — shows a clean line instead.
    """
    from .ssh_utils import clean_ssh_stderr, is_ssh_auth_failure_text

    cleaned = clean_ssh_stderr(raw_stderr)
    if not cleaned:
        return fallback
    if is_ssh_auth_failure_text(cleaned):
        return _('Authentication failed or cancelled.')
    return cleaned


def list_remote_files(
    connection,
    remote_path: str,
    *,
    connection_manager=None,
    config=None,
    timeout: float = 10,
) -> Tuple[List[Tuple[str, bool]], Optional[str]]:
    """List remote files via the native SSH/auth path.

    Uses ``build_ssh_connection`` + ``resolve_native_auth`` (askpass for
    passwords, passphrases, and MFA). No sshpass. Returns
    ``(entries, error_message)`` where entries are ``(name, is_directory)``.
    """
    if connection is None:
        return [], _('Missing host information.')

    from .remote_path_utils import (
        _normalize_remote_path,
        _quote_remote_path_for_shell,
    )
    from .ssh_connection_builder import (
        ConnectionContext,
        apply_headless_askpass_env,
        build_ssh_connection,
    )

    safe_path = _normalize_remote_path(remote_path)
    command_path = _quote_remote_path_for_shell(safe_path)
    # -L dereferences symlinks when classifying, so a symlink that points to a
    # directory is marked with a trailing "/" (and thus shown/navigated as a
    # folder). -p alone leaves symlinked dirs unmarked. See issue #1002.
    list_command = f"LC_ALL=C ls -1pL --color=never -- {command_path}"
    wrapped_command = (
        "set -f; "
        "printf '__SSHPILOT_BEGIN__\\n'; "
        f"{list_command}; "
        "status=$?; "
        "printf '__SSHPILOT_STATUS__%s\\n' \"$status\"; "
        "printf '__SSHPILOT_END__\\n'; "
        "exit $status"
    )
    remote_command = f"sh -lc {shlex.quote(wrapped_command)}"

    if config is None:
        try:
            from .config import Config
            config = Config()
        except Exception:
            config = None

    try:
        prepared = build_ssh_connection(
            ConnectionContext(
                connection=connection,
                connection_manager=connection_manager,
                config=config,
                command_type='ssh',
                remote_command=remote_command,
                native_mode=True,
            )
        )
        env = apply_headless_askpass_env(
            prepared.env,
            connection,
            session_password=getattr(prepared, 'password', None),
        )

        # A staged secret autofills instantly, so a short timeout only guards
        # against network stalls. With nothing staged, askpass may pop a
        # dialog (password/passphrase/OTP/FIDO) and a human needs time to
        # answer it — don't kill ssh mid-prompt.
        if not getattr(prepared, 'password', None):
            timeout = max(timeout, 180)

        result = subprocess.run(
            list(prepared.command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )

        stdout_lines = result.stdout.splitlines()
        begin_idx = next((idx for idx, line in enumerate(stdout_lines)
                          if line.strip() == '__SSHPILOT_BEGIN__'), None)
        status_idx = next((idx for idx, line in enumerate(stdout_lines)
                           if line.startswith('__SSHPILOT_STATUS__')), None)
        if begin_idx is None or status_idx is None or status_idx < begin_idx:
            logger.warning('SCP: Unexpected remote listing output for %s', safe_path)
            return [], _summarize_listing_error(
                result.stderr, _('Unable to parse remote listing output.'))
        try:
            status_line = stdout_lines[status_idx]
            status_code = int(status_line.replace('__SSHPILOT_STATUS__', '').strip() or '0')
        except ValueError:
            status_code = result.returncode

        listing_lines = stdout_lines[begin_idx + 1:status_idx]
        if status_code != 0:
            stderr = _summarize_listing_error(
                result.stderr, _('Failed to list remote directory.'))
            logger.warning('SCP: Remote list failed (%s): %s', safe_path, stderr)
            return [], stderr
        entries: List[Tuple[str, bool]] = []
        for raw_line in listing_lines:
            line = raw_line.rstrip()
            if not line:
                continue
            is_dir = line.endswith('/')
            name = line[:-1] if is_dir else line
            entries.append((name, is_dir))
        return entries, None
    except Exception as exc:
        logger.error('SCP: Error listing remote files: %s', exc)
        return [], str(exc)
