"""sshPilot plugin API.

This module is the ONLY supported import surface for plugins:

    from sshpilot.plugins.api import (
        SshPilotPlugin, PluginContext, ProtocolBackend,
        SpawnSpec, FieldSpec, Capability, API_VERSION,
    )

Everything else under ``sshpilot.*`` is private and may change without
notice. Plugins declare the API major version they target in their
``plugin.json``; the loader refuses to activate plugins whose major
version does not match ``API_VERSION[0]``.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# (major, minor). Bump minor for additive changes, major for breaking ones.
# 1.1: PluginContext.delete_secret; get_secret/set_secret wired to the keyring.
API_VERSION: Tuple[int, int] = (1, 1)


class Capability(enum.Enum):
    """Things a protocol backend can do. UI code uses these to show/hide
    actions (SFTP button, port-forwarding page, ssh-copy-id, ...) instead
    of checking ``protocol == 'ssh'``."""

    FILE_TRANSFER = "file-transfer"          # SFTP/SCP-style browsing
    PORT_FORWARDING = "port-forwarding"
    REMOTE_COMMAND = "remote-command"
    AUTH_PASSWORD = "auth-password"
    AUTH_KEY = "auth-key"
    KEY_DEPLOYMENT = "key-deployment"        # ssh-copy-id flow
    AGENT = "ssh-agent"
    JUMP_HOST = "jump-host"


@dataclass
class SpawnSpec:
    """A fully prepared, ready-to-spawn command for a terminal tab.

    This is the generalized successor of ``SSHConnectionCommand``: argv +
    env is all the terminal layer fundamentally needs. ``extras`` carries
    protocol-private hints during the migration period (e.g. the SSH
    backend's sshpass/askpass flags, which terminal.py still orchestrates);
    new protocols should NOT rely on extras being interpreted by core.
    """

    argv: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    working_directory: Optional[str] = None
    # Called with the child PID after a successful spawn (e.g. to start a
    # watchdog), and on child exit (for cleanup of FIFOs, temp files, ...).
    on_spawned: Optional[Callable[[int], None]] = None
    on_exited: Optional[Callable[[int], None]] = None
    # Transitional, protocol-private data. See docstring.
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FieldSpec:
    """Declarative description of one connection-editor field.

    Phase 2: the connection dialog renders these for non-SSH protocols
    instead of hardcoding rows. ``key`` is where the value lives in
    ``Connection.data``.
    """

    key: str
    label: str
    kind: str = "text"  # text | int | password | file | choice | switch
    default: Any = None
    choices: Optional[List[Tuple[str, str]]] = None  # (value, label)
    placeholder: str = ""
    required: bool = False
    group: str = "general"  # dialog section the row belongs to


class ProtocolBackend(abc.ABC):
    """A connection protocol (ssh, telnet, serial, mosh, ...).

    One instance per protocol is registered at plugin activation and
    shared across all connections; backends must therefore be stateless
    with respect to individual connections (state belongs to the spawned
    process / the SpawnSpec callbacks).
    """

    #: Stable identifier stored in Connection.data["protocol"].
    protocol_id: str = ""
    #: Human-readable name for the connection dialog's protocol selector.
    display_name: str = ""
    default_port: Optional[int] = None

    @abc.abstractmethod
    def capabilities(self) -> frozenset:
        """Return a frozenset of Capability members."""

    @abc.abstractmethod
    def build_spawn(self, connection: Any, ctx: "PluginContext") -> SpawnSpec:
        """Turn a Connection into something the terminal can spawn.

        Must not block on network I/O: anything slow belongs in the child
        process itself. May raise ``ProtocolError`` with a user-presentable
        message.
        """

    def connection_fields(self) -> List[FieldSpec]:
        """Fields the connection editor should show for this protocol.

        Default: empty (protocol uses only the shared host/nickname
        fields). The built-in SSH backend also returns [] because the
        existing hand-built dialog remains authoritative for SSH.
        """
        return []

    def validate(self, data: Dict[str, Any]) -> List[str]:
        """Return a list of human-readable validation errors (empty = ok)."""
        return []


class ProtocolError(RuntimeError):
    """Raised by build_spawn(); message is shown to the user."""


class PluginContext:
    """Capabilities handed to a plugin on activation.

    Deliberately narrow: plugins get *named* abilities, never raw access
    to windows, the keyring, or private modules. Grow this by adding
    methods, not by exposing internals.
    """

    def __init__(self, *, app_config: Any, connection_manager: Any,
                 protocol_registry: Any):
        self.config = app_config
        self.connection_manager = connection_manager
        self._protocols = protocol_registry

    # --- registration -------------------------------------------------
    def register_protocol(self, backend: ProtocolBackend) -> None:
        self._protocols.register(backend)

    # --- data access (narrow, stable wrappers) -------------------------
    def add_connection(self, data: Dict[str, Any]) -> Any:
        """Create and persist a new connection (used by provider plugins,
        e.g. VPS provisioning). Returns the Connection object; raises
        ValueError if the data is invalid."""
        return self.connection_manager.add_connection_from_data(data)

    @staticmethod
    def _check_secret_args(plugin_id: str, key: str) -> None:
        if not plugin_id or '/' in plugin_id:
            raise ValueError(f"Invalid plugin id {plugin_id!r}")
        if not key:
            raise ValueError("Secret key must be non-empty")

    def get_secret(self, plugin_id: str, key: str) -> Optional[str]:
        """Namespaced keyring read: secrets live under sshpilot-plugin/<id>."""
        self._check_secret_args(plugin_id, key)
        return self.connection_manager.get_plugin_secret(plugin_id, key)

    def set_secret(self, plugin_id: str, key: str, value: str) -> None:
        self._check_secret_args(plugin_id, key)
        if not self.connection_manager.store_plugin_secret(plugin_id, key, value):
            raise RuntimeError("No secure storage backend available")

    def delete_secret(self, plugin_id: str, key: str) -> bool:
        """Added in API 1.1."""
        self._check_secret_args(plugin_id, key)
        return bool(self.connection_manager.delete_plugin_secret(plugin_id, key))


class SshPilotPlugin(abc.ABC):
    """Base class every plugin's entry point must subclass.

    The loader instantiates it with no arguments, then calls
    ``activate(ctx)`` once. ``deactivate()`` is best-effort (app quit or
    plugin disabled in preferences).
    """

    @abc.abstractmethod
    def activate(self, ctx: PluginContext) -> None: ...

    def deactivate(self) -> None:  # noqa: B027 (intentional no-op default)
        pass
