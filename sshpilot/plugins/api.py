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
# 1.2: Event system (ctx.events), UI extension (ctx.ui), terminal control
#      (ctx.open_connection), key generation (ctx.generate_key), per-plugin
#      scoped ctx.secrets/ctx.settings, ctx.run_on_ui_thread, ctx.plugin_id.
# 1.3: Connection groups — ctx.create_group / add_connection_to_group /
#      add_connection_group (for multi-node provisioning).
# 1.4: ctx.list_connections() — read-only snapshot of all saved connections.
API_VERSION: Tuple[int, int] = (1, 4)

# Stable event names and event payload types live in host.py; re-exported here
# so plugins import everything from sshpilot.plugins.api. (host.py imports
# nothing from this module, so there is no import cycle.)
from .host import ConnectionInfo, Events, SessionInfo  # noqa: E402,F401


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
    working_directory: Optional[str] = None  # spawn cwd; defaults to ~ if None
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

        Must be **stateless per connection**: derive everything from
        ``connection.data`` and the environment. ``ctx`` here is a host-less
        spawn context (see ``PluginContext.for_spawn``) — ``ctx.secrets`` and
        ``ctx.settings`` are available and correctly scoped, but ``ctx.ui`` and
        ``ctx.events`` are ``None`` and must not be used.
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


class _EventsFacade:
    """Per-plugin view of the event bus (``ctx.events``). Subscriptions are
    tagged with the plugin id so they can be cleaned up on deactivate."""

    # Event-name constants, mirrored for convenience (ctx.events.APP_STARTED).
    APP_STARTED = Events.APP_STARTED
    APP_SHUTDOWN = Events.APP_SHUTDOWN
    CONNECTION_CREATED = Events.CONNECTION_CREATED
    CONNECTION_UPDATED = Events.CONNECTION_UPDATED
    CONNECTION_DELETED = Events.CONNECTION_DELETED
    SESSION_OPENED = Events.SESSION_OPENED
    SESSION_CLOSED = Events.SESSION_CLOSED

    def __init__(self, bus: Any, plugin_id: str):
        self._bus = bus
        self._plugin_id = plugin_id

    def subscribe(self, event: str, callback: Callable[[Any], None]) -> None:
        self._bus.subscribe(event, callback, plugin_id=self._plugin_id)

    def unsubscribe(self, event: str, callback: Callable[[Any], None]) -> None:
        self._bus.unsubscribe(event, callback, plugin_id=self._plugin_id)


class _UiFacade:
    """Per-plugin UI surface (``ctx.ui``). Page ids are namespaced by plugin
    so two plugins can both register a page called "deploy"."""

    def __init__(self, ui_host: Any, plugin_id: str):
        self._ui = ui_host
        self._plugin_id = plugin_id

    def _full(self, page_id: str) -> str:
        return f"{self._plugin_id}:{page_id}"

    def register_page(self, page_id: str, title: str, icon_name: str,
                      factory: Callable[[], Any]) -> None:
        """Register a page. ``factory`` is a zero-arg callable returning a
        ``Gtk.Widget`` built on demand when the page is first opened. Safe to
        call from ``activate``; the page appears under the Tools menu once the
        window is ready."""
        self._ui.register_page(self._full(page_id), title, icon_name, factory,
                               plugin_id=self._plugin_id)

    def open_page(self, page_id: str) -> None:
        """Open (or focus) a registered page as a tab. Valid after
        ``app_started``; calls made earlier are queued."""
        self._ui.open_page(self._full(page_id))

    def notify(self, message: str, timeout: int = 3) -> None:
        """Show a transient in-app toast. Valid after ``app_started``;
        earlier calls are queued."""
        self._ui.notify(message, timeout)


class _SecretStore:
    """Per-plugin keyring access (``ctx.secrets``), auto-scoped by plugin id."""

    def __init__(self, connection_manager: Any, plugin_id: str):
        self._cm = connection_manager
        self._plugin_id = plugin_id

    @staticmethod
    def _check_key(key: str) -> None:
        if not key:
            raise ValueError("Secret key must be non-empty")

    def get(self, key: str) -> Optional[str]:
        self._check_key(key)
        return self._cm.get_plugin_secret(self._plugin_id, key)

    def set(self, key: str, value: str) -> None:
        self._check_key(key)
        if not self._cm.store_plugin_secret(self._plugin_id, key, value):
            raise RuntimeError("No secure storage backend available")

    def delete(self, key: str) -> bool:
        self._check_key(key)
        return bool(self._cm.delete_plugin_secret(self._plugin_id, key))


