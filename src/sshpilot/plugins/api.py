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
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# (major, minor). Bump minor for additive changes, major for breaking ones.
# 1.1: PluginContext.delete_secret; get_secret/set_secret wired to the keyring.
# 1.2: Event system (ctx.events), UI extension (ctx.ui), terminal control
#      (ctx.open_connection), key generation (ctx.generate_key), per-plugin
#      scoped ctx.secrets/ctx.settings, ctx.run_on_ui_thread, ctx.plugin_id.
# 1.3: Connection groups — ctx.create_group / add_connection_to_group /
#      add_connection_group (for multi-node provisioning); ctx.update_connection
#      (refresh an existing connection in place).
# 1.4: ctx.list_connections() — read-only snapshot of all saved connections.
# 1.5: power APIs — ctx.run_command (one-shot remote command via the native
#      SSH/auth path), ctx.get_effective_ssh_config, ctx.copy_key_to_host,
#      ctx.list_keys/ctx.delete_key, terminal/session access
#      (ctx.list_sessions/ctx.read_terminal/ctx.send_terminal), and
#      self-contained ctx.data_dir/ctx.files/ctx.http helpers.
# 1.6: ctx.open_command_terminal(nickname, remote_command, title=) — open a new
#      terminal tab running a one-off command on a connection's host, over the
#      single native SSH/auth path (no new transport). For streamed output such
#      as `docker logs -f`, interactive `docker exec -it`, or `top`.
# 1.7: ctx.ui.register_connection_action(action_id, label, icon_name, callback)
#      — contribute an item to the connection-list right-click menu; callback
#      receives the connection nickname.
# 1.8: ctx.ui.register_page(..., add_menu_item=False, on_activate=cb) — register
#      a page with no Tools-menu entry (opened directly, e.g. one tab per host),
#      and/or have the menu entry run a callback instead of opening the page.
# 1.9: ctx.acquire_multiplex(nickname) / ctx.release_multiplex(nickname) — keep a
#      shared SSH ControlMaster warm for a host while a surface is open; run_command
#      then transparently reuses that one connection (no re-auth per call). For
#      polling surfaces such as the Docker Console.
# 1.10: ctx.identities — read-only view of SSH identities from the configured
#      identity providers (ctx.identities.list() / .is_agent_available()),
#      paralleling ctx.secrets. See sshpilot.identity / docs/IDENTITY_PROVIDERS.md.
# 1.11: ctx.run_local_command / ctx.open_local_command_terminal — captured and
#      streamed/interactive commands on the local machine (Flatpak-host aware).
# 1.12: ctx.ensure_local_forward(nickname, remote_port) — local port forwarded
#      to the host over the single SSH/auth path (ControlMaster -O forward,
#      background ssh -N fallback); ctx.ui.open_web_tab(url, title=) — show a
#      URL in an embedded WebKit tab (system-browser fallback).
# 1.13: ctx.run_command_stream / ctx.run_local_command_stream — long-lived
#      line-oriented streams (e.g. `docker logs -f`, `docker events`) over the
#      same native SSH/local paths as the one-shot command APIs; returns a
#      StreamHandle the caller stops when done.
API_VERSION: Tuple[int, int] = (1, 13)

# Stable event names and event payload types live in host.py; re-exported here
# so plugins import everything from sshpilot.plugins.api. (host.py imports
# nothing from this module, so there is no import cycle.)
from .host import ConnectionInfo, Events, SessionInfo  # noqa: E402,F401

