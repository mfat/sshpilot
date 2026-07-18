"""Shared helpers for the SCP UI (VTE transfer path in ``scp_window``).

Headless ``download_file`` / ``upload_file`` helpers were removed; transfers
run in a terminal via ``ScpWindowController._start_scp_transfer``.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, List, Optional, Dict, Any, Tuple

from .ssh_connection_builder import _build_base_ssh_command
from .ssh_config_utils import get_effective_ssh_config

__all__ = [
    'assemble_scp_transfer_args',
    'classify_sftp_error',
    'insert_legacy_scp_flag',
    'legacy_scp_flag_unsupported',
    '_apply_native_auth_env',
    '_build_scp_argv_prefix',
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
