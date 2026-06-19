"""Plugin host services: the event bus, UI extension surface, and the
bridges that turn internal application activity into stable plugin events.

This module is the engine behind the public ``PluginContext`` facades in
``api.py``. Plugins never import it directly — they receive scoped facades
(``ctx.events``, ``ctx.ui``) that delegate here. Keeping the machinery out of
``api.py`` lets the public surface stay small and stable.

Design notes that matter:

* **Timing.** Plugins are activated *before* the main window's UI exists
  (``MainWindow.__init__`` loads plugins, then ``setup_ui`` builds the
  widgets). So everything UI-facing is deferred: registrations are recorded
  and ``open_page``/``notify`` calls queue until :meth:`PluginHost.bind_window`
  wires the live window at the end of ``setup_ui``. ``activate(ctx)`` must
  therefore be registration-only; live UI/terminal/key work is valid only
  after the ``app_started`` event.
* **Decoupling.** Events carry frozen :class:`ConnectionInfo` / :class:`SessionInfo`
  snapshots built at emit time — the internal ``Connection`` object never
  enters a payload.
* **Solidity.** Event emit is synchronous and on the main thread; each plugin
  callback is wrapped so one misbehaving plugin cannot break dispatch, other
  plugins, or the app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class Events:
    """Stable event-name constants. Plugins subscribe via ``ctx.events``."""

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
    """Read-only snapshot of a connection handed to plugins in events and
    returned from ``ctx.add_connection``. Decoupled from the internal
    ``Connection`` class, which is private and may change."""

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
    """Read-only snapshot of an open terminal session (one tab)."""

    connection: ConnectionInfo
    session_id: str


class EventBus:
    """Synchronous, main-thread event dispatcher with per-callback isolation."""

    def __init__(self) -> None:
        # event name -> list of (plugin_id, callback)
        self._subs: Dict[str, List[Tuple[str, Callable[[Any], None]]]] = {}

    def subscribe(self, event: str, callback: Callable[[Any], None],
                  *, plugin_id: str) -> None:
        if event not in ALL_EVENTS:
            raise ValueError(f"Unknown event {event!r}; valid events: "
                             f"{sorted(ALL_EVENTS)}")
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._subs.setdefault(event, []).append((plugin_id, callback))

    def unsubscribe(self, event: str, callback: Callable[[Any], None],
                    *, plugin_id: str) -> None:
        subs = self._subs.get(event)
        if not subs:
            return
        self._subs[event] = [
            (pid, cb) for (pid, cb) in subs
            if not (pid == plugin_id and cb == callback)
        ]

    def unsubscribe_plugin(self, plugin_id: str) -> None:
        """Drop every subscription owned by a plugin (used on deactivate)."""
        for event in list(self._subs):
            self._subs[event] = [
                (pid, cb) for (pid, cb) in self._subs[event] if pid != plugin_id
            ]

    def emit(self, event: str, payload: Any) -> None:
        # Iterate a copy so a callback that (un)subscribes mid-dispatch is safe.
        for plugin_id, callback in list(self._subs.get(event, ())):
            try:
                callback(payload)
            except Exception:
                logger.exception(
                    "Plugin %r callback for event %r failed", plugin_id, event)


@dataclass
class _PageReg:
    full_id: str
    title: str
    icon_name: str
    factory: Callable[[], Any]
    plugin_id: str
    tab_page: Any = None  # cached Adw.TabPage once opened


class UiHost:
    """Hosts plugin-contributed UI. Registration is always safe; live calls
    (open_page/notify) queue until a window is bound."""

    def __init__(self) -> None:
        self._window = None
        self._pages: Dict[str, _PageReg] = {}
        self._pending_open: List[str] = []
        self._pending_toasts: List[Tuple[str, int]] = []

    # --- registration (safe before or after bind) ---------------------
    def register_page(self, full_id: str, title: str, icon_name: str,
                      factory: Callable[[], Any], *, plugin_id: str) -> None:
        if full_id in self._pages:
            logger.warning("Plugin page %r already registered; ignoring", full_id)
            return
        self._pages[full_id] = _PageReg(full_id, title, icon_name, factory, plugin_id)
        if self._window is not None:
            self._install_menu_item(self._pages[full_id])

    def page_ids_for_plugin(self, plugin_id: str) -> List[str]:
        """Full ids of pages registered by ``plugin_id`` (empty if none / the
        plugin isn't active). Used by Preferences to offer an 'open page' gear."""
        return [fid for fid, reg in self._pages.items() if reg.plugin_id == plugin_id]

    # --- live calls ---------------------------------------------------
    def open_page(self, full_id: str) -> None:
        if full_id not in self._pages:
            logger.warning("open_page for unknown page %r", full_id)
            return
        if self._window is None:
            self._pending_open.append(full_id)
            return
        self._open_now(full_id)

    def notify(self, message: str, timeout: int = 3) -> None:
        if self._window is None:
            self._pending_toasts.append((message, timeout))
            return
        self._show_toast(message, timeout)

    # --- binding ------------------------------------------------------
    def bind_window(self, window) -> None:
        self._window = window
        for reg in self._pages.values():
            self._install_menu_item(reg)
        for message, timeout in self._pending_toasts:
            self._show_toast(message, timeout)
        self._pending_toasts.clear()
        for full_id in self._pending_open:
            self._open_now(full_id)
        self._pending_open.clear()

    # --- internals ----------------------------------------------------
    def _show_toast(self, message: str, timeout: int) -> None:
        try:
            from gi.repository import Adw
            toast = Adw.Toast.new(str(message))
            try:
                toast.set_timeout(int(timeout))
            except Exception:
                pass
            self._window.toast_overlay.add_toast(toast)
        except Exception:
            logger.exception("Failed to show plugin toast")

    def _action_name(self, reg: _PageReg) -> str:
        safe = reg.full_id.replace(":", "-").replace(".", "-")
        return f"plugin-page-{safe}"

    def _install_menu_item(self, reg: _PageReg) -> None:
        window = self._window
        if window is None:
            return
        try:
            from gi.repository import Gio
            action_name = self._action_name(reg)
            if window.lookup_action(action_name) is None:
                action = Gio.SimpleAction.new(action_name, None)
                action.connect("activate", lambda *_a, fid=reg.full_id: self.open_page(fid))
                window.add_action(action)
                section = getattr(window, "_plugins_menu_section", None)
                if section is not None:
                    section.append(reg.title, f"win.{action_name}")
        except Exception:
            logger.exception("Failed to install menu item for page %r", reg.full_id)

    def _open_now(self, full_id: str) -> None:
        reg = self._pages[full_id]
        window = self._window
        # Re-focus an already-open page if its tab is still attached.
        if reg.tab_page is not None:
            try:
                pages = window.tab_view.get_pages()
                if reg.tab_page in list(pages):
                    window.tab_view.set_selected_page(reg.tab_page)
                    if hasattr(window, "show_tab_view"):
                        window.show_tab_view()
                    return
            except Exception:
                pass
            reg.tab_page = None
        # Build the widget (plugin code — isolate failures).
        try:
            widget = reg.factory()
        except Exception:
            logger.exception("Plugin page factory for %r failed", full_id)
            self._show_toast(f"Failed to open {reg.title}", 3)
            return
        try:
            if hasattr(window, "show_tab_view"):
                window.show_tab_view()
            page = window.tab_view.append(widget)
            page.set_title(reg.title)
            try:
                from sshpilot import icon_utils
                page.set_icon(icon_utils.new_gicon_from_icon_name(reg.icon_name))
            except Exception:
                pass
            window.tab_view.set_selected_page(page)
            reg.tab_page = page
        except Exception:
            logger.exception("Failed to open plugin page %r", full_id)
            self._show_toast(f"Failed to open {reg.title}", 3)


class PluginHost:
    """Owns the event bus and UI host, bridges internal signals to events,
    and exposes the live services (open connection, generate key, run on UI
    thread). Created once per process and bound to the first window."""

    def __init__(self, *, connection_manager) -> None:
        self.events = EventBus()
        self.ui = UiHost()
        self._cm = connection_manager
        self._window = None
        self._cm_handlers: List[int] = []
        # id(terminal) -> SessionInfo; also the reconnect-dedupe set.
        self._terminal_sessions: Dict[int, SessionInfo] = {}

    # --- binding ------------------------------------------------------
    def bind_window(self, window) -> None:
        if self._window is not None:
            return  # idempotent; first window wins (activate runs once/process)
        self._window = window
        self.ui.bind_window(window)
        self._connect_cm_signals()

    def _connect_cm_signals(self) -> None:
        cm = self._cm
        # ConnectionManager overrides connect() (it's the async per-connection
        # connect), so subscribe to its GObject signals via connect_after, the
        # same way the window does.
        if cm is None or not hasattr(cm, "connect_after"):
            return
        try:
            self._cm_handlers = [
                cm.connect_after("connection-added", self._on_cm_added),
                cm.connect_after("connection-updated", self._on_cm_updated),
                cm.connect_after("connection-removed", self._on_cm_removed),
            ]
        except Exception:
            logger.exception("Failed to connect connection-manager signals")

    # --- connection event bridges (main thread) -----------------------
    def _on_cm_added(self, _cm, conn) -> None:
        self.events.emit(Events.CONNECTION_CREATED, ConnectionInfo.from_connection(conn))

    def _on_cm_updated(self, _cm, conn) -> None:
        self.events.emit(Events.CONNECTION_UPDATED, ConnectionInfo.from_connection(conn))

    def _on_cm_removed(self, _cm, conn) -> None:
        self.events.emit(Events.CONNECTION_DELETED, ConnectionInfo.from_connection(conn))

    # --- session event dispatch (called from terminal_manager) --------
    def dispatch_session_opened(self, terminal) -> None:
        key = id(terminal)
        if key in self._terminal_sessions:
            return  # reconnect of an already-tracked terminal; not a new session
        info = SessionInfo(
            ConnectionInfo.from_connection(getattr(terminal, "connection", None)),
            str(key),
        )
        self._terminal_sessions[key] = info
        self.events.emit(Events.SESSION_OPENED, info)

    def dispatch_session_closed(self, terminal) -> None:
        key = id(terminal)
        info = self._terminal_sessions.pop(key, None)
        if info is None:
            info = SessionInfo(
                ConnectionInfo.from_connection(getattr(terminal, "connection", None)),
                str(key),
            )
        self.events.emit(Events.SESSION_CLOSED, info)

    # --- app lifecycle (called from main.py) --------------------------
    def dispatch_app_started(self) -> None:
        self.events.emit(Events.APP_STARTED, None)

    def dispatch_app_shutdown(self) -> None:
        self.events.emit(Events.APP_SHUTDOWN, None)

    # --- live services exposed via PluginContext ----------------------
    def open_connection(self, nickname: str) -> bool:
        if self._window is None:
            logger.warning("open_connection(%r) before window is ready", nickname)
            return False
        try:
            conn = self._cm.find_connection_by_nickname(nickname)
        except Exception:
            conn = None
        if conn is None:
            self.ui.notify(f"No connection named {nickname!r}")
            return False
        try:
            self._window.terminal_manager.connect_to_host(conn)
            return True
        except Exception:
            logger.exception("open_connection(%r) failed", nickname)
            return False

    # --- connection groups -------------------------------------------
    def ensure_group(self, name: str, color: Optional[str] = None) -> Optional[str]:
        """Find-or-create a connection group by display name; return its id.

        Idempotent: re-provisioning the same workspace reuses the existing
        group instead of erroring on a duplicate name. None if no window."""
        window = self._window
        gm = getattr(window, "group_manager", None) if window is not None else None
        if gm is None:
            logger.warning("ensure_group(%r) before window is ready", name)
            return None
        try:
            target = str(name).strip().lower()
            for g in gm.get_all_groups():
                if str(g.get("name", "")).strip().lower() == target:
                    return g.get("id")
            return gm.create_group(name, color=color)
        except Exception:
            logger.exception("ensure_group(%r) failed", name)
            return None

    def add_connection_to_group(self, nickname: str, group_id: str,
                                rebuild: bool = True) -> bool:
        """Move a connection (by nickname) into a group. Returns False if no
        window or the group does not exist (guarding the move's silent
        drop-to-root)."""
        window = self._window
        gm = getattr(window, "group_manager", None) if window is not None else None
        if gm is None or group_id not in getattr(gm, "groups", {}):
            return False
        try:
            gm.move_connection(nickname, group_id)
        except Exception:
            logger.exception("add_connection_to_group(%r, %r) failed",
                             nickname, group_id)
            return False
        if rebuild:
            self.rebuild_sidebar()
        return True

    def rebuild_sidebar(self) -> None:
        window = self._window
        if window is None or not hasattr(window, "rebuild_connection_list"):
            return
        # run_on_ui_thread runs inline on the main thread (group ops happen
        # there) and marshals from a worker; also works under the test stub.
        self.run_on_ui_thread(window.rebuild_connection_list)

    def generate_key(self, name: str, **kwargs) -> Optional[str]:
        """Generate an SSH key via the app's KeyManager. Returns the private
        key path, or None on failure. Valid after app_started."""
        window = self._window
        km = getattr(window, "key_manager", None) if window is not None else None
        if km is None:
            logger.warning("generate_key(%r) before key manager is ready", name)
            return None
        try:
            key = km.generate_key(name, **kwargs)
        except Exception:
            logger.exception("generate_key(%r) failed", name)
            return None
        if key is None:
            return None
        path = getattr(key, "private_path", None) or getattr(key, "path", None)
        return str(path) if path else None

    def run_on_ui_thread(self, fn: Callable, *args) -> None:
        # Already on the main thread (or in a headless test) → run inline.
        # From a worker thread → marshal onto the GLib main loop.
        import threading
        if threading.current_thread() is threading.main_thread():
            fn(*args)
            return
        try:
            from gi.repository import GLib

            def _wrapped():
                fn(*args)
                return False

            GLib.idle_add(_wrapped)
        except Exception:
            fn(*args)