# Re-exported so plugins can type/inspect ctx.identities.list() results without
# reaching into a private module. (identity.py imports nothing from plugins.)
from ..identity import Identity  # noqa: E402,F401


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
                      factory: Callable[[], Any], *,
                      add_menu_item: bool = True,
                      on_activate: Optional[Callable[[], None]] = None) -> None:
        """Register a page. ``factory`` is a zero-arg callable returning a
        ``Gtk.Widget`` built on demand when the page is first opened. Safe to
        call from ``activate``; the page appears under the Tools menu once the
        window is ready.

        ``add_menu_item=False`` (API >= 1.8) registers the page without a Tools-menu
        entry — for pages opened directly via ``open_page`` (e.g. one tab per
        host). ``on_activate`` (API >= 1.8), when given, is called when the
        Tools-menu item is chosen instead of opening this page — e.g. to open a
        different (per-host) page."""
        self._ui.register_page(self._full(page_id), title, icon_name, factory,
                               plugin_id=self._plugin_id,
                               add_menu_item=add_menu_item, on_activate=on_activate)

    def open_page(self, page_id: str) -> None:
        """Open (or focus) a registered page as a tab. Valid after
        ``app_started``; calls made earlier are queued."""
        self._ui.open_page(self._full(page_id))

    def notify(self, message: str, timeout: int = 3) -> None:
        """Show a transient in-app toast. Valid after ``app_started``;
        earlier calls are queued."""
        self._ui.notify(message, timeout)

    def register_connection_action(self, action_id: str, label: str,
                                   icon_name: str,
                                   callback: Callable[[str], None]) -> None:
        """Add an item to the connection-list right-click menu (API >= 1.7).

        ``callback`` is invoked with the right-clicked connection's nickname.
        Safe to call from ``activate``; the item appears for SSH connections
        once the window is ready. Use it to open a plugin page targeting that
        host (e.g. ``ctx.ui.open_page(...)`` then point the page at the host)."""
        register = getattr(self._ui, "register_connection_action", None)
        if callable(register):
            register(action_id, label, icon_name, callback, plugin_id=self._plugin_id)

    def open_web_tab(self, url: str, *, title: Optional[str] = None) -> bool:
        """Show ``url`` in an embedded WebKit tab (API >= 1.12), falling back
        to the system default browser when WebKit is unavailable. Returns True
        when something opened. Valid after ``app_started``."""
        opener = getattr(self._ui, "open_web_tab", None)
        if callable(opener):
            return bool(opener(url, title or url))
        return False


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


class _IdentityView:
    """Read-only view of SSH identities (``ctx.identities``).

    Lists the identities the configured identity providers currently expose (e.g.
    keys loaded in the system ssh-agent), paralleling the credential side. This is
    observation only — choosing/configuring providers is the user's job, not the
    plugin's.
    """

    def list(self) -> List["Identity"]:
        """All identities across currently-available providers."""
        from ..identity import get_identity_manager

        return get_identity_manager().list_identities()

    def is_agent_available(self) -> bool:
        """Whether the system ssh-agent is reachable right now."""
        from ..identity import get_identity_manager

        return get_identity_manager().system_agent().is_available()


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


