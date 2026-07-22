"""Detect whether an SSH host/user is already saved in ssh config.

This module does **not** create connections. It only answers “is this
hostname/IP (+ username) already represented among saved SSH connections?”
so callers (CLI connect, and others later) can decide whether to offer
“Save as new connection”.

Resolution prefers ``ssh -G`` via
:func:`ssh_config_utils.get_effective_ssh_config` when available.
"""

from __future__ import annotations

import getpass
import logging
from typing import Any, Iterable, Optional, Tuple

from .ssh_config_utils import get_effective_ssh_config

logger = logging.getLogger(__name__)


def _as_scalar(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        for item in reversed(value):
            text = str(item or '').strip()
            if text:
                return text
        return ''
    return str(value).strip()


def _default_username() -> str:
    try:
        return getpass.getuser() or ''
    except Exception:
        return ''


def resolve_connection_host_user(
    connection: Any,
    *,
    config_file: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(hostname, username)`` for identity comparison.

    Hostnames are lowercased for comparison; usernames keep their case but are
    stripped. Empty username falls back to the current local user (ssh default).
    """
    hostname = _as_scalar(getattr(connection, 'hostname', None))
    username = _as_scalar(getattr(connection, 'username', None))

    host_label = ''
    try:
        resolve = getattr(connection, 'resolve_host_identifier', None)
        if callable(resolve):
            host_label = _as_scalar(resolve())
    except Exception:
        host_label = ''
    if not host_label:
        try:
            get_eff = getattr(connection, 'get_effective_host', None)
            if callable(get_eff):
                host_label = _as_scalar(get_eff())
        except Exception:
            host_label = ''
    if not host_label:
        host_label = (
            hostname
            or _as_scalar(getattr(connection, 'host', None))
            or _as_scalar(getattr(connection, 'nickname', None))
        )

    if host_label:
        try:
            effective = get_effective_ssh_config(host_label, config_file=config_file) or {}
        except Exception:
            effective = {}
        if isinstance(effective, dict):
            eff_host = _as_scalar(effective.get('hostname'))
            eff_user = _as_scalar(effective.get('user'))
            if eff_host:
                hostname = eff_host
            # ssh -G always emits a `user` line, defaulting to the local login
            # when the matched config has no User. That default must NOT clobber
            # an explicit username (e.g. `root` from `root@host` on the CLI):
            # `ssh -G <ip>` can't match a `Host <alias>` block, so it would
            # resolve the wrong user and make the host look unsaved.
            if eff_user and not username:
                username = eff_user

    if not hostname:
        hostname = host_label

    if not username:
        username = _default_username()

    return hostname.lower(), username


def identity_key(hostname: str, username: str) -> str:
    """Stable key for session-scoped dismissals."""
    return f"{(hostname or '').lower()}|{(username or '')}"


def is_unsaved_host(
    connection: Any,
    connection_manager: Any,
    *,
    config_file: Optional[str] = None,
) -> bool:
    """Return True when *connection* is not already represented in ssh config.

    Unsaved means:
    - resolved hostname/IP does not appear among saved SSH connections, or
    - that hostname exists but not with this username.
    """
    if connection is None or connection_manager is None:
        return True

    if (getattr(connection, 'protocol', 'ssh') or 'ssh') != 'ssh':
        return False

    cfg_path = config_file
    if not cfg_path:
        cfg_path = getattr(connection_manager, 'ssh_config_path', None) or None

    target_host, target_user = resolve_connection_host_user(
        connection, config_file=cfg_path,
    )
    if not target_host:
        return True

    try:
        saved: Iterable[Any] = connection_manager.get_connections()
    except Exception:
        saved = getattr(connection_manager, 'connections', None) or []

    for other in saved:
        if other is connection:
            continue
        if (getattr(other, 'protocol', 'ssh') or 'ssh') != 'ssh':
            continue
        other_host, other_user = resolve_connection_host_user(
            other, config_file=cfg_path,
        )
        if other_host == target_host and other_user == target_user:
            return False

    return True


# Back-compat alias (prefer is_unsaved_host).
is_new_connection = is_unsaved_host


class SavePromptDismissals:
    """Process-lifetime (session) dismissals for the save-connection prompt."""

    def __init__(self) -> None:
        self._keys: set[str] = set()

    def dismiss(self, hostname: str, username: str) -> None:
        self._keys.add(identity_key(hostname, username))

    def dismiss_connection(self, connection: Any, *, config_file: Optional[str] = None) -> None:
        host, user = resolve_connection_host_user(connection, config_file=config_file)
        self.dismiss(host, user)

    def is_dismissed(self, hostname: str, username: str) -> bool:
        return identity_key(hostname, username) in self._keys

    def is_connection_dismissed(
        self, connection: Any, *, config_file: Optional[str] = None,
    ) -> bool:
        host, user = resolve_connection_host_user(connection, config_file=config_file)
        return self.is_dismissed(host, user)

    def clear(self) -> None:
        self._keys.clear()
