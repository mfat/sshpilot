"""Live daemon sessions management dialog."""

from __future__ import annotations

from .api.connection_identity import connection_id_for
import logging
from gettext import gettext as _

from gi.repository import Adw, Gtk
from .api.models.sessions import CloseSessionRequest, SessionState

logger = logging.getLogger(__name__)


class DaemonSessionsDialog:
    """Developer-facing dialog for live daemon session discovery/reattach."""

    def __init__(self, window, client, bridge):
        self.window = window
        self.client = client
        self.bridge = bridge
        self._dialog = None
        self._sessions_list = None
        self._status_label = None

    def show(self):
        try:
            self._dialog = Adw.Dialog()
            self._dialog.set_title(_("Daemon Terminal Sessions"))
            toolbar_view = Adw.ToolbarView()
            self._dialog.set_child(toolbar_view)

            header_bar = Adw.HeaderBar()
            header_bar.set_title_widget(Gtk.Label(label=_("Live Sessions")))
            close_button = Gtk.Button(label=_("Close"))
            close_button.connect("clicked", lambda *_: self._dialog.close())
            header_bar.pack_end(close_button)
            refresh_button = Gtk.Button()
            refresh_button.set_icon_name("view-refresh-symbolic")
            refresh_button.set_tooltip_text(_("Refresh session list"))
            refresh_button.connect("clicked", self._refresh_sessions)
            header_bar.pack_start(refresh_button)
            toolbar_view.add_top_bar(header_bar)

            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content_box.set_margin_top(12)
            content_box.set_margin_bottom(12)
            content_box.set_margin_start(12)
            content_box.set_margin_end(12)
            self._status_label = Gtk.Label(label=_("Loading sessions…"))
            self._status_label.set_halign(Gtk.Align.START)
            content_box.append(self._status_label)
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scrolled.set_vexpand(True)
            content_box.append(scrolled)
            self._sessions_list = Gtk.ListBox()
            self._sessions_list.add_css_class("boxed-list")
            scrolled.set_child(self._sessions_list)
            toolbar_view.set_content(content_box)

            self._dialog.set_content_width(640)
            self._dialog.set_content_height(420)
            self._dialog.present(self.window)
            self._refresh_sessions()
        except Exception:
            logger.error("Failed to show daemon sessions dialog", exc_info=True)

    def _refresh_sessions(self, *_args):
        self._status_label.set_text(_("Loading sessions…"))
        self._clear_sessions_list()
        self.bridge.submit(
            lambda: self.client.list_sessions(),
            on_success=self._on_sessions_loaded,
            on_error=self._on_sessions_error,
        )

    def _clear_sessions_list(self):
        child = self._sessions_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self._sessions_list.remove(child)
            child = next_child

    def _on_sessions_loaded(self, sessions):
        sessions = list(sessions or [])
        if not sessions:
            self._status_label.set_text(_("No daemon sessions"))
            return
        self._status_label.set_text(
            _("Found {count} daemon session(s)").format(count=len(sessions))
        )
        for session in sessions:
            self._add_session_row(session)

    def _on_sessions_error(self, error):
        logger.error("Failed to load daemon sessions: %s", type(error).__name__)
        self._status_label.set_text(_("Failed to load daemon sessions"))

    def _add_session_row(self, session):
        row = Adw.ActionRow()
        nickname = self._connection_nickname(session.connection_id)
        row.set_title(nickname or str(session.connection_id))
        owner = "none"
        if session.input_owner is not None:
            owner = str(session.input_owner.attachment_id)
        row.set_subtitle(
            _(
                "state={state} attachments={count} input_owner={owner}"
            ).format(
                state=getattr(session.state, "value", session.state),
                count=session.attachment_count,
                owner=owner,
            )
        )

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        attach_button = Gtk.Button(label=_("Open in new tab"))
        attach_button.add_css_class("suggested-action")
        attach_button.set_sensitive(
            session.state
            in {SessionState.RUNNING, SessionState.STARTING, SessionState.CREATED}
        )
        attach_button.connect(
            "clicked",
            lambda *_: self._attach_to_session(session),
        )
        button_box.append(attach_button)
        terminate_button = Gtk.Button(label=_("Terminate"))
        terminate_button.add_css_class("destructive-action")
        terminate_button.connect(
            "clicked",
            lambda *_: self._terminate_session(session),
        )
        button_box.append(terminate_button)
        row.add_suffix(button_box)
        self._sessions_list.append(row)

    def _connection_nickname(self, connection_id) -> str:
        manager = getattr(self.window, "connection_manager", None)
        if manager is None:
            return ""
        for connection in manager.get_connections():
            try:
                if connection_id_for(connection) == connection_id:
                    return getattr(connection, "nickname", "") or ""
            except Exception:
                continue
        return ""

    def _attach_to_session(self, session):
        manager = getattr(self.window, "terminal_manager", None)
        connection_manager = getattr(self.window, "connection_manager", None)
        if manager is None or connection_manager is None:
            return
        connection = None
        for candidate in connection_manager.get_connections():
            try:
                if connection_id_for(candidate) == session.connection_id:
                    connection = candidate
                    break
            except Exception:
                continue
        if connection is None:
            self._status_label.set_text(
                _("Saved connection for this session is no longer available")
            )
            return

        from .terminal import TerminalWidget
        from sshpilot import icon_utils

        group_color, group_name = manager._resolve_group_color_and_name(connection)
        terminal = TerminalWidget(
            connection,
            self.window.config,
            connection_manager,
            group_color=group_color,
        )
        terminal.connect("connection-established", manager.on_terminal_connected)
        terminal.connect("connection-lost", manager.on_terminal_disconnected)
        terminal.connect("title-changed", manager.on_terminal_title_changed)
        if group_name:
            setattr(terminal, "group_name", group_name)
        self.window.connection_to_terminals.setdefault(connection, []).append(terminal)
        self.window.terminal_to_connection[terminal] = connection
        self.window.active_terminals[connection] = terminal

        page = self.window.tab_view.append(terminal)
        page.set_title(connection.nickname)
        page.set_icon(icon_utils.new_gicon_from_icon_name("utilities-terminal-symbolic"))
        self.window.show_tab_view()
        self.window.tab_view.set_selected_page(page)
        terminal.apply_theme()
        terminal.attach_daemon_session(
            self.client,
            self.bridge,
            session.id,
            connection_id=session.connection_id,
        )
        self._dialog.close()

    def _terminate_session(self, session):
        self.bridge.submit(
            lambda: self.client.close_session(
                CloseSessionRequest(session_id=session.id)
            ),
            on_success=lambda _value: self._refresh_sessions(),
            on_error=self._on_sessions_error,
        )
