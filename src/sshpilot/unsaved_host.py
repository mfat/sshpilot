"""Frontend-only state for the post-connect save prompt.

The daemon owns destination matching.  This module deliberately contains no
SSH config access, connection-manager access, or secret/persistence behavior.
"""

from __future__ import annotations

from typing import Any, Tuple


def _as_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        for item in reversed(value):
            text = str(item or "").strip()
            if text:
                return text
        return ""
    return str(value).strip()


def connection_destination(connection: Any) -> Tuple[str, str]:
    """Return semantic destination fields from an ephemeral projection."""
    hostname = _as_scalar(getattr(connection, "hostname", None))
    username = _as_scalar(getattr(connection, "username", None))
    if not hostname:
        hostname = (
            _as_scalar(getattr(connection, "host", None))
            or _as_scalar(getattr(connection, "nickname", None))
        )
    return hostname.lower(), username


def identity_key(hostname: str, username: str) -> str:
    """Stable key for session-scoped dismissals."""
    return f"{(hostname or '').lower()}|{(username or '')}"


class SavePromptDismissals:
    """Process-lifetime (session) dismissals for the save-connection prompt."""

    def __init__(self) -> None:
        self._keys: set[str] = set()

    def dismiss(self, hostname: str, username: str) -> None:
        self._keys.add(identity_key(hostname, username))

    def dismiss_connection(self, connection: Any) -> None:
        host, user = connection_destination(connection)
        self.dismiss(host, user)

    def is_dismissed(self, hostname: str, username: str) -> bool:
        return identity_key(hostname, username) in self._keys

    def is_connection_dismissed(self, connection: Any) -> bool:
        host, user = connection_destination(connection)
        return self.is_dismissed(host, user)

    def clear(self) -> None:
        self._keys.clear()
