"""Frontend/runtime-compatible connection value objects.

Saved connection authority lives in the daemon API.  These objects are only
ephemeral projections used by older terminal and presentation code.
"""

from __future__ import annotations

import asyncio
import enum
from typing import Any, Dict, List


class ConnectionState(enum.Enum):
    UNKNOWN = "unknown"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"


class Connection:
    """Ephemeral connection projection; it has no persistence or I/O methods."""

    def __init__(self, data: Dict[str, Any]):
        self.data = dict(data or {})
        self._status = ConnectionState.UNKNOWN
        self._status_reason = ""
        self.connection = None
        self.forwarders: List[asyncio.Task] = []
        self.listeners = []
        self.generation = int(self.data.get("generation", 0) or 0)
        self._apply_data(self.data)
        self._connection_manager = None

    def _apply_data(self, data: Dict[str, Any]) -> None:
        self.id = str(data.get("id") or data.get("nickname") or "")
        self.ssh_alias = str(data.get("ssh_alias") or data.get("nickname") or self.id)
        self.display_name = str(data.get("display_name") or data.get("nickname") or self.ssh_alias)
        self.identity_status = str(data.get("identity_status") or "resolved")
        self.nickname = self.ssh_alias
        self.host = str(data.get("host") or self.nickname)
        self.hostname = str(data.get("hostname") or "")
        self.username = str(data.get("username") or "")
        self.port = int(data.get("port") or 22)
        self.protocol = str(data.get("protocol") or "ssh")
        self.aliases = tuple(data.get("aliases") or ())
        self.source = str(data.get("source") or "")
        self.isolated_mode = bool(data.get("isolated_mode", False))

    def update_data(self, data: Dict[str, Any]) -> None:
        self.data.update(data)
        self._apply_data(self.data)

    def __str__(self) -> str:
        return f"{self.display_name} ({self.username}@{self.hostname})"

    def get_effective_host(self) -> str:
        return self.hostname or self.host or self.nickname

    def resolve_host_identifier(self) -> str:
        return self.host or self.nickname or self.hostname

    def get_status(self) -> ConnectionState:
        return self._status

    def get_status_reason(self) -> str:
        return self._status_reason

    def set_status(self, state: ConnectionState, reason: str = "") -> None:
        self._status = state
        self._status_reason = reason or ""

    @property
    def is_connected(self) -> bool:
        return self._status is ConnectionState.CONNECTED

    @is_connected.setter
    def is_connected(self, value: bool) -> None:
        # A plain is_connected=False (e.g. generic cleanup) must not erase a
        # richer FAILED state (and its reason) already recorded.
        if value:
            self.set_status(ConnectionState.CONNECTED)
        elif self._status is not ConnectionState.FAILED:
            self.set_status(ConnectionState.DISCONNECTED)
