"""Welcome page widget for sshPilot."""

import gi
import logging

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, Gdk

from gettext import gettext as _

from .platform_utils import is_macos
from . import icon_utils

logger = logging.getLogger(__name__)

_CSS = b"""
.startpage-hero {
    background-color: alpha(@accent_bg_color, 0.15);
    border: 1px solid alpha(@accent_color, 0.3);
    border-radius: 16px;
    min-width: 64px;
    min-height: 64px;
}
.startpage-hero image { color: @accent_color; }
.startpage-chip { padding: 6px 14px; border-radius: 8px; }
.startpage-card { padding: 4px; }
.startpage-status {
    border-radius: 50%;
    min-width: 8px;
    min-height: 8px;
    background-color: alpha(@window_fg_color, 0.35);
}
.startpage-status.online { background-color: @success_color; }
.startpage-mono {
    font-family: monospace;
    font-size: 0.85em;
}
.startpage-heading {
    font-size: 0.8em;
    font-weight: bold;
    opacity: 0.6;
}
"""

_css_loaded = False


def _ensure_css():
    global _css_loaded
    if _css_loaded:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_loaded = True


class WelcomePage(Gtk.Overlay):
    """Start page shown in the pinned Start tab."""

    def __init__(self, window) -> None:
        super().__init__()
        _ensure_css()
        self.window = window
        self.connection_manager = window.connection_manager
        self.config = window.config
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_focus(False)

        self._pinned_rows_box = None

        self.connection_manager.connect_after('connection-removed', self._on_connection_removed)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_can_focus(False)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content_box.set_margin_top(24)
        content_box.set_margin_bottom(24)
        content_box.set_valign(Gtk.Align.CENTER)
        content_box.set_can_focus(False)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(520)
        clamp.set_tightening_threshold(400)
        clamp.set_child(content_box)
        clamp.set_vexpand(True)
        clamp.set_can_focus(False)
        scrolled.set_child(clamp)
        self.set_child(scrolled)

        current_shortcuts = self._get_safe_current_shortcuts()
        content_box.append(self._build_layout(current_shortcuts))

        # Footer links pinned to the bottom of the page
        footer = self._build_footer()
        footer.set_halign(Gtk.Align.CENTER)
        footer.set_valign(Gtk.Align.END)
        footer.set_margin_bottom(32)
        self.add_overlay(footer)

    # --- Layout ---

    def _build_layout(self, current_shortcuts):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # Hero icon — button that opens a local terminal
        hero_icon = icon_utils.new_image_from_icon_name('utilities-terminal-symbolic', 28)
        hero_btn = Gtk.Button()
        hero_btn.set_child(hero_icon)
        hero_btn.add_css_class('startpage-hero')
        hero_btn.set_halign(Gtk.Align.CENTER)
        hero_btn.set_valign(Gtk.Align.CENTER)
        hero_btn.set_margin_bottom(28)
        hero_btn.set_can_focus(False)
        hero_btn.set_tooltip_text(_('Open Local Terminal'))
        hero_btn.connect('clicked', lambda *_a: self.window.terminal_manager.show_local_terminal())
        box.append(hero_btn)

        # Subtitle
        subtitle = Gtk.Label(label=_('Double-click a host to connect or create a new connection'))
        subtitle.add_css_class('dim-label')
        subtitle.set_halign(Gtk.Align.CENTER)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_wrap(True)
        subtitle.set_margin_top(8)
        subtitle.set_margin_bottom(32)
        box.append(subtitle)

        # Primary action: New connection
        new_accel = self._get_action_accel_display(current_shortcuts, 'new-connection')
        btn_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_content.append(icon_utils.new_image_from_icon_name('list-add-symbolic'))
        btn_content.append(Gtk.Label(label=_('New connection')))
        if new_accel:
            accel_lbl = Gtk.Label(label=new_accel)
            accel_lbl.set_opacity(0.7)
            btn_content.append(accel_lbl)
        new_btn = Gtk.Button()
        new_btn.set_child(btn_content)
        new_btn.add_css_class('suggested-action')
        new_btn.add_css_class('pill')
        new_btn.set_halign(Gtk.Align.CENTER)
        new_btn.set_can_focus(False)
        new_btn.set_margin_bottom(32)
        new_btn.connect('clicked', lambda *_a: self.window.get_application().activate_action('new-connection'))
        box.append(new_btn)

        # Quick action chips
        chips = Gtk.FlowBox()
        chips.set_selection_mode(Gtk.SelectionMode.NONE)
        chips.set_halign(Gtk.Align.CENTER)
        chips.set_max_children_per_line(3)
        chips.set_column_spacing(8)
        chips.set_row_spacing(8)
        chips.set_can_focus(False)
        chips.set_margin_bottom(36)
        quick_actions = [
            ('document-edit-symbolic', _('Edit SSH Configuration'),
             lambda: self.window.get_application().activate_action('edit-ssh-config')),
            ('utilities-terminal-symbolic', _('Local terminal'),
             lambda: self.window.terminal_manager.show_local_terminal()),
            ('system-run-symbolic', _('Snippets'),
             self._open_command_blocks_sidebar),
        ]
        for icon_name, label, cb in quick_actions:
            chips.append(self._build_chip(icon_name, label, cb))
        box.append(chips)

        # Pinned connections / sessions (populated dynamically)
        self._pinned_rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.append(self._pinned_rows_box)
        self._populate_pinned_rows_box()

        return box

    def _build_footer(self):
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        shortcuts_link = Gtk.Button(label=_('Keyboard shortcuts'))
        shortcuts_link.add_css_class('flat')
        shortcuts_link.add_css_class('dim-label')
        shortcuts_link.set_can_focus(False)
        shortcuts_link.connect('clicked', lambda *_a: self.window.show_shortcuts_window())
        footer.append(shortcuts_link)
        sep = Gtk.Label(label='·')
        sep.add_css_class('dim-label')
        footer.append(sep)
        docs_link = Gtk.Button(label=_('Online documentation'))
        docs_link.add_css_class('flat')
        docs_link.add_css_class('dim-label')
        docs_link.set_can_focus(False)
        docs_link.connect('clicked', lambda *_a: self.open_online_help())
        footer.append(docs_link)
        return footer

    def _build_chip(self, icon_name, label, callback):
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.append(icon_utils.new_image_from_icon_name(icon_name))
        content.append(Gtk.Label(label=label))
        btn = Gtk.Button()
        btn.set_child(content)
        btn.add_css_class('startpage-chip')
        btn.set_can_focus(False)
        btn.connect('clicked', lambda *_a: callback())
        return btn

    def _open_command_blocks_sidebar(self) -> None:
        """Toggle the command blocks right sidebar from the start page."""
        if hasattr(self.window, '_toggle_command_blocks_panel'):
            self.window._toggle_command_blocks_panel()

    # --- Pinned connections ---

    def _build_conn_card(self, conn):
        """Card button for a pinned connection."""
        online = conn in getattr(self.window, 'active_terminals', {})

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dot = Gtk.Box()
        dot.add_css_class('startpage-status')
        if online:
            dot.add_css_class('online')
        dot.set_valign(Gtk.Align.CENTER)
        top.append(dot)
        name = Gtk.Label(label=conn.nickname, xalign=0)
        name.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        top.append(name)

        host_label = getattr(conn, 'host', '') or getattr(conn, 'hostname', '')
        username = getattr(conn, 'username', '')
        target = f"{username}@{host_label}" if username and host_label else host_label
        sub = Gtk.Label(label=target, xalign=0)
        sub.add_css_class('startpage-mono')
        sub.add_css_class('dim-label')
        sub.set_ellipsize(3)
        sub.set_margin_start(14)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        inner.append(top)
        inner.append(sub)

        card = Gtk.Button()
        card.set_child(inner)
        card.add_css_class('card')
        card.add_css_class('startpage-card')
        card.set_hexpand(True)
        card.set_can_focus(False)
        card.connect('clicked', lambda _b, c=conn: self.window.terminal_manager.connect_to_host(c))
        return card

    def _build_pinned_section(self):
        """Grid of pinned host cards, or None if none are pinned."""
        pinned_nicknames = self.config.get_pinned_nicknames()
        if not pinned_nicknames:
            return None
        conn_map = {c.nickname: c for c in self.connection_manager.connections}

        grid = Gtk.FlowBox()
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_max_children_per_line(2)
        grid.set_min_children_per_line(1)
        grid.set_homogeneous(True)
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)
        grid.set_can_focus(False)

        rows_added = 0
        for nickname in pinned_nicknames:
            conn = conn_map.get(nickname)
            if conn is None:
                continue
            grid.append(self._build_conn_card(conn))
            rows_added += 1
        if rows_added == 0:
            return None
        return self._section(_('Pinned Connections'), grid)

    def _build_pinned_sessions_section(self):
        """Grid of pinned session cards, or None if none are pinned."""
        session_manager = getattr(self.window, 'session_manager', None)
        if session_manager is None:
            return None
        pinned_names = session_manager.get_pinned_session_names()
        if not pinned_names:
            return None

        grid = Gtk.FlowBox()
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_max_children_per_line(2)
        grid.set_min_children_per_line(1)
        grid.set_homogeneous(True)
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)
        grid.set_can_focus(False)

        rows_added = 0
        for name in pinned_names:
            data = session_manager.get_session(name)
            if not isinstance(data, dict):
                continue

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            top.append(icon_utils.new_image_from_icon_name('view-dual-symbolic'))
            lbl = Gtk.Label(label=name, xalign=0)
            lbl.set_ellipsize(3)
            top.append(lbl)
            sub = Gtk.Label(
                label=_("{n} tab(s)").format(n=len(data.get('tabs', []))), xalign=0
            )
            sub.add_css_class('dim-label')
            sub.set_margin_start(24)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.append(top)
            inner.append(sub)

            card = Gtk.Button()
            card.set_child(inner)
            card.add_css_class('card')
            card.add_css_class('startpage-card')
            card.set_hexpand(True)
            card.set_can_focus(False)
            card.connect('clicked', lambda _b, n=name, d=data: self.window._prompt_open_session(n, d))
            grid.append(card)
            rows_added += 1
        if rows_added == 0:
            return None
        return self._section(_('Pinned Sessions'), grid)

    def _section(self, title, child):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class('startpage-heading')
        box.append(heading)
        box.append(child)
        return box

    def _populate_pinned_rows_box(self):
        """Fill _pinned_rows_box with the current pinned sections."""
        if self._pinned_rows_box is None:
            return
        child = self._pinned_rows_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._pinned_rows_box.remove(child)
            child = nxt
        section = self._build_pinned_section()
        if section is not None:
            self._pinned_rows_box.append(section)
        sessions_section = self._build_pinned_sessions_section()
        if sessions_section is not None:
            self._pinned_rows_box.append(sessions_section)

    def refresh_pinned(self):
        """Rebuild the pinned section after a pin/unpin action."""
        self._populate_pinned_rows_box()

    def _on_connection_removed(self, _manager, connection):
        """Auto-unpin a connection when it is deleted from the inventory."""
        try:
            nickname = getattr(connection, 'nickname', None)
            if nickname and self.config.is_pinned(nickname):
                self.config.unpin_connection(nickname)
                self.refresh_pinned()
        except Exception:
            pass

    def show_sidebar_hint(self):
        """Show a hint about using the sidebar to manage connections"""
        toast = Adw.Toast.new(_('Use the sidebar to add and manage your SSH connections'))
        toast.set_timeout(3)
        if hasattr(self.window, 'add_toast'):
            self.window.add_toast(toast)
        else:
            overlay = self.window.get_child()
            if isinstance(overlay, Adw.ToastOverlay):
                overlay.add_toast(toast)

    def _get_safe_current_shortcuts(self):
        """Safely get current shortcuts including user customizations from the app.
        Returns a dict: { action_name: [accel, ...] }
        """
        shortcuts = {}
        try:
            app = self.window.get_application()
            if not app:
                return shortcuts

            if hasattr(app, 'get_registered_shortcut_defaults'):
                defaults = app.get_registered_shortcut_defaults()
                if isinstance(defaults, dict):
                    shortcuts.update(defaults)

            if hasattr(app, 'config') and app.config:
                for action_name in list(shortcuts.keys()):
                    try:
                        override = app.config.get_shortcut_override(action_name)
                        if override is not None:
                            if override:
                                shortcuts[action_name] = override
                            else:
                                shortcuts.pop(action_name, None)
                    except Exception:
                        continue
        except Exception:
            pass
        return shortcuts

    def _format_accelerator_display(self, accel: str) -> str:
        """Convert a GTK accelerator like '<primary><Shift>comma' to a
        platform-friendly display like '⌘+⇧+,' or 'Ctrl+Shift+,'.
        Robust against mixed case/order like '<shift><ctrl>u'.
        """
        if not accel:
            return ''
        import re
        mac = is_macos()
        primary = '⌘' if mac else 'Ctrl'
        shift_lbl = '⇧' if mac else 'Shift'
        alt_lbl = '⌥' if mac else 'Alt'

        s = accel.strip()
        tokens = [m.group(1).lower() for m in re.finditer(r'<([^>]+)>', s)]
        key = re.sub(r'<[^>]+>', '', s).strip()

        mods = set()
        for t in tokens:
            if t in ('primary', 'meta', 'cmd', 'command'):
                mods.add('primary')
            elif t in ('ctrl', 'control'):
                mods.add('primary')
            elif t == 'shift':
                mods.add('shift')
            elif t == 'alt':
                mods.add('alt')

        parts = []
        if 'primary' in mods:
            parts.append(primary)
        if 'shift' in mods:
            parts.append(shift_lbl)
        if 'alt' in mods:
            parts.append(alt_lbl)

        key_map = {
            'comma': ',',
            'slash': '/',
            'backslash': '\\',
            'period': '.',
            'space': 'Space',
        }
        key_disp = key_map.get(key.lower(), key)
        if len(key_disp) == 1:
            key_disp = key_disp.upper()
        if key_disp:
            parts.append(key_disp)

        disp = '+'.join(parts)
        disp = re.sub(r'<[^>]+>', '', disp)
        return disp

    def _get_action_accel_display(self, shortcuts: dict, action_name: str) -> str:
        """Get the first accelerator for an action and format it for display."""
        try:
            accels = shortcuts.get(action_name)
            if not accels:
                return ''
            accel = accels[0] if isinstance(accels, (list, tuple)) else accels
            return self._format_accelerator_display(accel)
        except Exception:
            return ''

    def open_online_help(self):
        """Open online help documentation"""
        logger.debug("open_online_help called")
        import webbrowser
        try:
            webbrowser.open('https://github.com/mfat/sshpilot/wiki')
            logger.debug("Successfully opened browser")
        except Exception as e:
            logger.error(f"Failed to open browser: {e}")
            try:
                dialog = Adw.MessageDialog(
                    transient_for=self.window,
                    modal=True,
                    heading=_("Online Help"),
                    body=_("Visit the SSH Pilot documentation at:\nhttps://github.com/mfat/sshpilot/wiki")
                )
                dialog.add_response("ok", _("OK"))
                dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
                dialog.set_default_response("ok")
                dialog.set_close_response("ok")
                dialog.present()
                logger.debug("Showed fallback help dialog")
            except Exception as e2:
                logger.error(f"Failed to show help dialog: {e2}", exc_info=True)
