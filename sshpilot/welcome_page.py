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
.startpage-chip {
    padding: 7px 16px;
    border-radius: 9999px;
    min-height: 0;
    background: transparent;
    border: 1px solid alpha(@window_fg_color, 0.18);
    box-shadow: none;
}
.startpage-chip:hover { background: alpha(@window_fg_color, 0.08); }
.startpage-chip:active { background: alpha(@window_fg_color, 0.14); }
.startpage-mono {
    font-family: monospace;
    font-size: 0.85em;
}
.startpage-recent-head {
    font-size: 0.8em;
    font-weight: normal;
    opacity: 0.55;
    padding-bottom: 8px;
    border-bottom: 1px solid alpha(@window_fg_color, 0.12);
}
.startpage-recent-row, .startpage-recent-row label { font-weight: normal; }
.startpage-recent-row {
    padding: 9px 2px;
    background: transparent;
    box-shadow: none;
    border-radius: 0;
    min-height: 0;
    border-bottom: 1px solid alpha(@window_fg_color, 0.08);
}
.startpage-recent-row:hover { background: alpha(@window_fg_color, 0.06); }
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

        self._pinned_box = None

        self.connection_manager.connect_after('connection-removed', self._on_connection_removed)

        current_shortcuts = self._get_safe_current_shortcuts()
        self._shortcuts = current_shortcuts

        self.set_child(self._build_minimal_view(current_shortcuts))

        # Footer links pinned to the bottom-centre of the page
        footer = self._build_footer()
        footer.set_halign(Gtk.Align.CENTER)
        footer.set_valign(Gtk.Align.END)
        footer.set_margin_bottom(32)
        self.add_overlay(footer)

    # --- New connection pill ---

    def _build_new_connection_pill(self):
        btn_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_content.append(icon_utils.new_image_from_icon_name('list-add-symbolic'))
        btn_content.append(Gtk.Label(label=_('New connection')))
        new_btn = Gtk.Button()
        new_btn.set_child(btn_content)
        new_btn.add_css_class('suggested-action')
        new_btn.add_css_class('pill')
        new_btn.set_can_focus(False)
        new_btn.set_tooltip_text(self._tooltip(_('New connection'), 'new-connection'))
        new_btn.connect('clicked', lambda *_a: self.window.get_application().activate_action('new-connection'))
        return new_btn

    # --- Collapsible extra actions ---

    def _build_extras(self, current_shortcuts):
        """Secondary actions + pinned sections, hidden behind a revealer."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_top(8)

        pill = self._build_new_connection_pill()
        pill.set_halign(Gtk.Align.CENTER)
        pill.set_margin_bottom(16)
        box.append(pill)

        # Shared size group keeps every chip (both rows) the same width.
        chip_sizes = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        chips = self._make_chip_row([
            ('document-edit-symbolic', _('Edit SSH Configuration'),
             lambda _b: self.window.get_application().activate_action('edit-ssh-config'),
             'edit-ssh-config'),
            ('folder-remote-symbolic', _('SFTP File Manager'),
             self._open_file_manager,
             'manage-files'),
            ('system-run-symbolic', _('Snippets'),
             lambda _b: self._open_command_blocks_sidebar(),
             'toggle-command-blocks'),
        ], chip_sizes)
        chips.set_margin_bottom(8)
        box.append(chips)

        more_chips = self._make_chip_row([
            ('network-server-symbolic', _('Manage Known hosts'),
             lambda _b: self.window.on_edit_known_hosts_action(None, None),
             'edit-known-hosts'),
            ('dialog-password-symbolic', _('Authorized keys'),
             lambda _b: self.window.on_manage_local_authorized_keys_action(None, None),
             'manage-local-authorized-keys'),
            ('brand-docker-symbolic', _('Docker Console'),
             self._open_docker_console,
             None),
        ], chip_sizes)
        more_chips.set_margin_top(8)
        box.append(more_chips)

        return box

    # --- Main view ---

    def _build_minimal_view(self, current_shortcuts):
        """Compact start page: clickable terminal hero, connection summary and a
        Recent list, with the secondary actions tucked behind a revealer."""
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_can_focus(False)

        connections = list(getattr(self.connection_manager, 'connections', []) or [])
        try:
            group_count = len(self.window.group_manager.get_all_groups())
        except Exception:
            group_count = 0

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_halign(Gtk.Align.FILL)
        inner.set_hexpand(True)
        inner.set_valign(Gtk.Align.CENTER)
        inner.set_margin_top(24)
        inner.set_margin_bottom(24)

        hero_btn = Gtk.Button()
        hero_btn.set_child(icon_utils.new_image_from_icon_name('utilities-terminal-symbolic', 28))
        hero_btn.add_css_class('flat')
        hero_btn.set_halign(Gtk.Align.CENTER)
        hero_btn.set_can_focus(False)
        hero_btn.set_tooltip_text(self._tooltip(_('Open Local Terminal'), 'local-terminal'))
        hero_btn.connect('clicked', lambda *_a: self.window.terminal_manager.show_local_terminal())
        inner.append(hero_btn)

        title = Gtk.Label(label='SSH Pilot')
        title.add_css_class('title-3')
        title.set_margin_top(12)
        inner.append(title)

        summary = Gtk.Label(
            label=_('{n} connections across {m} groups').format(
                n=len(connections), m=group_count)
        )
        summary.add_css_class('dim-label')
        summary.set_margin_top(2)
        summary.set_margin_bottom(28)
        inner.append(summary)

        lists = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        lists.set_hexpand(True)
        lists.append(self._build_min_connection_list(connections))
        self._pinned_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._pinned_box.set_hexpand(True)
        lists.append(self._pinned_box)
        self._populate_pinned_box()

        clamp = Adw.Clamp()
        clamp.set_maximum_size(720)
        clamp.set_tightening_threshold(600)
        clamp.set_hexpand(True)
        clamp.set_child(lists)
        inner.append(clamp)

        # Secondary actions + pinned sections, hidden by default
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_child(self._build_extras(current_shortcuts))
        revealer.set_reveal_child(False)

        chevron = icon_utils.new_image_from_icon_name('pan-down-symbolic')
        more_btn = Gtk.Button()
        more_btn.set_child(chevron)
        more_btn.add_css_class('flat')
        more_btn.add_css_class('circular')
        more_btn.set_halign(Gtk.Align.CENTER)
        more_btn.set_margin_top(16)
        more_btn.set_can_focus(False)
        more_btn.set_tooltip_text(_('Show more actions'))
        more_btn.connect('clicked', self._on_toggle_more, revealer, chevron)
        inner.append(more_btn)
        inner.append(revealer)

        scrolled.set_child(inner)
        return scrolled

    # --- Shared row/section widgets ---

    def _min_row(self, title, subtitle, on_click):
        """A single Recent/Pinned row: title left, subtitle (mono) right."""
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = Gtk.Label(label=title or '', xalign=0)
        name.set_ellipsize(3)
        name.set_hexpand(True)
        line.append(name)
        if subtitle:
            addr = Gtk.Label(label=subtitle, xalign=1)
            addr.add_css_class('startpage-mono')
            addr.add_css_class('dim-label')
            addr.set_ellipsize(3)
            line.append(addr)

        btn = Gtk.Button()
        btn.set_child(line)
        btn.add_css_class('startpage-recent-row')
        btn.set_can_focus(False)
        btn.connect('clicked', on_click)
        return btn

    def _min_section(self, heading_text, rows):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_hexpand(True)
        heading = Gtk.Label(label=heading_text, xalign=0)
        heading.set_hexpand(True)
        heading.add_css_class('startpage-recent-head')
        heading.set_margin_bottom(4)
        box.append(heading)
        for row in rows:
            box.append(row)
        return box

    @staticmethod
    def _conn_target(conn):
        # Prefer the real hostname/IP; ``host`` is often just the config alias.
        host = getattr(conn, 'hostname', '') or getattr(conn, 'host', '')
        user = getattr(conn, 'username', '')
        return f"{user}@{host}" if user and host else host

    def _build_min_connection_list(self, connections):
        if not connections:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            box.set_hexpand(True)
            heading = Gtk.Label(label=_('Recent'), xalign=0)
            heading.set_hexpand(True)
            heading.add_css_class('startpage-recent-head')
            heading.set_margin_bottom(4)
            box.append(heading)
            empty = Gtk.Label(label=_('No connections yet'), xalign=0)
            empty.add_css_class('dim-label')
            empty.set_margin_top(8)
            box.append(empty)
            return box

        def _last_used(conn):
            try:
                return self.config.get_connection_meta(conn.nickname).get('last_used', 0) or 0
            except Exception:
                return 0

        recent = sorted(connections, key=_last_used, reverse=True)[:4]
        rows = [
            self._min_row(
                getattr(conn, 'nickname', ''), self._conn_target(conn),
                lambda _b, c=conn: self.window.terminal_manager.connect_to_host(c))
            for conn in recent
        ]
        return self._min_section(_('Recent'), rows)

    def _populate_pinned_box(self):
        """Fill the Pinned section (rows, styled like Recent) below the list."""
        box = getattr(self, '_pinned_box', None)
        if box is None:
            return
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

        conn_map = {c.nickname: c for c in self.connection_manager.connections}
        pinned = [conn_map[n] for n in self.config.get_pinned_nicknames() if n in conn_map][:4]
        if pinned:
            rows = [
                self._min_row(
                    conn.nickname, self._conn_target(conn),
                    lambda _b, c=conn: self.window.terminal_manager.connect_to_host(c))
                for conn in pinned
            ]
            box.append(self._min_section(_('Pinned'), rows))

    def _build_footer(self):
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        shortcuts_link = Gtk.Button(label=_('Keyboard shortcuts'))
        shortcuts_link.add_css_class('flat')
        shortcuts_link.add_css_class('dim-label')
        shortcuts_link.set_can_focus(False)
        shortcuts_link.set_tooltip_text(self._tooltip(_('Keyboard shortcuts'), 'shortcuts'))
        shortcuts_link.connect('clicked', lambda *_a: self.window.show_shortcuts_window())
        footer.append(shortcuts_link)
        sep = Gtk.Label(label='·')
        sep.add_css_class('dim-label')
        footer.append(sep)
        docs_link = Gtk.Button(label=_('Online documentation'))
        docs_link.add_css_class('flat')
        docs_link.add_css_class('dim-label')
        docs_link.set_can_focus(False)
        docs_link.set_tooltip_text(self._tooltip(_('Online documentation'), 'help'))
        docs_link.connect('clicked', lambda *_a: self.open_online_help())
        footer.append(docs_link)
        return footer

    def _make_chip_row(self, actions, size_group=None):
        """Build a homogeneous 3-column FlowBox of equal-width chip buttons."""
        fb = Gtk.FlowBox()
        fb.set_selection_mode(Gtk.SelectionMode.NONE)
        fb.set_halign(Gtk.Align.CENTER)
        fb.set_max_children_per_line(3)
        fb.set_min_children_per_line(3)
        fb.set_column_spacing(8)
        fb.set_row_spacing(8)
        fb.set_can_focus(False)
        for icon_name, label, cb, action_name in actions:
            fb.append(self._build_chip(icon_name, label, cb, action_name, size_group))
        return fb

    def _build_chip(self, icon_name, label, callback, action_name=None, size_group=None):
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.set_halign(Gtk.Align.CENTER)
        content.append(icon_utils.new_image_from_icon_name(icon_name))
        content.append(Gtk.Label(label=label))
        btn = Gtk.Button()
        btn.set_child(content)
        btn.add_css_class('startpage-chip')
        btn.set_can_focus(False)
        if size_group is not None:
            size_group.add_widget(btn)
        btn.set_tooltip_text(self._tooltip(label, action_name))
        # clicked passes the button as first arg; callbacks accept it (used as
        # the popover anchor for Copy key to server).
        btn.connect('clicked', callback)
        return btn

    def _tooltip(self, text, action_name=None):
        """Label plus the action's current keyboard shortcut, if one is set."""
        accel = self._get_action_accel_display(getattr(self, '_shortcuts', {}) or {}, action_name) if action_name else ''
        return f"{text}  ({accel})" if accel else text

    def _on_toggle_more(self, button, revealer, chevron):
        reveal = not revealer.get_reveal_child()
        revealer.set_reveal_child(reveal)
        icon_utils.set_icon_from_name(chevron, 'pan-up-symbolic' if reveal else 'pan-down-symbolic')
        button.set_tooltip_text(_('Show fewer actions') if reveal else _('Show more actions'))

    def _pick_host(self, anchor, on_selected):
        """Show the host picker, or prompt to create a connection if none exist."""
        if not list(getattr(self.connection_manager, 'connections', [])):
            self._prompt_create_connection()
            return
        from .host_picker import show_host_picker
        show_host_picker(self.window, anchor, on_selected, toast=self._show_toast)

    def _prompt_create_connection(self):
        """No hosts yet — offer to create one."""
        dialog = Adw.MessageDialog(
            transient_for=self.window,
            modal=True,
            heading=_('No Connections Yet'),
            body=_('Create a connection first to use this action.'),
        )
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('create', _('New Connection'))
        dialog.set_response_appearance('create', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('create')
        dialog.set_close_response('cancel')
        dialog.connect(
            'response',
            lambda _d, resp: self.window.get_application().activate_action('new-connection')
            if resp == 'create' else None,
        )
        dialog.present()

    def _open_docker_console(self, anchor):
        """Open the Docker Console page (it has its own host chooser), or, if the
        docker-manager plugin is disabled, prompt the user to enable it.

        Mirrors the Tools-menu entry: open_page honors the page's on_activate.
        The page is only registered while the plugin is active, so its presence
        is the enabled signal.
        """
        ui = getattr(getattr(self.window, 'plugin_host', None), 'ui', None)
        manager_id = 'docker-manager:manager'
        if ui is None or manager_id not in ui.page_ids_for_plugin('docker-manager'):
            self._prompt_enable_plugin(_('Docker Console'))
            return
        ui.open_page(manager_id)

    def _prompt_enable_plugin(self, plugin_name):
        """Tell the user the plugin is off and offer to open Settings."""
        dialog = Adw.MessageDialog(
            transient_for=self.window,
            modal=True,
            heading=_('Plugin Disabled'),
            body=_('The %s plugin is disabled. Enable it from Settings ▸ Plugins to use this feature.') % plugin_name,
        )
        dialog.add_response('close', _('Close'))
        dialog.add_response('open', _('Open Settings'))
        dialog.set_response_appearance('open', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('open')
        dialog.set_close_response('close')
        dialog.connect(
            'response',
            lambda _d, resp: self.window.show_preferences() if resp == 'open' else None,
        )
        dialog.present()

    def _open_file_manager(self, anchor):
        """Pick a host, then open the SFTP file manager for it."""
        self._pick_host(anchor, self._open_file_manager_for)

    def _open_file_manager_for(self, connection):
        try:
            self.window._open_manage_files_for_connection(connection)
        except Exception:
            logger.error("Failed to open file manager", exc_info=True)

    def _show_toast(self, message):
        try:
            self.window.add_toast(Adw.Toast.new(message))
        except Exception:
            pass

    def _open_command_blocks_sidebar(self) -> None:
        """Toggle the command blocks right sidebar from the start page."""
        if hasattr(self.window, '_toggle_command_blocks_panel'):
            self.window._toggle_command_blocks_panel()

    # --- Pinned connections ---

    def refresh_pinned(self):
        """Rebuild the pinned section after a pin/unpin action."""
        self._populate_pinned_box()

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