@dataclass
class CommandResult:
    """Outcome of ``ctx.run_command``: the remote command's exit code and its
    captured output. ``exit_code`` is -1 when the command could not be run at
    all (unknown connection, ssh missing, timeout)."""
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class StreamHandle:
    """Handle for a long-lived command started by ``run_command_stream`` /
    ``run_local_command_stream``. Call :meth:`stop` to terminate the process
    and join the reader thread. Safe to call ``stop`` more than once."""

    def __init__(self) -> None:
        self._proc: Any = None
        self._thread: Optional[threading.Thread] = None
        self._cleanup: Optional[Callable[[], None]] = None
        self._stopped = False
        self._lock = threading.Lock()

    def _attach(self, proc: Any, thread: threading.Thread,
                cleanup: Optional[Callable[[], None]] = None) -> None:
        self._proc = proc
        self._thread = thread
        self._cleanup = cleanup

    @property
    def running(self) -> bool:
        with self._lock:
            if self._stopped:
                return False
            proc = self._proc
        return proc is not None and proc.poll() is None

    def stop(self) -> None:
        """Terminate the stream process (if still running) and wait for the
        reader thread. Idempotent."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            proc = self._proc
            thread = self._thread
            cleanup = self._cleanup
            self._cleanup = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        if cleanup is not None:
            try:
                cleanup()
            except Exception:  # noqa: BLE001
                pass


@dataclass
class HttpResponse:
    """Result of an ``ctx.http`` call."""
    status: int
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        import json as _json
        return _json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class _FilesFacade:
    """Sandboxed file access (``ctx.files``) rooted at the plugin's own data
    directory (``ctx.data_dir``). All paths are resolved relative to that root
    and escapes (``..`` / absolute paths leaving the root) are rejected, so a
    plugin can persist caches/state without hand-rolling XDG paths."""

    def __init__(self, root_getter: Callable[[], str]):
        self._root_getter = root_getter

    def path(self, rel: str) -> str:
        """Absolute path for ``rel`` inside the plugin data dir (creating the
        dir). Raises ValueError if ``rel`` would escape the data dir."""
        import os
        root = self._root_getter()
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            raise ValueError(f"Path {rel!r} escapes the plugin data directory")
        return full

    def exists(self, rel: str) -> bool:
        import os
        return os.path.exists(self.path(rel))

    def read_text(self, rel: str, encoding: str = "utf-8") -> str:
        with open(self.path(rel), encoding=encoding) as fh:
            return fh.read()

    def read_bytes(self, rel: str) -> bytes:
        with open(self.path(rel), "rb") as fh:
            return fh.read()

    def write_text(self, rel: str, data: str, encoding: str = "utf-8") -> None:
        import os
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding=encoding) as fh:
            fh.write(data)

    def write_bytes(self, rel: str, data: bytes) -> None:
        import os
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)


class _HttpFacade:
    """Minimal blocking HTTP client (``ctx.http``) over urllib so plugins
    don't hand-roll requests. Blocking — call from a worker thread and marshal
    results back with ``ctx.run_on_ui_thread``."""

    def get(self, url: str, *, headers: Optional[Dict[str, str]] = None,
            timeout: float = 30) -> HttpResponse:
        return self._request("GET", url, headers=headers, timeout=timeout)

    def post(self, url: str, *, data: Optional[bytes] = None,
             json: Any = None, headers: Optional[Dict[str, str]] = None,
             timeout: float = 30) -> HttpResponse:
        body = data
        hdrs = dict(headers or {})
        if json is not None:
            import json as _json
            body = _json.dumps(json).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        return self._request("POST", url, data=body, headers=hdrs,
                             timeout=timeout)

    @staticmethod
    def _request(method: str, url: str, *, data: Optional[bytes] = None,
                 headers: Optional[Dict[str, str]] = None,
                 timeout: float = 30) -> HttpResponse:
        import urllib.request
        import urllib.error
        if not str(url).lower().startswith(("http://", "https://")):
            raise ValueError("ctx.http only supports http(s) URLs")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=dict(headers or {}))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = getattr(resp, "status", None)
                if status is None and hasattr(resp, "getcode"):
                    status = resp.getcode()
                return HttpResponse(
                    status=int(status or 0),
                    text=raw.decode("utf-8", "replace"),
                    headers={k: v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            return HttpResponse(status=exc.code,
                                text=raw.decode("utf-8", "replace"),
                                headers={k: v for k, v in (exc.headers or {}).items()})


# --- plugin-requested local port forwards (ctx.ensure_local_forward) ------
# Keyed by (nickname, remote_port). ``proc`` is None for forwards added onto
# the shared ControlMaster with ``ssh -O forward`` (they die with the master);
# fallback forwards own a background ``ssh -N`` process, killed at app exit.
# ponytail: forwards live until app quit / master exit; add `ssh -O cancel`
# per-tab teardown if port hoarding ever matters.
@dataclass
class _Forward:
    local_port: int
    proc: Optional[Any] = None  # subprocess.Popen for the ssh -N fallback


_FORWARDS: Dict[Tuple[str, int], _Forward] = {}
_FORWARDS_LOCK = threading.Lock()
_FORWARDS_ATEXIT = False


def _kill_forward_procs() -> None:
    with _FORWARDS_LOCK:
        procs = [f.proc for f in _FORWARDS.values() if f.proc is not None]
        _FORWARDS.clear()
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass


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
        self.identities = _IdentityView()
        self.settings = _SettingStore(app_config, plugin_id)
        # Self-contained helpers (no host needed) — available even in for_spawn.
        self.files = _FilesFacade(self._data_dir)
        self.http = _HttpFacade()

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

    def open_command_terminal(self, nickname: str, remote_command: str,
                              *, title: Optional[str] = None,
                              pty_prompt: Optional[str] = None,
                              pty_response: Optional[str] = None) -> bool:
        """Open a new terminal tab that runs ``remote_command`` on the host of
        the connection ``nickname`` (API >= 1.6).

        The command runs over the app's single native SSH path (the same
        ``~/.ssh/config`` / ProxyJump / stored-credential handling as a normal
        session) — no new transport is introduced. Use this for streamed or
        interactive output that ``run_command`` (one-shot, captured) cannot
        show, e.g. ``docker logs -f``, ``docker exec -it <c> sh``, or live
        ``docker stats``. ``title`` overrides the tab title.

        ``pty_prompt``/``pty_response`` arm a one-shot auto-fill: the first time
        ``pty_prompt`` appears in the terminal output, ``pty_response`` (plus a
        newline) is typed into the PTY — used to answer a remote ``sudo``
        password prompt without the secret ever touching a command line.

        Returns False if the connection is unknown, the command is empty, or the
        UI isn't ready. Valid after ``app_started``."""
        if self._host is None:
            return False
        return self._host.open_command_terminal(
            nickname, remote_command, title=title,
            pty_prompt=pty_prompt, pty_response=pty_response)

    def open_local_command_terminal(self, command: str, *,
                                    title: Optional[str] = None,
                                    pty_prompt: Optional[str] = None,
                                    pty_response: Optional[str] = None) -> bool:
        """Open a local terminal tab and run ``command`` in its shell (API >= 1.11).

        Use this for streamed or interactive local output. In Flatpak the
        existing local-terminal host bridge is reused, so the command runs on
        the host rather than inside the sandbox. ``pty_prompt`` /
        ``pty_response`` have the same one-shot auto-fill semantics as
        :meth:`open_command_terminal`.
        """
        if self._host is None:
            return False
        return self._host.open_local_command_terminal(
            command, title=title,
            pty_prompt=pty_prompt, pty_response=pty_response)

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

    def list_keys(self) -> List[Dict[str, str]]:
        """List SSH keys known to the app's key manager as
        ``[{"private_path", "public_path"}]``. Empty if the UI isn't ready.
        Valid after ``app_started``."""
        return self._host.list_keys() if self._host is not None else []

    def delete_key(self, private_path: str) -> bool:
        """Delete an SSH key pair (private + ``.pub``) managed by the app. Only
        keys inside the app's key directory may be deleted; returns False
        otherwise or if the UI isn't ready. Valid after ``app_started``."""
        return (self._host.delete_key(private_path)
                if self._host is not None else False)

    # --- remote commands & ssh config ---------------------------------
    def run_command(self, nickname: str, command: str, *,
                    timeout: float = 30,
                    input: Optional[str] = None) -> "CommandResult":
        """Run a one-shot command on a saved connection and capture its output.

        Reuses the app's single SSH/auth path (``build_ssh_connection`` +
        ``resolve_native_auth`` via ``~/.ssh/config``), so ProxyJump, ports,
        identities and stored credentials all apply. **Blocking** — call from a
        worker thread and marshal UI work back via ``run_on_ui_thread``.
        Returns a ``CommandResult`` (``exit_code == -1`` means it could not be
        launched).

        ``input`` is written to the remote command's stdin (e.g. a password for
        ``sudo -S``); the SSH transport itself is non-interactive (no PTY)."""
        import os
        import subprocess
        from ..ssh_connection_builder import (
            ConnectionContext, build_ssh_connection)
        from .. import ssh_multiplex
        conn = self.connection_manager.find_connection_by_nickname(nickname)
        if conn is None:
            logger.debug("run_command(%r): no such connection", nickname)
            return CommandResult(-1, "", f"No connection named {nickname!r}")
        mux = ssh_multiplex.is_active(nickname)
        logger.debug(
            "run_command(%r) mux=%s timeout=%s stdin=%s: %s",
            nickname, mux, timeout, "yes" if input else "no", command,
        )
        cleanup = None
        try:
            # When a surface has acquired multiplexing for this host, reuse its
            # ControlMaster socket: the first call opens the master, the rest ride
            # it (no re-auth). Transparent — callers don't opt in per-call.
            extra_args = (ssh_multiplex.controlmaster_args() if mux else None)
            ctx = ConnectionContext(
                connection=conn, connection_manager=self.connection_manager,
                config=self.config, command_type='ssh',
                remote_command=command, native_mode=True, extra_args=extra_args)
            prepared = build_ssh_connection(ctx)
            argv = list(prepared.command)
            from ..ssh_connection_builder import apply_headless_askpass_env
            # No user-visible TTY — force graphical askpass for secrets/MFA.
            env = apply_headless_askpass_env(
                prepared.env, conn,
                session_password=getattr(prepared, "password", None),
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("run_command(%r) argv: %s", nickname, argv)
            # Capture via temp files — NOT pipes (capture_output=True).
            # ControlPersist backgrounds a master that inherits our stderr pipe;
            # with verbose SSH (-v) that master keeps writing/holding the pipe so
            # subprocess.run never sees EOF and hangs until timeout (~30s). File
            # redirection does not block on the master's lifetime.
            # Also: don't inherit the app's stdin unless the caller fed input
            # (e.g. sudo -S password).
            import tempfile
            with tempfile.TemporaryDirectory(prefix="sshpilot-cmd-") as td:
                out_path = os.path.join(td, "stdout")
                err_path = os.path.join(td, "stderr")
                with open(out_path, "w", encoding="utf-8") as out_f, \
                        open(err_path, "w", encoding="utf-8") as err_f:
                    run_kwargs: dict = dict(
                        env=env, stdout=out_f, stderr=err_f,
                        timeout=timeout, check=False, text=True,
                    )
                    if input is not None:
                        run_kwargs["input"] = input
                    else:
                        run_kwargs["stdin"] = subprocess.DEVNULL
                    completed = subprocess.run(argv, **run_kwargs)
                with open(out_path, encoding="utf-8", errors="replace") as out_f:
                    stdout = out_f.read()
                with open(err_path, encoding="utf-8", errors="replace") as err_f:
                    stderr = err_f.read()
            logger.debug(
                "run_command(%r) exit=%s stdout=%dB stderr=%dB",
                nickname, completed.returncode,
                len(stdout or ""), len(stderr or ""),
            )
            if completed.returncode != 0 and (stderr or stdout):
                logger.debug(
                    "run_command(%r) failure output: %.800s",
                    nickname, (stderr or stdout or "").strip(),
                )
            return CommandResult(completed.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            logger.debug("run_command(%r) timed out after %ss: %s",
                         nickname, timeout, command)
            return CommandResult(-1, "", "Command timed out")
        except Exception as exc:  # noqa: BLE001 — surface as a failed result
            logger.debug("run_command(%r) failed: %s", nickname, exc,
                         exc_info=True)
            return CommandResult(-1, "", str(exc))
        finally:
            if cleanup is not None:
                cleanup()

    def run_local_command(self, command: str, *, timeout: float = 30,
                          input: Optional[str] = None) -> "CommandResult":
        """Run a local shell command and capture its output (API >= 1.11).

        **Blocking** — call from a worker thread. In Flatpak the command is run
        on the host via ``flatpak-spawn --host``. This is a local execution API;
        remote commands must continue to use :meth:`run_command`.
        """
        import os
        import shutil
        import subprocess
        from ..platform_utils import is_flatpak

        if not command or not str(command).strip():
            logger.debug("run_local_command: empty command")
            return CommandResult(-1, "", "Command is empty")
        shell = shutil.which("sh") or "/bin/sh"
        argv = [shell, "-lc", str(command)]
        if is_flatpak():
            spawn = shutil.which("flatpak-spawn")
            if spawn is None:
                return CommandResult(
                    -1, "", "flatpak-spawn is unavailable; cannot run host command")
            argv = [spawn, "--host", "sh", "-lc", str(command)]
        logger.debug(
            "run_local_command timeout=%s stdin=%s: %s",
            timeout, "yes" if input else "no", command,
        )
        try:
            result = subprocess.run(
                argv, env=os.environ.copy(), capture_output=True, text=True,
                timeout=timeout, check=False, input=input)
            logger.debug(
                "run_local_command exit=%s stdout=%dB stderr=%dB",
                result.returncode,
                len(result.stdout or ""), len(result.stderr or ""),
            )
            if result.returncode != 0 and (result.stderr or result.stdout):
                logger.debug(
                    "run_local_command failure output: %.800s",
                    (result.stderr or result.stdout or "").strip(),
                )
            return CommandResult(result.returncode, result.stdout, result.stderr)
        except subprocess.TimeoutExpired:
            logger.debug("run_local_command timed out after %ss: %s",
                         timeout, command)
            return CommandResult(-1, "", "Command timed out")
        except Exception as exc:  # noqa: BLE001 — surface as a failed result
            logger.debug("run_local_command failed: %s", exc, exc_info=True)
            return CommandResult(-1, "", str(exc))

    # --- streaming commands (API >= 1.13) --------------------------------
    def run_command_stream(
            self, nickname: str, command: str, *,
            on_line: Callable[[str], None],
            on_done: Optional[Callable[[int], None]] = None,
            input: Optional[str] = None) -> StreamHandle:
        """Start a long-lived remote command and deliver stdout/stderr lines.

        Reuses the same native SSH/auth path as :meth:`run_command` (including
        ControlMaster when acquired). There is no timeout — the caller must
        :meth:`StreamHandle.stop` when finished (e.g. page unmap, selection
        change). ``on_line`` / ``on_done`` are invoked on the UI thread via
        :meth:`run_on_ui_thread`.

        If the command cannot be started, ``on_done(-1)`` is scheduled and the
        returned handle is already stopped.
        """
        handle = StreamHandle()
        from ..ssh_connection_builder import (
            ConnectionContext, build_ssh_connection)
        from .. import ssh_multiplex
        conn = self.connection_manager.find_connection_by_nickname(nickname)
        if conn is None:
            logger.debug("run_command_stream(%r): no such connection", nickname)
            self._finish_stream_early(handle, on_done, -1)
            return handle
        mux = ssh_multiplex.is_active(nickname)
        logger.debug(
            "run_command_stream(%r) mux=%s stdin=%s: %s",
            nickname, mux, "yes" if input else "no", command,
        )
        cleanup = None
        try:
            extra_args = (ssh_multiplex.controlmaster_args() if mux else None)
            ctx = ConnectionContext(
                connection=conn, connection_manager=self.connection_manager,
                config=self.config, command_type='ssh',
                remote_command=command, native_mode=True, extra_args=extra_args)
            prepared = build_ssh_connection(ctx)
            argv = list(prepared.command)
            from ..ssh_connection_builder import apply_headless_askpass_env
            env = apply_headless_askpass_env(
                prepared.env, conn,
                session_password=getattr(prepared, "password", None),
            )
            self._spawn_stream(
                handle, argv, env, on_line=on_line, on_done=on_done,
                input_text=input, cleanup=cleanup)
        except Exception as exc:  # noqa: BLE001
            logger.debug("run_command_stream(%r) failed: %s", nickname, exc,
                         exc_info=True)
            if cleanup is not None:
                try:
                    cleanup()
                except Exception:  # noqa: BLE001
                    pass
            self._finish_stream_early(handle, on_done, -1)
        return handle

    def run_local_command_stream(
            self, command: str, *,
            on_line: Callable[[str], None],
            on_done: Optional[Callable[[int], None]] = None,
            input: Optional[str] = None) -> StreamHandle:
        """Start a long-lived local shell command and deliver output lines.

        Flatpak-host aware (same as :meth:`run_local_command`). No timeout —
        call :meth:`StreamHandle.stop` when finished. Callbacks run on the UI
        thread.
        """
        handle = StreamHandle()
        import os
        import shutil
        from ..platform_utils import is_flatpak

        if not command or not str(command).strip():
            logger.debug("run_local_command_stream: empty command")
            self._finish_stream_early(handle, on_done, -1)
            return handle
        shell = shutil.which("sh") or "/bin/sh"
        argv = [shell, "-lc", str(command)]
        if is_flatpak():
            spawn = shutil.which("flatpak-spawn")
            if spawn is None:
                self._finish_stream_early(handle, on_done, -1)
                return handle
            argv = [spawn, "--host", "sh", "-lc", str(command)]
        logger.debug(
            "run_local_command_stream stdin=%s: %s",
            "yes" if input else "no", command,
        )
        try:
            self._spawn_stream(
                handle, argv, os.environ.copy(), on_line=on_line,
                on_done=on_done, input_text=input)
        except Exception as exc:  # noqa: BLE001
            logger.debug("run_local_command_stream failed: %s", exc,
                         exc_info=True)
            self._finish_stream_early(handle, on_done, -1)
        return handle

    def _finish_stream_early(
            self, handle: StreamHandle,
            on_done: Optional[Callable[[int], None]],
            exit_code: int) -> None:
        handle.stop()  # mark stopped before any reader starts
        if on_done is not None:
            self.run_on_ui_thread(on_done, exit_code)

    def _spawn_stream(
            self, handle: StreamHandle, argv: List[str], env: dict, *,
            on_line: Callable[[str], None],
            on_done: Optional[Callable[[int], None]],
            input_text: Optional[str] = None,
            cleanup: Optional[Callable[[], None]] = None) -> None:
        import subprocess
        proc = subprocess.Popen(
            argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        if input_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(input_text)
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

        def reader() -> None:
            exit_code = -1
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    if handle._stopped:  # noqa: SLF001 — same-module flag
                        break
                    line = raw.rstrip("\n\r")
                    self.run_on_ui_thread(on_line, line)
            except Exception as exc:  # noqa: BLE001
                logger.debug("stream reader error: %s", exc, exc_info=True)
            finally:
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except Exception:  # noqa: BLE001
                    pass
                code = proc.poll()
                if code is None:
                    try:
                        proc.wait(timeout=1)
                    except Exception:  # noqa: BLE001
                        pass
                    code = proc.poll()
                exit_code = code if code is not None else -1
                if cleanup is not None:
                    try:
                        cleanup()
                    except Exception:  # noqa: BLE001
                        pass
                if on_done is not None:
                    self.run_on_ui_thread(on_done, exit_code)

        thread = threading.Thread(target=reader, daemon=True)
        handle._attach(proc, thread, cleanup=None)  # cleanup runs in reader
        thread.start()

    def acquire_multiplex(self, nickname: str) -> None:
        """Keep a shared SSH master (ControlMaster) warm for ``nickname`` while a
        surface is open, so its ``run_command`` calls reuse one connection instead
        of re-handshaking each time. Refcounted and reusable by any plugin —
        balance every call with :meth:`release_multiplex`. The master itself is
        created lazily by the first ``run_command`` (``ControlMaster=auto``)."""
        from .. import ssh_multiplex
        if nickname:
            ssh_multiplex.acquire(nickname)

    def release_multiplex(self, nickname: str) -> None:
        """Drop a multiplex reference acquired with :meth:`acquire_multiplex`. When
        the last reference goes away, tear the master down promptly via
        ``ssh -O exit`` (reusing the native auth path); if that fails the master
        expires on its own via ControlPersist."""
        from .. import ssh_multiplex
        if not nickname or not ssh_multiplex.release(nickname):
            return
        try:
            import os
            import subprocess
            from ..ssh_connection_builder import (
                ConnectionContext, build_ssh_connection)
            conn = self.connection_manager.find_connection_by_nickname(nickname)
            if conn is None:
                return
            ctx = ConnectionContext(
                connection=conn, connection_manager=self.connection_manager,
                config=self.config, command_type='ssh', native_mode=True,
                extra_args=["-O", "exit", "-o",
                            f"ControlPath={ssh_multiplex.control_path()}"])
            prepared = build_ssh_connection(ctx)
            env = {**os.environ, **(prepared.env or {})}
            subprocess.run(list(prepared.command), env=env, capture_output=True,
                           text=True, timeout=10, check=False)
        except Exception:  # noqa: BLE001 — best-effort teardown; ControlPersist
            pass               # expiry is the fallback, so failures are harmless

    def ensure_local_forward(self, nickname: str, remote_port: int, *,
                             timeout: float = 15) -> int:
        """Return a local TCP port forwarded to ``localhost:remote_port`` on the
        connection's host (API >= 1.12), establishing it if needed over the
        app's single SSH/auth path. Prefers adding the forward onto the shared
        ControlMaster (``ssh -O forward``); otherwise spawns a background
        ``ssh -N``. Forwards are reused per (nickname, remote_port) and live
        until app quit (or with the master). **Blocking** — call from a worker
        thread. Raises ``RuntimeError`` on failure."""
        import atexit
        import subprocess
        import time
        from ..ssh_connection_builder import (
            ConnectionContext, build_ssh_connection)
        from ..port_utils import find_available_port, is_port_available
        from .. import ssh_multiplex

        global _FORWARDS_ATEXIT
        key = (nickname, int(remote_port))
        with _FORWARDS_LOCK:
            if not _FORWARDS_ATEXIT:
                atexit.register(_kill_forward_procs)
                _FORWARDS_ATEXIT = True
            fwd = _FORWARDS.get(key)
            if fwd is not None:
                alive = (fwd.proc.poll() is None if fwd.proc is not None
                         else not is_port_available(fwd.local_port))
                if alive:
                    return fwd.local_port
                _FORWARDS.pop(key, None)

        conn = self.connection_manager.find_connection_by_nickname(nickname)
        if conn is None:
            raise RuntimeError(f"No connection named {nickname!r}")
        lp = find_available_port(
            remote_port if remote_port >= 1024 else 8000 + remote_port)
        if not lp:
            raise RuntimeError("No free local port")
        forward = f"{lp}:localhost:{int(remote_port)}"

        def _build(extra_args: List[str]):
            ctx = ConnectionContext(
                connection=conn, connection_manager=self.connection_manager,
                config=self.config, command_type='ssh', native_mode=True,
                extra_args=extra_args)
            return build_ssh_connection(ctx)

        # Preferred: add the forward onto the shared ControlMaster. Failure is
        # normal (mux off, master not created yet) — fall through, don't error.
        if ssh_multiplex.is_active(nickname):
            try:
                prepared = _build(
                    ["-O", "forward", "-o",
                     f"ControlPath={ssh_multiplex.control_path()}",
                     "-L", forward])
                from ..ssh_connection_builder import apply_headless_askpass_env
                env = apply_headless_askpass_env(
                    prepared.env, conn,
                    session_password=getattr(prepared, "password", None),
                )
                result = subprocess.run(
                    list(prepared.command), env=env, capture_output=True,
                    text=True, timeout=10, check=False)
                if result.returncode == 0:
                    with _FORWARDS_LOCK:
                        _FORWARDS[key] = _Forward(lp)
                    return lp
            except Exception:
                pass

        # Fallback: a dedicated background ssh -N via the same builder/auth.
        cleanup = None
        try:
            prepared = _build(
                ["-N", "-o", "ExitOnForwardFailure=yes", "-L", forward])
            argv = list(prepared.command)
            from ..ssh_connection_builder import apply_headless_askpass_env
            env = apply_headless_askpass_env(
                prepared.env, conn,
                session_password=getattr(prepared, "password", None),
            )
            proc = subprocess.Popen(
                argv, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and proc.poll() is None:
                if not is_port_available(lp):
                    with _FORWARDS_LOCK:
                        _FORWARDS[key] = _Forward(lp, proc)
                    return lp
                time.sleep(0.2)
            try:
                proc.terminate()
            except Exception:
                pass
            raise RuntimeError(f"Could not establish port forward to {nickname}")
        finally:
            if cleanup is not None:
                cleanup()

    def get_effective_ssh_config(self, nickname: str) -> Dict[str, Any]:
        """Return the resolved per-host SSH options for a connection, as
        computed by ``ssh -G`` (keys lowercased; multi-value options are
        lists). Reflects everything in ``~/.ssh/config`` for that host."""
        from ..ssh_config_utils import get_effective_ssh_config
        conn = self.connection_manager.find_connection_by_nickname(nickname)
        host = nickname
        if conn is not None:
            host = getattr(conn, "nickname", None) or getattr(conn, "host", None) or nickname
        return dict(get_effective_ssh_config(host) or {})

    def copy_key_to_host(self, nickname: str, public_key_path: str) -> bool:
        """Install a public key on a saved host via the app's ssh-copy-id path
        (reusing ``resolve_native_auth`` and ``~/.ssh/config``). **Blocking** —
        call from a worker thread. Returns True on success."""
        import subprocess
        from ..ssh_connection_builder import (
            _build_base_ssh_command,
            apply_forced_askpass_env,
            resolve_native_auth,
        )
        from ..ssh_config_utils import get_effective_ssh_config
        conn = self.connection_manager.find_connection_by_nickname(nickname)
        if conn is None or not public_key_path:
            return False
        try:
            host = getattr(conn, "nickname", None) or getattr(conn, "host", None) or nickname
            effective = get_effective_ssh_config(host) or {}
            argv = _build_base_ssh_command(conn, effective, self.config,
                                           'ssh-copy-id')
            argv.extend(['-i', public_key_path])
            auth = resolve_native_auth(conn, self.connection_manager, self.config)
            argv.extend(auth.extra_opts or [])
            # Same as the ssh-copy-id UI: REQUIRE=force → graphical askpass.
            env = apply_forced_askpass_env(
                auth.env, conn,
                session_password=getattr(auth, "password", None),
            )
            result = subprocess.run(
                argv, env=env, capture_output=True, text=True, check=False)
            return result.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    # --- terminals / sessions -----------------------------------------
    def list_sessions(self) -> List["SessionInfo"]:
        """Return the currently open terminal sessions (empty if UI not ready).
        Each ``SessionInfo`` carries the connection and a ``session_id`` usable
        with ``read_terminal``/``send_terminal``. Valid after ``app_started``."""
        return self._host.list_sessions() if self._host is not None else []

    def read_terminal(self, session_id: str,
                      max_chars: Optional[int] = None) -> Optional[str]:
        """Read the visible/scrollback text of a session's terminal (up to
        ``max_chars`` from the end). None if the session is unknown or the UI
        isn't ready. Valid after ``app_started``."""
        return (self._host.read_terminal(session_id, max_chars)
                if self._host is not None else None)

    def send_terminal(self, session_id: str, text: str) -> bool:
        """Send ``text`` (as keyboard input) to a session's terminal. Returns
        False if the session is unknown or the UI isn't ready. Valid after
        ``app_started``."""
        return (self._host.send_terminal(session_id, text)
                if self._host is not None else False)

    # --- per-plugin data dir ------------------------------------------
    @property
    def data_dir(self) -> str:
        """Absolute path to a private, persistent directory for this plugin
        (created on first access). Backed by the XDG data dir."""
        return self._data_dir()

    def _data_dir(self) -> str:
        import os
        # Resolved from XDG_DATA_HOME directly (like the plugin loader's
        # _user_plugin_dir), so it sits beside the installed plugins and needs
        # no GTK/GLib at import time.
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
        path = os.path.join(base, "sshpilot", "plugin-data", self.plugin_id)
        os.makedirs(path, exist_ok=True)
        return path

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