class _SettingStore:
    """Per-plugin config access (``ctx.settings``), namespaced under
    ``plugins.<plugin_id>.<key>`` in the app config. For non-secret data;
    use ``ctx.secrets`` for credentials."""

    def __init__(self, app_config: Any, plugin_id: str):
        self._config = app_config
        self._plugin_id = plugin_id

    def _full(self, key: str) -> str:
        return f"plugins.{self._plugin_id}.{key}"

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get_setting(self._full(key), default)

    def set(self, key: str, value: Any) -> None:
        self._config.set_setting(self._full(key), value)


class PluginContext:
    """Capabilities handed to a plugin on activation.

    Deliberately narrow: plugins get *named* abilities, never raw access
    to windows, the keyring, or private modules. Grow this by adding
    methods, not by exposing internals.

    One context is created per plugin, so ``plugin_id`` is known and the
    ``secrets``/``settings`` accessors are auto-scoped.

    Lifecycle: ``activate(ctx)`` is for registration only — register
    protocols/pages, subscribe to events, read settings. Live UI, terminal,
    and key-generation calls (``ui.open_page``/``ui.notify``,
    ``open_connection``, ``generate_key``) are valid only after the
    ``app_started`` event; calls made earlier are safely queued where possible.
    """

    def __init__(self, *, plugin_id: str, app_config: Any,
                 connection_manager: Any, protocol_registry: Any,
                 host: Any = None):
        self.plugin_id = plugin_id
        # Kept as live attributes: the built-in SSH backend reads them in
        # build_spawn(). Treat as an advanced escape hatch in plugins.
        self.config = app_config
        self.connection_manager = connection_manager
        self._protocols = protocol_registry
        self._host = host

        self.events = _EventsFacade(host.events, plugin_id) if host is not None else None
        self.ui = _UiFacade(host.ui, plugin_id) if host is not None else None
        self.secrets = _SecretStore(connection_manager, plugin_id)
        self.settings = _SettingStore(app_config, plugin_id)

    @classmethod
    def for_spawn(cls, *, plugin_id: str, app_config: Any,
                  connection_manager: Any, protocol_registry: Any) -> "PluginContext":
        """Build a host-less context for ProtocolBackend.build_spawn().

        Created by the terminal at spawn time, scoped to the plugin that
        registered the protocol. ``events``/``ui`` are None (build_spawn must
        not touch them); ``secrets``/``settings`` work and are correctly
        scoped, so a backend may read its own stored config when assembling
        argv/env."""
        return cls(plugin_id=plugin_id, app_config=app_config,
                   connection_manager=connection_manager,
                   protocol_registry=protocol_registry, host=None)

    # --- registration -------------------------------------------------
    def register_protocol(self, backend: ProtocolBackend) -> None:
        self._protocols.register(backend, plugin_id=self.plugin_id)

    # --- connections --------------------------------------------------
    def add_connection(self, data: Dict[str, Any]) -> "ConnectionInfo":
        """Create and persist a new connection (used by provider plugins,
        e.g. VPS provisioning). Returns a read-only ``ConnectionInfo``; raises
        ValueError if the data is invalid (including a duplicate nickname)."""
        conn = self.connection_manager.add_connection_from_data(data)
        return ConnectionInfo.from_connection(conn)

    def update_connection(self, nickname: str, data: Dict[str, Any]) -> bool:
        """Update an existing connection (by nickname) in place — rewrites its
        stored settings and re-stores its password. Returns False if no
        connection with that nickname exists. Use together with
        ``add_connection`` to refresh a provisioned host whose address or
        credentials changed (e.g. a workspace was stopped and restarted)."""
        conn = self.connection_manager.find_connection_by_nickname(nickname)
        if conn is None:
            return False
        return bool(self.connection_manager.update_connection(conn, dict(data)))

    def list_connections(self) -> List["ConnectionInfo"]:
        """Return a read-only snapshot of every saved connection as
        ``ConnectionInfo`` objects. Safe to call any time after load (it reads
        the persisted list, not live UI). Use it to drive a dashboard, an
        export, or a 'apply to existing' action; subscribe to the
        ``connection_*`` events to keep a derived view fresh."""
        conns = getattr(self.connection_manager, "connections", None) or []
        return [ConnectionInfo.from_connection(c) for c in conns]

    def open_connection(self, nickname: str) -> bool:
        """Open a terminal tab for an existing connection (by nickname).
        Returns False if unknown or the UI isn't ready. Valid after
        ``app_started``."""
        return self._host.open_connection(nickname) if self._host is not None else False

    # --- groups -------------------------------------------------------
    def create_group(self, name: str, color: Optional[str] = None) -> Optional[str]:
        """Find-or-create a sidebar group by display name; return its id
        (None if the UI isn't ready). Idempotent — safe to call on every
        provision. Valid after ``app_started``."""
        return self._host.ensure_group(name, color) if self._host is not None else None

    def add_connection_to_group(self, nickname: str, group_id: str) -> bool:
        """Add an existing connection to a group and refresh the sidebar.
        Returns False if unknown / UI not ready. Valid after ``app_started``."""
        return (self._host.add_connection_to_group(nickname, group_id)
                if self._host is not None else False)

    def add_connection_group(self, group_name: str,
                             connections: List[Dict[str, Any]],
                             color: Optional[str] = None):
        """Convenience for multi-node provisioning: ensure a group, create
        each connection (reusing any that already exist by nickname), assign
        them to the group, and refresh the sidebar ONCE. Returns
        ``(group_id, [ConnectionInfo])`` (group_id is None if the UI isn't
        ready). Valid after ``app_started``."""
        group_id = self.create_group(group_name, color)
        infos: List["ConnectionInfo"] = []
        for data in connections:
            nickname = (data.get("nickname") or data.get("host") or "").strip()
            try:
                infos.append(self.add_connection(data))
            except ValueError:
                existing = self.connection_manager.find_connection_by_nickname(nickname)
                if existing is not None:
                    infos.append(ConnectionInfo.from_connection(existing))
            if group_id and nickname and self._host is not None:
                self._host.add_connection_to_group(nickname, group_id, rebuild=False)
        if group_id and self._host is not None:
            self._host.rebuild_sidebar()
        return group_id, infos

    # --- keys ---------------------------------------------------------
    def generate_key(self, name: str, **kwargs) -> Optional[str]:
        """Generate an SSH key via the app's key manager and return its
        private-key path (None on failure). Accepts the same keyword args as
        the key manager (``key_type``, ``key_size``, ``comment``,
        ``passphrase``). Valid after ``app_started``."""
        return self._host.generate_key(name, **kwargs) if self._host is not None else None

    # --- threading ----------------------------------------------------
    def run_on_ui_thread(self, fn: Callable, *args) -> None:
        """Schedule ``fn(*args)`` to run on the GTK main thread. Use this to
        return to the UI from a background worker (do network/provisioning
        work off-thread, then marshal UI/connection calls back here)."""
        if self._host is not None:
            self._host.run_on_ui_thread(fn, *args)
        else:
            fn(*args)

    # --- secrets (legacy explicit-id API; prefer ctx.secrets) ----------
    def _check_secret_args(self, plugin_id: str, key: str) -> None:
        if not plugin_id or '/' in plugin_id:
            raise ValueError(f"Invalid plugin id {plugin_id!r}")
        # A plugin may only touch its own namespace — the explicit id can't be
        # used to read/write another plugin's secrets. Use the scoped
        # ``ctx.secrets`` (auto-scoped) instead.
        if plugin_id != self.plugin_id:
            raise ValueError(
                "A plugin may only access its own secrets; use ctx.secrets")
        if not key:
            raise ValueError("Secret key must be non-empty")

    def get_secret(self, plugin_id: str, key: str) -> Optional[str]:
        """Namespaced keyring read. Prefer the scoped ``ctx.secrets.get``."""
        self._check_secret_args(plugin_id, key)
        return self.connection_manager.get_plugin_secret(plugin_id, key)

    def set_secret(self, plugin_id: str, key: str, value: str) -> None:
        """Prefer the scoped ``ctx.secrets.set``."""
        self._check_secret_args(plugin_id, key)
        if not self.connection_manager.store_plugin_secret(plugin_id, key, value):
            raise RuntimeError("No secure storage backend available")

    def delete_secret(self, plugin_id: str, key: str) -> bool:
        """Prefer the scoped ``ctx.secrets.delete``."""
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
