"""Core plugin contracts (headless; no widgets).

GTK-facing contributions live in ``sshpilot.gtk.plugins``. Existing plugins
should keep importing from ``sshpilot.plugins.api`` (compatibility shim).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# Keep in sync with sshpilot.plugins.api.API_VERSION
API_VERSION: Tuple[int, int] = (1, 13)


class Capability(enum.Enum):
    """Things a protocol backend can do."""

    FILE_TRANSFER = "file-transfer"
    PORT_FORWARDING = "port-forwarding"
    REMOTE_COMMAND = "remote-command"
    AUTH_PASSWORD = "auth-password"
    AUTH_KEY = "auth-key"
    KEY_DEPLOYMENT = "key-deployment"
    AGENT = "ssh-agent"
    JUMP_HOST = "jump-host"


@dataclass
class SpawnSpec:
    """Ready-to-spawn command for a terminal / process host."""

    argv: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    working_directory: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FieldSpec:
    """Declarative connection-editor field (no widget binding)."""

    key: str
    label: str
    kind: str = "text"  # text | int | password | file | choice | switch
    default: Any = None
    choices: Optional[List[Tuple[str, str]]] = None  # (value, label)
    placeholder: str = ""
    required: bool = False
    group: str = "general"


class Events:
    """Stable event-name constants."""

    APP_STARTED = "app_started"
    APP_SHUTDOWN = "app_shutdown"
    CONNECTION_CREATED = "connection_created"
    CONNECTION_UPDATED = "connection_updated"
    CONNECTION_DELETED = "connection_deleted"
    SESSION_OPENED = "session_opened"
    SESSION_CLOSED = "session_closed"


ALL_EVENTS = frozenset({
    Events.APP_STARTED,
    Events.APP_SHUTDOWN,
    Events.CONNECTION_CREATED,
    Events.CONNECTION_UPDATED,
    Events.CONNECTION_DELETED,
    Events.SESSION_OPENED,
    Events.SESSION_CLOSED,
})


@dataclass(frozen=True)
class ConnectionInfo:
    """Read-only connection snapshot for plugin events."""

    nickname: str
    host: str
    username: str
    protocol: str
    port: int

    @classmethod
    def from_connection(cls, conn: Any) -> "ConnectionInfo":
        return cls(
            nickname=getattr(conn, "nickname", "") or "",
            host=(getattr(conn, "hostname", "") or getattr(conn, "host", "") or ""),
            username=getattr(conn, "username", "") or "",
            protocol=getattr(conn, "protocol", "ssh") or "ssh",
            port=int(getattr(conn, "port", 22) or 22),
        )


@dataclass(frozen=True)
class SessionInfo:
    """Read-only snapshot of an open terminal session."""

    connection: ConnectionInfo
    session_id: str


class EventBus:
    """Synchronous event dispatcher with per-callback isolation."""

    def __init__(self) -> None:
        self._subs: Dict[str, List[Tuple[str, Callable[[Any], None]]]] = {}

    def subscribe(
        self,
        event: str,
        callback: Callable[[Any], None],
        *,
        plugin_id: str,
    ) -> None:
        if event not in ALL_EVENTS:
            raise ValueError(
                f"Unknown event {event!r}; valid events: {sorted(ALL_EVENTS)}"
            )
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._subs.setdefault(event, []).append((plugin_id, callback))

    def unsubscribe(
        self,
        event: str,
        callback: Callable[[Any], None],
        *,
        plugin_id: str,
    ) -> None:
        subs = self._subs.get(event)
        if not subs:
            return
        self._subs[event] = [
            (pid, cb)
            for (pid, cb) in subs
            if not (pid == plugin_id and cb == callback)
        ]

    def unsubscribe_plugin(self, plugin_id: str) -> None:
        for event in list(self._subs):
            self._subs[event] = [
                (pid, cb) for (pid, cb) in self._subs[event] if pid != plugin_id
            ]

    def emit(self, event: str, payload: Any) -> None:
        import logging

        log = logging.getLogger(__name__)
        for plugin_id, callback in list(self._subs.get(event, ())):
            try:
                callback(payload)
            except Exception:
                log.exception(
                    "Plugin %r callback for event %r failed", plugin_id, event
                )
