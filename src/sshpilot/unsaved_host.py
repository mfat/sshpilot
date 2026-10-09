"""Frontend-only state for the post-connect save prompt.

The daemon owns destination matching.  This module deliberately contains no
SSH config access, connection-manager access, or secret/persistence behavior.
"""

from __future__ import annotations

from typing import Any, Iterable, Tuple


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


def _protocol(connection: Any) -> str:
    return (_as_scalar(getattr(connection, "protocol", None)) or "ssh").casefold()


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


def connection_target(
    connection: Any, required_keys: Iterable[str]
) -> Tuple[Tuple[str, str], ...]:
    """The fields naming a hostless protocol's target, for the daemon check.

    ``required_keys`` are the protocol's required fields (serial's ``device``,
    a container's ``container``). A protocol with a host field is matched on
    host, user and port instead, so it gets no target.
    """
    keys = [key for key in required_keys if key]
    if not keys or "host" in keys or "hostname" in keys:
        return ()
    data = getattr(connection, "data", None) or {}
    return tuple(
        (key, _as_scalar(data.get(key, getattr(connection, key, None))))
        for key in sorted(keys)
    )


def identity_key(hostname: str, username: str, protocol: str = "ssh") -> str:
    """Stable key for session-scoped dismissals."""
    key = f"{(hostname or '').lower()}|{(username or '')}"
    protocol = (protocol or "ssh").casefold()
    return key if protocol == "ssh" else f"{protocol}:{key}"


class SavePromptDismissals:
    """Process-lifetime (session) dismissals for the save-connection prompt."""

    def __init__(self) -> None:
        self._keys: set[str] = set()

    def dismiss(self, hostname: str, username: str, protocol: str = "ssh") -> None:
        self._keys.add(identity_key(hostname, username, protocol))

    def dismiss_connection(self, connection: Any) -> None:
        host, user = connection_destination(connection)
        self.dismiss(host, user, _protocol(connection))

    def is_dismissed(self, hostname: str, username: str, protocol: str = "ssh") -> bool:
        return identity_key(hostname, username, protocol) in self._keys

    def is_connection_dismissed(self, connection: Any) -> bool:
        host, user = connection_destination(connection)
        return self.is_dismissed(host, user, _protocol(connection))

    def clear(self) -> None:
        self._keys.clear()
