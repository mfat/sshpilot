"""Welcome page widget for sshPilot."""

import gi
import logging

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, Gdk, Gio, GLib

from gettext import gettext as _

from .platform_utils import is_macos

logger = logging.getLogger(__name__)


class WelcomePage(Gtk.Overlay):
    """Welcome page shown when no tabs are open."""



    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.connection_manager = window.connection_manager
        self.config = window.config
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_focus(False)

        # Placeholder populated after layout is built
        self._pinned_rows_box = None

        self.connection_manager.connect_after('connection-removed', self._on_connection_removed)
        
        # Create a scrolled window to hold all content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_can_focus(False)
        
        # Main content box
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content_box.set_margin_top(12)
        content_box.set_margin_bottom(24)
        content_box.set_valign(Gtk.Align.START)
        content_box.set_can_focus(False)
        
        # Clamp for proper width
        clamp = Adw.Clamp()
        clamp.set_maximum_size(1200)
        clamp.set_tightening_threshold(400)
        clamp.set_child(content_box)
        clamp.set_vexpand(False)
        clamp.set_can_focus(False)
        scrolled.set_child(clamp)
        self.set_child(scrolled)
        
        # Get current shortcuts for tooltips
        current_shortcuts = self._get_safe_current_shortcuts()
        
        # Welcome header - custom layout for better control
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        header_box.set_halign(Gtk.Align.CENTER)
        header_box.set_valign(Gtk.Align.START)
        header_box.set_margin_top(24)
        header_box.set_margin_bottom(24)
        header_box.set_vexpand(False)
        header_box.set_can_focus(False)
        
        # App icon
        from sshpilot import icon_utils
        icon = icon_utils.new_image_from_icon_name('io.github.mfat.sshpilot')
        icon.set_pixel_size(64)
        icon.set_can_focus(False)
        header_box.append(icon)
        
        content_box.append(header_box)

        content_box.append(self._build_action_rows_layout(current_shortcuts))

    def _build_action_rows_layout(self, current_shortcuts):
        """Build the action rows layout"""
        # Wrap in clamp to constrain width
        clamp = Adw.Clamp()
        clamp.set_maximum_size(600)
        clamp.set_tightening_threshold(400)
        clamp.set_vexpand(False)
        clamp.set_can_focus(False)
        
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Pinned connections section (populated dynamically)
        self._pinned_rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        container.append(self._pinned_rows_box)
        self._populate_pinned_rows_box()

        # Getting Started section
        getting_started_group = Adw.PreferencesGroup()
        getting_started_group.set_margin_start(12)
        getting_started_group.set_margin_end(12)
        getting_started_group.set_margin_top(12)
        getting_started_group.set_vexpand(False)
        getting_started_group.set_can_focus(False)
        getting_started_group.add_css_class('separate')
        container.append(getting_started_group)
        
        # Add New Connection action row
        new_connection_accel = self._get_action_accel_display(current_shortcuts, 'new-connection')
        new_connection_row = Adw.ActionRow()
        new_connection_row.set_title(_('Add a New Connection'))
        new_connection_row.set_activatable(True)
        new_connection_row.set_can_focus(False)
        from sshpilot import icon_utils
        prefix_img = icon_utils.new_image_from_icon_name('list-add-symbolic')
        prefix_img.set_can_focus(False)
        new_connection_row.add_prefix(prefix_img)
        if new_connection_accel:
            shortcut_label = Gtk.Label(label=new_connection_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            new_connection_row.add_suffix(shortcut_label)
        new_connection_row.connect('activated', lambda *_: self.window.get_application().activate_action('new-connection'))
        getting_started_group.add(new_connection_row)
        
        # Edit SSH Config action row
        edit_config_accel = self._get_action_accel_display(current_shortcuts, 'edit-ssh-config')
        if hasattr(self.config, 'isolated_mode') and self.config.isolated_mode:
            config_location = '~/.config/sshpilot/config'
        else:
            config_location = '~/.ssh/config'
        edit_config_row = Adw.ActionRow()
        edit_config_row.set_title(_('View and Edit SSH Config'))
        edit_config_row.set_activatable(True)
        edit_config_row.set_can_focus(False)
        prefix_img = icon_utils.new_image_from_icon_name('document-edit-symbolic')
        prefix_img.set_can_focus(False)
        edit_config_row.add_prefix(prefix_img)
        if edit_config_accel:
            shortcut_label = Gtk.Label(label=edit_config_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            edit_config_row.add_suffix(shortcut_label)
        edit_config_row.connect('activated', lambda *_: self.window.get_application().activate_action('edit-ssh-config'))
        getting_started_group.add(edit_config_row)
        
        # Local Terminal action row
        local_terminal_accel = self._get_action_accel_display(current_shortcuts, 'local-terminal')
        local_terminal_row = Adw.ActionRow()
        local_terminal_row.set_title(_('Open Local Terminal'))
        local_terminal_row.set_activatable(True)
        local_terminal_row.set_can_focus(False)
        prefix_img = icon_utils.new_image_from_icon_name('utilities-terminal-symbolic')
        prefix_img.set_can_focus(False)
        local_terminal_row.add_prefix(prefix_img)
        if local_terminal_accel:
            shortcut_label = Gtk.Label(label=local_terminal_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            local_terminal_row.add_suffix(shortcut_label)
        local_terminal_row.connect('activated', lambda *_: self.window.terminal_manager.show_local_terminal())
        getting_started_group.add(local_terminal_row)

        # Command Blocks action row
        cmd_blocks_accel = self._get_action_accel_display(
            current_shortcuts, 'toggle-command-blocks'
        )
        if not cmd_blocks_accel:
            try:
                app = self.window.get_application()
                if app:
                    accels = app.get_accels_for_action('win.toggle-command-blocks')
                    if accels:
                        cmd_blocks_accel = self._format_accelerator_display(accels[0])
            except Exception:
                pass
        if not cmd_blocks_accel:
            cmd_blocks_accel = self._format_accelerator_display('<primary><alt>s')
        command_blocks_row = Adw.ActionRow()
        command_blocks_row.set_title(_('Browse Command Snippets'))
        command_blocks_row.set_activatable(True)
        command_blocks_row.set_can_focus(False)
        prefix_img = icon_utils.new_image_from_icon_name('star-large-symbolic')
        prefix_img.set_can_focus(False)
        command_blocks_row.add_prefix(prefix_img)
        shortcut_label = Gtk.Label(label=cmd_blocks_accel)
        shortcut_label.add_css_class('dim-label')
        shortcut_label.set_can_focus(False)
        command_blocks_row.add_suffix(shortcut_label)
        command_blocks_row.connect(
            'activated', lambda *_: self._open_command_blocks_sidebar()
        )
        getting_started_group.add(command_blocks_row)
        
        # Help & Resources section
        help_group = Adw.PreferencesGroup()
        help_group.set_margin_start(12)
        help_group.set_margin_end(12)
        help_group.set_margin_top(24)
        help_group.set_vexpand(False)
        help_group.set_can_focus(False)
        help_group.add_css_class('separate')
        container.append(help_group)
        
        # Shortcuts action row
        shortcuts_accel = self._get_action_accel_display(current_shortcuts, 'shortcuts')
        shortcuts_row = Adw.ActionRow()
        shortcuts_row.set_title(_('Keyboard Shortcuts'))
        shortcuts_row.set_activatable(True)
        shortcuts_row.set_can_focus(False)
        prefix_img = icon_utils.new_image_from_icon_name('preferences-desktop-keyboard-symbolic')
        prefix_img.set_can_focus(False)
        shortcuts_row.add_prefix(prefix_img)
        if shortcuts_accel:
            shortcut_label = Gtk.Label(label=shortcuts_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcuts_row.add_suffix(shortcut_label)
        shortcuts_row.connect('activated', lambda *_: self.window.show_shortcuts_window())
        help_group.add(shortcuts_row)
        
        # Online help action row
        help_accel = self._get_action_accel_display(current_shortcuts, 'help')
        help_row = Adw.ActionRow()
        help_row.set_title(_('Online Documentation'))
        help_row.set_activatable(True)
        help_row.set_can_focus(False)
        prefix_img = icon_utils.new_image_from_icon_name('help-browser-symbolic')
        prefix_img.set_can_focus(False)
        help_row.add_prefix(prefix_img)
        if help_accel:
            shortcut_label = Gtk.Label(label=help_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            help_row.add_suffix(shortcut_label)
        help_row.connect('activated', lambda *_: self.open_online_help())
        help_group.add(help_row)
        
        # Set container as child of clamp
        clamp.set_child(container)
        
        return clamp

    def _open_command_blocks_sidebar(self) -> None:
        """Show the command blocks right sidebar from the start page."""
        if hasattr(self.window, '_toggle_command_blocks_panel'):
            self.window._toggle_command_blocks_panel(True)
    
    # --- Pinned connections ---

    def _build_pinned_section(self) -> 'Adw.PreferencesGroup | None':
        """Build an Adw.PreferencesGroup of pinned hosts, or None if none are pinned."""
        from sshpilot import icon_utils
        pinned_nicknames = self.config.get_pinned_nicknames()
        if not pinned_nicknames:
            return None

        conn_map = {c.nickname: c for c in self.connection_manager.connections}
        rows_added = 0
        group = Adw.PreferencesGroup(title=_("Pinned Connections"))
        group.set_margin_start(12)
        group.set_margin_end(12)
        group.set_margin_top(12)
        group.set_margin_bottom(4)
        group.set_can_focus(False)
        group.add_css_class('separate')

        for nickname in pinned_nicknames:
            conn = conn_map.get(nickname)
            if conn is None:
                continue
            row = Adw.ActionRow()
            row.set_title(nickname)
            host_label = getattr(conn, 'host', '') or getattr(conn, 'hostname', '')
            username = getattr(conn, 'username', '')
            if username and host_label:
                row.set_subtitle(f"{username}@{host_label}")
            elif host_label:
                row.set_subtitle(host_label)
            row.set_activatable(True)
            row.set_can_focus(False)

            prefix_img = icon_utils.new_image_from_icon_name('network-server-symbolic')
            prefix_img.set_can_focus(False)
            row.add_prefix(prefix_img)

            connect_btn = Gtk.Button(label=_("Connect"))
            connect_btn.set_valign(Gtk.Align.CENTER)
            connect_btn.add_css_class('suggested-action')
            connect_btn.add_css_class('pill')
            connect_btn.set_can_focus(False)
            _conn = conn
            connect_btn.connect('clicked', lambda _b, c=_conn: self.window.terminal_manager.connect_to_host(c))
            row.add_suffix(connect_btn)

            row.connect('activated', lambda _r, c=_conn: self.window.terminal_manager.connect_to_host(c))
            group.add(row)
            rows_added += 1

        return group if rows_added > 0 else None

    def _build_pinned_sessions_section(self) -> 'Adw.PreferencesGroup | None':
        """Build an Adw.PreferencesGroup of pinned sessions, or None if none are pinned."""
        from sshpilot import icon_utils
        session_manager = getattr(self.window, 'session_manager', None)
        if session_manager is None:
            return None
        pinned_names = session_manager.get_pinned_session_names()
        if not pinned_names:
            return None

        group = Adw.PreferencesGroup(title=_("Pinned Sessions"))
        group.set_margin_start(12)
        group.set_margin_end(12)
        group.set_margin_top(12)
        group.set_margin_bottom(4)
        group.set_can_focus(False)
        group.add_css_class('separate')

        rows_added = 0
        for name in pinned_names:
            data = session_manager.get_session(name)
            if not isinstance(data, dict):
                continue
            row = Adw.ActionRow()
            row.set_title(name)
            tab_count = len(data.get('tabs', []))
            row.set_subtitle(_("{n} tab(s)").format(n=tab_count))
            row.set_activatable(True)
            row.set_can_focus(False)

            prefix_img = icon_utils.new_image_from_icon_name('view-dual-symbolic')
            prefix_img.set_can_focus(False)
            row.add_prefix(prefix_img)

            open_btn = Gtk.Button(label=_("Open"))
            open_btn.set_valign(Gtk.Align.CENTER)
            open_btn.add_css_class('suggested-action')
            open_btn.add_css_class('pill')
            open_btn.set_can_focus(False)
            open_btn.connect('clicked', lambda _b, n=name, d=data: self.window._prompt_open_session(n, d))
            row.add_suffix(open_btn)

            row.connect('activated', lambda _r, n=name, d=data: self.window._prompt_open_session(n, d))
            group.add(row)
            rows_added += 1

        return group if rows_added > 0 else None

    def _populate_pinned_rows_box(self):
        """Fill _pinned_rows_box with the current pinned group (rows layout)."""
        if self._pinned_rows_box is None:
            return
        child = self._pinned_rows_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._pinned_rows_box.remove(child)
            child = nxt
        group = self._build_pinned_section()
        if group is not None:
            self._pinned_rows_box.append(group)
        sessions_group = self._build_pinned_sessions_section()
        if sessions_group is not None:
            self._pinned_rows_box.append(sessions_group)

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
            # Fallback for older API
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

            # Get default registered shortcuts if available
            if hasattr(app, 'get_registered_shortcut_defaults'):
                defaults = app.get_registered_shortcut_defaults()
                if isinstance(defaults, dict):
                    shortcuts.update(defaults)

            # Apply user overrides from config if available
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
        # Extract all <token> in order
        tokens = [m.group(1).lower() for m in re.finditer(r'<([^>]+)>', s)]
        # Remove all <...> to get the key part
        key = re.sub(r'<[^>]+>', '', s).strip()

        # Map tokens to normalized set
        mods = set()
        for t in tokens:
            if t in ('primary', 'meta', 'cmd', 'command'):  # treat as primary
                mods.add('primary')
            elif t in ('ctrl', 'control'):
                mods.add('primary')  # normalize to primary for display
            elif t == 'shift':
                mods.add('shift')
            elif t == 'alt':
                mods.add('alt')

        # Build parts in consistent order
        parts = []
        if 'primary' in mods:
            parts.append(primary)
        if 'shift' in mods:
            parts.append(shift_lbl)
        if 'alt' in mods:
            parts.append(alt_lbl)

        # Map common key names
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
        # Fallback safety: strip any lingering angle brackets
        disp = re.sub(r'<[^>]+>', '', disp)
        return disp

    def _get_action_accel_display(self, shortcuts: dict, action_name: str) -> str:
        """Get the first accelerator for an action and format it for display."""
        try:
            accels = shortcuts.get(action_name)
            if not accels:
                return ''
            # If it's a list, use the first; if it's a string, use directly
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
            # Fallback: show a dialog with the URL
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
