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
    add_menu_item: bool = True            # whether to add a Tools-menu entry
    on_activate: Optional[Callable[[], None]] = None  # menu click handler (overrides open_page)


@dataclass
class _ConnActionReg:
    """A plugin-contributed item in the connection-list right-click menu."""
    full_id: str
    label: str
    icon_name: str
    callback: Callable[[str], None]  # called with the connection nickname
    plugin_id: str


class UiHost:
    """Hosts plugin-contributed UI. Registration is always safe; live calls
    (open_page/notify) queue until a window is bound."""

    def __init__(self) -> None:
        self._window = None
        self._pages: Dict[str, _PageReg] = {}
        self._pending_open: List[str] = []
        self._pending_toasts: List[Tuple[str, int]] = []
        # Plugin-contributed connection context-menu actions. The window reads
        # this when it builds a connection's right-click menu (rebuilt each
        # time), so actions registered at activate time appear without any
        # window-action plumbing.
        self._connection_actions: Dict[str, _ConnActionReg] = {}

    # --- registration (safe before or after bind) ---------------------
    def register_page(self, full_id: str, title: str, icon_name: str,
                      factory: Callable[[], Any], *, plugin_id: str,
                      add_menu_item: bool = True,
                      on_activate: Optional[Callable[[], None]] = None) -> None:
        if full_id in self._pages:
            logger.warning("Plugin page %r already registered; ignoring", full_id)
            return
        self._pages[full_id] = _PageReg(full_id, title, icon_name, factory, plugin_id,
                                        add_menu_item=add_menu_item, on_activate=on_activate)
        if self._window is not None:
            self._install_menu_item(self._pages[full_id])

    def page_ids_for_plugin(self, plugin_id: str) -> List[str]:
        """Full ids of pages registered by ``plugin_id`` (empty if none / the
        plugin isn't active). Used by Preferences to offer an 'open page' gear."""
        return [fid for fid, reg in self._pages.items() if reg.plugin_id == plugin_id]

    def register_connection_action(self, action_id: str, label: str,
                                   icon_name: str, callback: Callable[[str], None],
                                   *, plugin_id: str) -> None:
        """Register an item for the connection-list right-click menu. ``callback``
        is called with the connection's nickname when chosen. Safe before or
        after bind — the menu is rebuilt on each right-click."""
        full_id = f"{plugin_id}:{action_id}"
        if full_id in self._connection_actions:
            logger.warning("Connection action %r already registered; ignoring", full_id)
            return
        self._connection_actions[full_id] = _ConnActionReg(
            full_id, label, icon_name, callback, plugin_id)

    def connection_actions(self) -> List[_ConnActionReg]:
        """All registered connection context-menu actions (window reads this)."""
        return list(self._connection_actions.values())

    def remove_plugin_actions(self, plugin_id: str) -> None:
        """Drop every connection action a plugin registered (used on deactivate)."""
        for full_id in [fid for fid, reg in self._connection_actions.items()
                        if reg.plugin_id == plugin_id]:
            self._connection_actions.pop(full_id, None)

    def remove_plugin_pages(self, plugin_id: str) -> None:
        """Drop every page a plugin registered (used on deactivate): close its
        open tab, remove its window action, and forget the registration.
        Best-effort — a failure on one page never blocks the others."""
        for full_id in [fid for fid, reg in self._pages.items()
                        if reg.plugin_id == plugin_id]:
            reg = self._pages.pop(full_id, None)
            if reg is None:
                continue
            try:
                self._pending_open.remove(full_id)
            except ValueError:
                pass
            window = self._window
            if window is None:
                continue
            # Close the tab if it's still attached.
            if reg.tab_page is not None:
                try:
                    if reg.tab_page in list(window.tab_view.get_pages()):
                        window.tab_view.close_page(reg.tab_page)
                except Exception:
                    logger.exception("Failed to close plugin page %r", full_id)
                reg.tab_page = None
            # Remove the menu action so it can't be re-invoked.
            try:
                from gi.repository import Gio  # noqa: F401
                action_name = self._action_name(reg)
                if window.lookup_action(action_name) is not None:
                    window.remove_action(action_name)
            except Exception:
                logger.exception("Failed to remove action for page %r", full_id)

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
        if not reg.add_menu_item:
            return  # page is opened directly (e.g. per-host tabs), no menu entry
        try:
            from gi.repository import Gio
            action_name = self._action_name(reg)

            def _activate(*_a, reg=reg):
                if reg.on_activate is not None:
                    reg.on_activate()
                else:
                    self.open_page(reg.full_id)

            if window.lookup_action(action_name) is None:
                action = Gio.SimpleAction.new(action_name, None)
                action.connect("activate", _activate)
                window.add_action(action)
                section = getattr(window, "_plugins_menu_section", None)
                if section is not None:
                    section.append(reg.title, f"win.{action_name}")
        except Exception:
            logger.exception("Failed to install menu item for page %r", reg.full_id)

    def _open_now(self, full_id: str) -> None:
        reg = self._pages[full_id]
        # Pages registered with on_activate are opened indirectly (e.g. Docker
        # Console's Tools-menu entry redirects to a per-host tab). Match the
        # menu handler so open_page / Preferences gear behave the same way.
        if reg.on_activate is not None:
            reg.on_activate()
            return
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
        if widget is None:
            logger.error("Plugin page factory for %r returned None", full_id)
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
        # session_id (str) -> weakref to the live terminal widget, for
        # ctx.read_terminal / ctx.send_terminal. Weak so a closed tab's widget
        # can be collected even if a plugin held the id.
        self._terminal_widgets: Dict[str, Any] = {}

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
        import weakref
        try:
            self._terminal_widgets[str(key)] = weakref.ref(terminal)
        except TypeError:
            self._terminal_widgets[str(key)] = lambda t=terminal: t
        self.events.emit(Events.SESSION_OPENED, info)

    def dispatch_session_closed(self, terminal) -> None:
        key = id(terminal)
        self._terminal_widgets.pop(str(key), None)
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

    def open_command_terminal(self, nickname: str, remote_command: str,
                              *, title: Optional[str] = None,
                              pty_prompt: Optional[str] = None,
                              pty_response: Optional[str] = None) -> bool:
        if self._window is None:
            logger.warning("open_command_terminal(%r) before window is ready", nickname)
            return False
        if not remote_command or not str(remote_command).strip():
            return False
        try:
            conn = self._cm.find_connection_by_nickname(nickname)
        except Exception:
            conn = None
        if conn is None:
            self.ui.notify(f"No connection named {nickname!r}")
            return False
        try:
            import copy
            # Transient clone so the saved connection's cached command/state is
            # not polluted and the one-off command runs in its own tab. The clone
            # re-prepares via the native builder with the remote command appended
            # on the CLI (same path as ctx.run_command) — no new SSH/auth path.
            clone = copy.copy(conn)
            clone.ssh_cmd = []
            clone.ssh_connection_cmd = None
            clone.ssh_env = {}
            self._window.terminal_manager.connect_to_host(
                clone, force_new=True,
                remote_command=str(remote_command), tab_title=title,
                force_tty=True,  # a command in a terminal tab always wants a PTY
                pty_prompt=pty_prompt, pty_response=pty_response,
            )
            return True
        except Exception:
            logger.exception("open_command_terminal(%r) failed", nickname)
            return False

    def open_local_command_terminal(self, command: str, *,
                                    title: Optional[str] = None,
                                    pty_prompt: Optional[str] = None,
                                    pty_response: Optional[str] = None) -> bool:
        if self._window is None:
            logger.warning("open_local_command_terminal before window is ready")
            return False
        if not command or not str(command).strip():
            return False
        try:
            return bool(self._window.terminal_manager.show_local_terminal(
                title=title or "Local Command",
                command=str(command),
                pty_prompt=pty_prompt,
                pty_response=pty_response,
            ))
        except Exception:
            logger.exception("open_local_command_terminal failed")
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

    # --- keys ---------------------------------------------------------
    def list_keys(self) -> List[Dict[str, str]]:
        """Public+private paths of keys known to the app's KeyManager."""
        window = self._window
        km = getattr(window, "key_manager", None) if window is not None else None
        if km is None:
            return []
        try:
            keys = km.discover_keys()
        except Exception:
            logger.exception("list_keys failed")
            return []
        out: List[Dict[str, str]] = []
        for k in keys or []:
            priv = getattr(k, "private_path", None) or getattr(k, "path", None)
            if not priv:
                continue
            pub = getattr(k, "public_path", None) or f"{priv}.pub"
            out.append({"private_path": str(priv), "public_path": str(pub)})
        return out

    def delete_key(self, private_path: str) -> bool:
        """Delete a key pair, but only if it lives inside the KeyManager's key
        directory (guards against deleting arbitrary files)."""
        import os
        window = self._window
        km = getattr(window, "key_manager", None) if window is not None else None
        if km is None or not private_path:
            return False
        key_dir = getattr(km, "key_dir", None) or getattr(km, "ssh_dir", None)
        if not key_dir:
            return False
        key_dir = os.path.realpath(str(key_dir))
        target = os.path.realpath(str(private_path))
        if target != key_dir and not target.startswith(key_dir + os.sep):
            logger.warning("delete_key refused outside key dir: %r", private_path)
            return False
        ok = False
        for path in (target, f"{target}.pub"):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    ok = True
            except OSError:
                logger.exception("delete_key failed for %r", path)
        return ok

    # --- terminals / sessions -----------------------------------------
    def list_sessions(self) -> List[SessionInfo]:
        """Snapshot of currently open terminal sessions."""
        return list(self._terminal_sessions.values())

    def _terminal_for(self, session_id: str):
        ref = self._terminal_widgets.get(str(session_id))
        return ref() if ref is not None else None

    def read_terminal(self, session_id: str,
                      max_chars: Optional[int] = None) -> Optional[str]:
        """Read a session terminal's text via the backend's get_content (which
        already handles the VTE feed/get_text_format gotcha)."""
        term = self._terminal_for(session_id)
        if term is None:
            return None
        backend = getattr(term, "backend", None)
        try:
            if backend is not None and hasattr(backend, "get_content"):
                return backend.get_content(max_chars)
        except Exception:
            logger.exception("read_terminal failed")
        return None

    def send_terminal(self, session_id: str, text: str) -> bool:
        """Feed input to a session terminal (on the UI thread)."""
        term = self._terminal_for(session_id)
        if term is None or text is None:
            return False
        data = text.encode("utf-8")

        def _feed():
            backend = getattr(term, "backend", None)
            if backend is not None and hasattr(backend, "feed_child"):
                backend.feed_child(data)
            elif getattr(term, "vte", None) is not None:
                term.vte.feed_child(data)

        try:
            self.run_on_ui_thread(_feed)
            return True
        except Exception:
            logger.exception("send_terminal failed")
            return False

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
