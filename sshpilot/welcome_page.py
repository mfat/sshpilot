"""Welcome page widget for sshPilot."""

import gi
import os
import re
import shlex
import logging

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, Gdk, Gio, GLib

from gettext import gettext as _

from .connection_manager import Connection
from .platform_utils import is_macos, get_ssh_dir

logger = logging.getLogger(__name__)


SSH_OPTIONS_EXPECTING_ARGUMENT = {
    "-b",
    "-B",
    "-c",
    "-D",
    "-E",
    "-e",
    "-F",
    "-I",
    "-J",
    "-L",
    "-l",
    "-m",
    "-O",
    "-o",
    "-p",
    "-Q",
    "-R",
    "-S",
    "-W",
    "-w",
}


# An address token is either an IPv6 literal in brackets [::1] or any
# sequence of characters that contains no unbracketed colon (hostname/IPv4).
_ADDR = r'(?:\[[^\]]*\]|[^\[\]:]+)'
_PORT = r'\d+'

# Matches [bind_addr:]port:host:hostport with full bracket-awareness.
_FORWARD_RE = re.compile(
    r'^(?:(' + _ADDR + r'):)?'   # optional bind_addr:
    r'(' + _PORT + r')'           # listen_port
    r':(' + _ADDR + r')'          # remote host
    r':(' + _PORT + r')$'         # remote port
)

# Matches [bind_addr:]port for -D (dynamic SOCKS proxy).
_DYNAMIC_RE = re.compile(
    r'^(?:(' + _ADDR + r'):)?'   # optional bind_addr:
    r'(' + _PORT + r')$'          # port
)


def _parse_forward_spec(spec: str, fwd_type: str):
    """Parse -L/-R spec [bind_addr:]port:host:hostport into a forwarding_rules dict.

    Handles IPv6 literals in brackets (e.g. [::1]:8080:localhost:80).
    Returns None if the spec cannot be parsed; callers should preserve the raw
    spec in unparsed_args so the rule is not silently lost.
    """
    m = _FORWARD_RE.match(spec)
    if not m:
        return None
    bind_addr, listen_port, remote_host, remote_port = m.groups()
    rule = {'type': fwd_type, 'enabled': True,
            'listen_addr': bind_addr or 'localhost', 'listen_port': int(listen_port)}
    if fwd_type == 'local':
        rule['remote_host'] = remote_host
        rule['remote_port'] = int(remote_port)
    else:
        rule['local_host'] = remote_host
        rule['local_port'] = int(remote_port)
    return rule


def _parse_dynamic_spec(spec: str):
    """Parse -D spec [bind_addr:]port into a forwarding_rules dict.

    Handles IPv6 bind addresses in brackets (e.g. [::1]:1080).
    Returns None if the spec cannot be parsed.
    """
    m = _DYNAMIC_RE.match(spec)
    if not m:
        return None
    bind_addr, port = m.groups()
    return {'type': 'dynamic', 'enabled': True,
            'listen_addr': bind_addr or 'localhost', 'listen_port': int(port)}


def _append_extra_config(data: dict, line: str) -> None:
    """Append a SSH-config-syntax 'Key value' line to extra_ssh_config."""
    existing = data.get("extra_ssh_config", "")
    data["extra_ssh_config"] = (existing + "\n" + line).lstrip("\n")


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

        # Placeholders populated after layouts are built
        self._pinned_rows_box = None
        self._pinned_cards_box = None

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
        
        # Welcome title
        title_label = Gtk.Label()
        title_label.set_text(_('Welcome to SSH Pilot'))
        title_label.add_css_class('title-1')
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_can_focus(False)
        header_box.append(title_label)
        
        # Description
        desc_label = Gtk.Label()
        #desc_label.set_text(_('A modern SSH connection manager with integrated terminal'))
        desc_label.add_css_class('dim-label')
        desc_label.set_halign(Gtk.Align.CENTER)
        desc_label.set_wrap(True)
        desc_label.set_justify(Gtk.Justification.CENTER)
        desc_label.set_can_focus(False)
        header_box.append(desc_label)
        
        content_box.append(header_box)
        
        # Stack to hold both layouts
        self.layout_stack = Gtk.Stack()
        self.layout_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.layout_stack.set_transition_duration(200)
        content_box.append(self.layout_stack)
        
        # Build both layouts
        cards_widget = self._build_cards_layout(current_shortcuts)
        rows_widget = self._build_action_rows_layout(current_shortcuts)
        
        self.layout_stack.add_named(cards_widget, 'cards')
        self.layout_stack.add_named(rows_widget, 'rows')
        
        # Load saved layout preference
        saved_layout = self.config.get_setting('ui.welcome_page_layout', 'rows')
        use_cards = saved_layout == 'cards'
        
        # Layout toggle button in top right (as overlay)
        self.layout_toggle = Gtk.ToggleButton()
        self.layout_toggle.set_active(use_cards)  # active = cards, inactive = rows
        self.layout_toggle.connect('toggled', self._on_layout_toggle_changed)
        self.layout_toggle.set_margin_start(12)
        self.layout_toggle.set_margin_end(12)
        self.layout_toggle.set_margin_top(12)
        self.layout_toggle.set_halign(Gtk.Align.END)
        self.layout_toggle.set_valign(Gtk.Align.START)
        self.add_overlay(self.layout_toggle)
        
        # Set initial layout and update toggle icon
        from sshpilot import icon_utils
        if use_cards:
            self.layout_stack.set_visible_child_name('cards')
            icon_utils.set_button_icon(self.layout_toggle, 'view-list-symbolic')
            self.layout_toggle.set_tooltip_text(_('Switch to list view'))
        else:
            self.layout_stack.set_visible_child_name('rows')
            icon_utils.set_button_icon(self.layout_toggle, 'view-grid-symbolic')
            self.layout_toggle.set_tooltip_text(_('Switch to grid view'))
    
    def _build_cards_layout(self, current_shortcuts):
        """Build the cards grid layout"""
        # Outer box so the pinned section can sit above the cards grid
        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._pinned_cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer_box.append(self._pinned_cards_box)
        self._populate_pinned_cards_box()

        cards_grid = Gtk.FlowBox()
        cards_grid.set_selection_mode(Gtk.SelectionMode.NONE)
        cards_grid.set_max_children_per_line(3)
        cards_grid.set_min_children_per_line(1)
        cards_grid.set_column_spacing(12)
        cards_grid.set_row_spacing(12)
        cards_grid.set_margin_start(12)
        cards_grid.set_margin_end(12)
        cards_grid.set_margin_top(12)
        cards_grid.set_homogeneous(True)
        
        # Quick Connect card
        quick_connect_accel = self._get_action_accel_display(current_shortcuts, 'quick-connect')
        quick_connect_btn = Gtk.Button()
        quick_connect_btn.set_can_focus(False)
        quick_connect_btn.add_css_class('card')
        quick_connect_btn.set_size_request(120, 120)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(8)
        card_box.set_margin_end(8)
        card_box.set_margin_top(8)
        card_box.set_margin_bottom(8)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        from sshpilot import icon_utils
        prefix_img = icon_utils.new_image_from_icon_name('network-server-symbolic')
        prefix_img.set_can_focus(False)
        prefix_img.set_pixel_size(32)
        card_box.append(prefix_img)
        
        title_label = Gtk.Label(label=_('Quick Connect'))
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.add_css_class('title-4')
        card_box.append(title_label)
        
        if quick_connect_accel:
            shortcut_label = Gtk.Label(label=quick_connect_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcut_label.set_halign(Gtk.Align.CENTER)
            card_box.append(shortcut_label)
        
        quick_connect_btn.set_child(card_box)
        quick_connect_btn.connect('clicked', lambda *_: self.on_quick_connect_clicked(None))
        cards_grid.append(quick_connect_btn)
        
        # Add New Connection card
        new_connection_accel = self._get_action_accel_display(current_shortcuts, 'new-connection')
        new_connection_btn = Gtk.Button()
        new_connection_btn.set_can_focus(False)
        new_connection_btn.add_css_class('card')
        new_connection_btn.set_size_request(120, 120)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(8)
        card_box.set_margin_end(8)
        card_box.set_margin_top(8)
        card_box.set_margin_bottom(8)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = icon_utils.new_image_from_icon_name('list-add-symbolic')
        prefix_img.set_can_focus(False)
        prefix_img.set_pixel_size(32)
        card_box.append(prefix_img)
        
        title_label = Gtk.Label(label=_('Add a New Connection'))
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.add_css_class('title-4')
        card_box.append(title_label)
        
        if new_connection_accel:
            shortcut_label = Gtk.Label(label=new_connection_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcut_label.set_halign(Gtk.Align.CENTER)
            card_box.append(shortcut_label)
        
        new_connection_btn.set_child(card_box)
        new_connection_btn.connect('clicked', lambda *_: self.window.get_application().activate_action('new-connection'))
        cards_grid.append(new_connection_btn)
        
        # Edit SSH Config action row
        edit_config_accel = self._get_action_accel_display(current_shortcuts, 'edit-ssh-config')
        
        # Check if using isolated mode or default SSH config
        if hasattr(self.config, 'isolated_mode') and self.config.isolated_mode:
            config_location = '~/.config/sshpilot/config'
        else:
            config_location = '~/.ssh/config'
        
        edit_config_btn = Gtk.Button()
        edit_config_btn.set_can_focus(False)
        edit_config_btn.add_css_class('card')
        edit_config_btn.set_size_request(120, 120)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(8)
        card_box.set_margin_end(8)
        card_box.set_margin_top(8)
        card_box.set_margin_bottom(8)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = icon_utils.new_image_from_icon_name('document-edit-symbolic')
        prefix_img.set_can_focus(False)
        prefix_img.set_pixel_size(32)
        card_box.append(prefix_img)
        
        title_label = Gtk.Label(label=_('View and Edit SSH Config'))
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.add_css_class('title-4')
        card_box.append(title_label)
        
        if edit_config_accel:
            shortcut_label = Gtk.Label(label=edit_config_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcut_label.set_halign(Gtk.Align.CENTER)
            card_box.append(shortcut_label)
        
        edit_config_btn.set_child(card_box)
        edit_config_btn.connect('clicked', lambda *_: self.window.get_application().activate_action('edit-ssh-config'))
        cards_grid.append(edit_config_btn)
        
        # Local Terminal card
        local_terminal_accel = self._get_action_accel_display(current_shortcuts, 'local-terminal')
        local_terminal_btn = Gtk.Button()
        local_terminal_btn.set_can_focus(False)
        local_terminal_btn.add_css_class('card')
        local_terminal_btn.set_size_request(120, 120)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(8)
        card_box.set_margin_end(8)
        card_box.set_margin_top(8)
        card_box.set_margin_bottom(8)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = icon_utils.new_image_from_icon_name('utilities-terminal-symbolic')
        prefix_img.set_can_focus(False)
        prefix_img.set_pixel_size(32)
        card_box.append(prefix_img)
        
        title_label = Gtk.Label(label=_('Open Local Terminal'))
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.add_css_class('title-4')
        card_box.append(title_label)
        
        if local_terminal_accel:
            shortcut_label = Gtk.Label(label=local_terminal_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcut_label.set_halign(Gtk.Align.CENTER)
            card_box.append(shortcut_label)
        
        local_terminal_btn.set_child(card_box)
        local_terminal_btn.connect('clicked', lambda *_: self.window.terminal_manager.show_local_terminal())
        cards_grid.append(local_terminal_btn)
        
        # Keyboard Shortcuts card
        shortcuts_accel = self._get_action_accel_display(current_shortcuts, 'shortcuts')
        shortcuts_btn = Gtk.Button()
        shortcuts_btn.set_can_focus(False)
        shortcuts_btn.add_css_class('card')
        shortcuts_btn.set_size_request(120, 120)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(8)
        card_box.set_margin_end(8)
        card_box.set_margin_top(8)
        card_box.set_margin_bottom(8)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = icon_utils.new_image_from_icon_name('preferences-desktop-keyboard-symbolic')
        prefix_img.set_can_focus(False)
        prefix_img.set_pixel_size(32)
        card_box.append(prefix_img)
        
        title_label = Gtk.Label(label=_('Keyboard Shortcuts'))
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.add_css_class('title-4')
        card_box.append(title_label)
        
        if shortcuts_accel:
            shortcut_label = Gtk.Label(label=shortcuts_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcut_label.set_halign(Gtk.Align.CENTER)
            card_box.append(shortcut_label)
        
        shortcuts_btn.set_child(card_box)
        shortcuts_btn.connect('clicked', lambda *_: self.window.show_shortcuts_window())
        cards_grid.append(shortcuts_btn)
        
        # Online Documentation card
        help_accel = self._get_action_accel_display(current_shortcuts, 'help')
        help_btn = Gtk.Button()
        help_btn.set_can_focus(False)
        help_btn.add_css_class('card')
        help_btn.set_size_request(120, 120)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(8)
        card_box.set_margin_end(8)
        card_box.set_margin_top(8)
        card_box.set_margin_bottom(8)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = icon_utils.new_image_from_icon_name('help-browser-symbolic')
        prefix_img.set_can_focus(False)
        prefix_img.set_pixel_size(32)
        card_box.append(prefix_img)
        
        title_label = Gtk.Label(label=_('Online Documentation'))
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.add_css_class('title-4')
        card_box.append(title_label)
        
        if help_accel:
            shortcut_label = Gtk.Label(label=help_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcut_label.set_halign(Gtk.Align.CENTER)
            card_box.append(shortcut_label)
        
        help_btn.set_child(card_box)
        help_btn.connect('clicked', lambda *_: self.open_online_help())
        cards_grid.append(help_btn)
        
        # About card
        about_accel = self._get_action_accel_display(current_shortcuts, 'about')
        about_btn = Gtk.Button()
        about_btn.set_can_focus(False)
        about_btn.add_css_class('card')
        about_btn.set_size_request(120, 120)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(8)
        card_box.set_margin_end(8)
        card_box.set_margin_top(8)
        card_box.set_margin_bottom(8)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = icon_utils.new_image_from_icon_name('help-about-symbolic')
        prefix_img.set_can_focus(False)
        prefix_img.set_pixel_size(32)
        card_box.append(prefix_img)
        
        title_label = Gtk.Label(label=_('About'))
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.add_css_class('title-4')
        card_box.append(title_label)
        
        if about_accel:
            shortcut_label = Gtk.Label(label=about_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcut_label.set_halign(Gtk.Align.CENTER)
            card_box.append(shortcut_label)
        
        about_btn.set_child(card_box)
        about_btn.connect('clicked', lambda *_: self.window.get_application().activate_action('about'))
        cards_grid.append(about_btn)

        outer_box.append(cards_grid)
        return outer_box
    
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
        
        # Quick Connect action row
        quick_connect_accel = self._get_action_accel_display(current_shortcuts, 'quick-connect')
        quick_connect_row = Adw.ActionRow()
        quick_connect_row.set_title(_('Quick Connect'))
        quick_connect_row.set_activatable(True)
        quick_connect_row.set_can_focus(False)
        from sshpilot import icon_utils
        prefix_img = icon_utils.new_image_from_icon_name('network-server-symbolic')
        prefix_img.set_can_focus(False)
        quick_connect_row.add_prefix(prefix_img)
        if quick_connect_accel:
            shortcut_label = Gtk.Label(label=quick_connect_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            quick_connect_row.add_suffix(shortcut_label)
        quick_connect_row.connect('activated', lambda *_: self.on_quick_connect_clicked(None))
        getting_started_group.add(quick_connect_row)
        
        # Add New Connection action row
        new_connection_accel = self._get_action_accel_display(current_shortcuts, 'new-connection')
        new_connection_row = Adw.ActionRow()
        new_connection_row.set_title(_('Add a New Connection'))
        new_connection_row.set_activatable(True)
        new_connection_row.set_can_focus(False)
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
    
    def _on_layout_toggle_changed(self, toggle):
        """Handle layout toggle change"""
        from sshpilot import icon_utils
        if toggle.get_active():
            # Cards view
            self.layout_stack.set_visible_child_name('cards')
            icon_utils.set_button_icon(toggle, 'view-list-symbolic')
            toggle.set_tooltip_text(_('Switch to list view'))
            # Save preference
            self.config.set_setting('ui.welcome_page_layout', 'cards')
        else:
            # List view
            self.layout_stack.set_visible_child_name('rows')
            icon_utils.set_button_icon(toggle, 'view-grid-symbolic')
            toggle.set_tooltip_text(_('Switch to grid view'))
            # Save preference
            self.config.set_setting('ui.welcome_page_layout', 'rows')
    
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

    def _populate_pinned_cards_box(self):
        """Fill _pinned_cards_box with the current pinned group (cards layout)."""
        if self._pinned_cards_box is None:
            return
        child = self._pinned_cards_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._pinned_cards_box.remove(child)
            child = nxt
        group = self._build_pinned_section()
        if group is not None:
            self._pinned_cards_box.append(group)
        sessions_group = self._build_pinned_sessions_section()
        if sessions_group is not None:
            self._pinned_cards_box.append(sessions_group)

    def refresh_pinned(self):
        """Rebuild the pinned section in both layouts after a pin/unpin action."""
        self._populate_pinned_rows_box()
        self._populate_pinned_cards_box()

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


    # Quick connect handlers


    def on_quick_connect_clicked(self, button):
        """Open quick connect dialog"""
        dialog = QuickConnectDialog(self.window)
        dialog.present()

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
    
    def _parse_ssh_command(self, command_text):
        """Parse SSH command text and extract connection parameters"""
        try:
            raw_command = command_text.strip()

            # Handle simple user@host format (backward compatibility)
            if not raw_command.startswith('ssh') and '@' in raw_command and ' ' not in raw_command:
                username, host = raw_command.split('@', 1)
                return {
                    "nickname": host,
                    "host": host,
                    "username": username,
                    "port": 22,
                    "auth_method": 0,  # Default to key-based auth
                    "key_select_mode": 0,  # Try all keys
                    "quick_connect_command": "",
                    "unparsed_args": [],
                }

            quick_connect_command = raw_command if raw_command.startswith('ssh') else ""
            working_text = raw_command

            # Parse full SSH command
            # Remove 'ssh' prefix if present
            if working_text.startswith('ssh '):
                working_text = working_text[4:]
            elif working_text.startswith('ssh'):
                working_text = working_text[3:]

            # Use shlex to properly parse the command with quoted arguments
            try:
                args = shlex.split(working_text)
            except ValueError:
                # If shlex fails, fall back to simple split
                args = working_text.split()

            connection_data = {
                "nickname": "",
                "host": "",
                "username": "",
                "port": 22,
                "auth_method": 0,
                "key_select_mode": 0,
                "keyfile": "",
                "certificate": "",
                "x11_forwarding": False,
                "forwarding_rules": [],
                "proxy_jump": [],
                "forward_agent": False,
                "extra_ssh_config": "",
                "quick_connect_command": quick_connect_command,
                "unparsed_args": [],
            }

            i = 0
            while i < len(args):
                arg = args[i]

                if arg == '-p' and i + 1 < len(args):
                    try:
                        connection_data["port"] = int(args[i + 1])
                        i += 2
                        continue
                    except ValueError:
                        pass
                elif arg == '-i' and i + 1 < len(args):
                    connection_data["keyfile"] = args[i + 1]
                    connection_data["key_select_mode"] = 2
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    option = args[i + 1]
                    parsed = option.split('=', 1)
                    if len(parsed) == 2:
                        key, value = parsed
                        key_lower = key.lower()
                        value = value.strip()
                        if key_lower == 'user':
                            connection_data["username"] = value
                        elif key_lower == 'port':
                            try:
                                connection_data["port"] = int(value)
                            except ValueError:
                                pass
                        elif key_lower == 'identityfile':
                            connection_data["keyfile"] = value
                            connection_data["key_select_mode"] = 2
                        elif key_lower == 'identitiesonly':
                            if value.lower() in ('yes', 'true', '1', 'on'):
                                connection_data["key_select_mode"] = 1
                            elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                                connection_data["key_select_mode"] = 2
                        elif key_lower == 'forwardagent':
                            connection_data["forward_agent"] = value.lower() in ('yes', 'true', '1', 'on')
                        else:
                            _append_extra_config(connection_data, f"{key} {value}")
                    i += 2
                    continue
                elif arg.startswith('-o') and '=' in arg[2:]:
                    key, value = arg[2:].split('=', 1)
                    key_lower = key.lower()
                    value = value.strip()
                    if key_lower == 'identityfile':
                        connection_data["keyfile"] = value
                        connection_data["key_select_mode"] = 2
                    elif key_lower == 'identitiesonly':
                        if value.lower() in ('yes', 'true', '1', 'on'):
                            connection_data["key_select_mode"] = 1
                        elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                            connection_data["key_select_mode"] = 2
                    elif key_lower == 'user':
                        connection_data["username"] = value
                    elif key_lower == 'port':
                        try:
                            connection_data["port"] = int(value)
                        except ValueError:
                            pass
                    elif key_lower == 'forwardagent':
                        connection_data["forward_agent"] = value.lower() in ('yes', 'true', '1', 'on')
                    else:
                        _append_extra_config(connection_data, f"{key} {value}")
                    i += 1
                    continue
                elif arg == '-X':
                    connection_data["x11_forwarding"] = True
                    i += 1
                    continue
                elif arg == '-A':
                    connection_data["forward_agent"] = True
                    i += 1
                    continue
                elif arg == '-C':
                    _append_extra_config(connection_data, "Compression yes")
                    i += 1
                    continue
                elif arg == '-4':
                    _append_extra_config(connection_data, "AddressFamily inet")
                    i += 1
                    continue
                elif arg == '-6':
                    _append_extra_config(connection_data, "AddressFamily inet6")
                    i += 1
                    continue
                elif arg == '-J' and i + 1 < len(args):
                    connection_data["proxy_jump"] = [
                        h.strip() for h in args[i + 1].split(',') if h.strip()
                    ]
                    i += 2
                    continue
                elif arg == '-L' and i + 1 < len(args):
                    rule = _parse_forward_spec(args[i + 1], 'local')
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg == '-R' and i + 1 < len(args):
                    rule = _parse_forward_spec(args[i + 1], 'remote')
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg == '-D' and i + 1 < len(args):
                    rule = _parse_dynamic_spec(args[i + 1])
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg.startswith('-p'):
                    try:
                        connection_data["port"] = int(arg[2:])
                        i += 1
                        continue
                    except ValueError:
                        pass
                elif arg.startswith('-i'):
                    connection_data["keyfile"] = arg[2:]
                    connection_data["key_select_mode"] = 2
                    i += 1
                    continue
                elif not arg.startswith('-'):
                    if not connection_data["host"]:
                        if '@' in arg:
                            username, host = arg.split('@', 1)
                            connection_data["username"] = username
                            connection_data["host"] = host
                            connection_data["nickname"] = host
                        else:
                            connection_data["host"] = arg
                            connection_data["nickname"] = arg
                    else:
                        connection_data["unparsed_args"].append(arg)
                    i += 1
                else:
                    option_key = arg
                    attached_value = ""
                    if option_key.startswith('--'):
                        option_key, _sep, attached_value = option_key.partition('=')
                    elif option_key.startswith('-') and len(option_key) > 2:
                        option_key, attached_value = option_key[:2], option_key[2:]

                    if option_key in SSH_OPTIONS_EXPECTING_ARGUMENT:
                        if attached_value:
                            i += 1
                        elif i + 1 < len(args) and not args[i + 1].startswith('-'):
                            i += 2
                        else:
                            i += 1
                    else:
                        i += 1
                    continue
            

            # Validate that we have at least a host
            if not connection_data["host"]:
                return None

            if connection_data.get("keyfile") and connection_data.get("key_select_mode", 0) == 0:
                connection_data["key_select_mode"] = 2

            return connection_data

        except Exception:
            # If parsing fails, try simple fallback
            if '@' in command_text:
                try:
                    username, host = command_text.split('@', 1)
                    return {
                        "nickname": host,
                        "host": host,
                        "username": username,
                        "port": 22,
                        "auth_method": 0,
                        "key_select_mode": 0,
                        "quick_connect_command": "",
                        "unparsed_args": [],
                    }
                except Exception:
                    pass
            return None


class QuickConnectDialog(Adw.MessageDialog):
    """Modal dialog for quick SSH connection"""

    def __init__(self, parent_window):
        super().__init__()

        self.parent_window = parent_window
        self._selected_keyfile = ""

        self.set_modal(True)
        self.set_transient_for(parent_window)
        self.set_heading(_("Quick Connect"))

        content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)
        content_area.set_size_request(420, -1)

        # Inline error banner (hidden until validation fails)
        self.error_label = Gtk.Label()
        self.error_label.set_halign(Gtk.Align.START)
        self.error_label.set_wrap(True)
        self.error_label.set_visible(False)
        self.error_label.add_css_class("error")
        content_area.append(self.error_label)

        # Connection fields group
        prefs_group = Adw.PreferencesGroup()

        self.host_row = Adw.EntryRow()
        self.host_row.set_title(_("Host"))
        self.host_row.connect('entry-activated', self.on_connect)
        self.host_row.connect('changed', self._on_host_changed)

        # Port entry inline next to host
        port_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        port_box.set_valign(Gtk.Align.CENTER)
        port_sep = Gtk.Label(label=":")
        port_sep.add_css_class("dim-label")
        self.port_entry = Gtk.Entry()
        self.port_entry.set_placeholder_text("22")
        self.port_entry.set_width_chars(5)
        self.port_entry.set_max_width_chars(5)
        self.port_entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self.port_entry.connect('activate', self.on_connect)
        port_box.append(port_sep)
        port_box.append(self.port_entry)
        self.host_row.add_suffix(port_box)

        prefs_group.add(self.host_row)

        self.user_row = Adw.EntryRow()
        self.user_row.set_title(_("Username (optional)"))
        prefs_group.add(self.user_row)

        content_area.append(prefs_group)

        # Auth fields group
        auth_group = Adw.PreferencesGroup()

        self.password_row = Adw.PasswordEntryRow(title=_("Password (optional)"))
        auth_group.add(self.password_row)

        self.keyfile_row = Adw.ActionRow()
        self.keyfile_row.set_title(_("Key File (optional)"))
        self.keyfile_row.set_subtitle(_("No key file selected"))
        browse_button = Gtk.Button(label=_("Browse…"))
        browse_button.set_valign(Gtk.Align.CENTER)
        browse_button.connect('clicked', self._browse_for_key_file)
        self.keyfile_row.add_suffix(browse_button)
        auth_group.add(self.keyfile_row)

        content_area.append(auth_group)

        self.set_extra_child(content_area)

        self.add_response("cancel", _("Cancel"))
        self.add_response("save", _("Save Connection…"))
        self.add_response("connect", _("Connect"))
        self.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)

        self.connect('response', self.on_response)
        self.host_row.grab_focus()

    def _on_host_changed(self, entry):
        if self.error_label.get_visible():
            self.error_label.set_visible(False)

    def _show_error(self, message: str):
        self.error_label.set_text(message)
        self.error_label.set_visible(True)

    def on_response(self, dialog, response):
        if response == "connect":
            self.on_connect()
        elif response == "save":
            self._on_save_connection()
        else:
            self.destroy()

    def _build_result_from_fields(self):
        """Build a connection data dict from the Host/Username/Port entry rows.

        Returns a dict on success, or raises ValueError with a user-facing message.
        """
        host = self.host_row.get_text().strip()
        if not host:
            raise ValueError(_("Host is required."))

        username = self.user_row.get_text().strip()

        port_text = self.port_entry.get_text().strip()
        if port_text:
            try:
                port = int(port_text)
            except ValueError:
                raise ValueError(_("Port must be a number."))
        else:
            port = 22

        return {
            "nickname": host,
            "host": host,
            "hostname": host,
            "username": username,
            "port": port,
            "auth_method": 0,
            "key_select_mode": 0,
            "quick_connect_command": "",
            "unparsed_args": [],
        }

    def on_connect(self, *args):
        try:
            result = self._build_result_from_fields()
        except ValueError as exc:
            self._show_error(str(exc))
            return

        errors = self._validate_parsed(result)
        if errors:
            self._show_error("\n".join(errors))
            return

        password = self.password_row.get_text()
        if password:
            result["password"] = password
            result["auth_method"] = 1
        if self._selected_keyfile:
            result["keyfile"] = self._selected_keyfile
            result["key_select_mode"] = 2

        connection = Connection(result)
        self.parent_window.terminal_manager.connect_to_host(connection, force_new=False)
        self.destroy()

    def _on_save_connection(self):
        try:
            result = self._build_result_from_fields()
        except ValueError as exc:
            self._show_error(str(exc))
            return

        errors = self._validate_parsed(result)
        if errors:
            self._show_error("\n".join(errors))
            return

        password = self.password_row.get_text()
        if password:
            result["password"] = password
            result["auth_method"] = 1
        if self._selected_keyfile:
            result["keyfile"] = self._selected_keyfile
            result["key_select_mode"] = 2

        connection = Connection(result)
        self.destroy()

        from .connection_dialog import ConnectionDialog
        conn_dialog = ConnectionDialog(
            self.parent_window,
            connection=connection,
            connection_manager=getattr(self.parent_window, 'connection_manager', None),
        )
        # load_connection_data() already ran inside __init__ to pre-fill the form.
        # Mark as new so window.on_connection_saved takes the new-connection branch
        # that appends to connection_manager.connections instead of the edit branch
        # that tries to update a transient object not registered in the manager.
        conn_dialog.is_editing = False
        conn_dialog.connect('connection-saved', self._on_connection_saved)
        conn_dialog.present()

    def _on_connection_saved(self, dialog, connection_data):
        if hasattr(self.parent_window, 'on_connection_saved'):
            self.parent_window.on_connection_saved(dialog, connection_data)

    def _browse_for_key_file(self, button):
        dialog = Gtk.FileDialog(title=_("Select SSH Key File"))
        ssh_dir = get_ssh_dir()
        if ssh_dir and os.path.isdir(ssh_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(ssh_dir))
        dialog.open(self, None, self._on_key_file_selected)

    def _on_key_file_selected(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
            self._selected_keyfile = gfile.get_path()
            self.keyfile_row.set_subtitle(self._selected_keyfile)
        except Exception:
            pass

    def _validate_parsed(self, connection_data: dict) -> list:
        from .connection_dialog import SSHConnectionValidator
        validator = SSHConnectionValidator()
        errors = []

        hostname = connection_data.get("hostname") or connection_data.get("host", "")
        result = validator.validate_hostname(hostname)
        if not result.is_valid:
            errors.append(result.message)

        port = connection_data.get("port", 22)
        result = validator.validate_port(str(port))
        if not result.is_valid:
            errors.append(result.message)

        return errors

    def _parse_ssh_command(self, command_text):
        """Parse an SSH command string into connection parameters.

        Returns a connection data dict, a dict with an "error" key for user-visible
        errors, or None when the input cannot be parsed at all.
        Only accepts bare user@host or commands starting with 'ssh'.
        """
        try:
            raw_command = command_text.strip()

            # Allow bare user@host without the 'ssh' prefix
            if '@' in raw_command and ' ' not in raw_command and not raw_command.startswith('ssh'):
                parts = raw_command.split('@', 1)
                username, host = parts[0], parts[1]
                if not host:
                    return None
                return {
                    "nickname": host,
                    "host": host,
                    "hostname": host,
                    "username": username,
                    "port": 22,
                    "auth_method": 0,
                    "key_select_mode": 0,
                    "quick_connect_command": "",
                    "unparsed_args": [],
                }

            # For any other input the first token must be exactly "ssh"
            try:
                tokens = shlex.split(raw_command)
            except ValueError:
                tokens = raw_command.split()

            if not tokens:
                return None

            if tokens[0] != "ssh":
                return {"error": _("Only SSH commands are allowed. Example: ssh user@host")}

            quick_connect_command = raw_command
            args = tokens[1:]

            connection_data = {
                "nickname": "",
                "host": "",
                "hostname": "",
                "username": "",
                "port": 22,
                "auth_method": 0,
                "key_select_mode": 0,
                "keyfile": "",
                "certificate": "",
                "x11_forwarding": False,
                "forwarding_rules": [],
                "proxy_jump": [],
                "forward_agent": False,
                "extra_ssh_config": "",
                "quick_connect_command": quick_connect_command,
                "unparsed_args": [],
            }

            i = 0
            while i < len(args):
                arg = args[i]

                if arg == '-p' and i + 1 < len(args):
                    try:
                        connection_data["port"] = int(args[i + 1])
                        i += 2
                        continue
                    except ValueError:
                        pass
                elif arg == '-i' and i + 1 < len(args):
                    connection_data["keyfile"] = args[i + 1]
                    connection_data["key_select_mode"] = 2
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    option = args[i + 1]
                    parsed = option.split('=', 1)
                    if len(parsed) == 2:
                        key, value = parsed
                        key_lower = key.lower()
                        value = value.strip()
                        if key_lower == 'user':
                            connection_data["username"] = value
                        elif key_lower == 'port':
                            try:
                                connection_data["port"] = int(value)
                            except ValueError:
                                pass
                        elif key_lower == 'identityfile':
                            connection_data["keyfile"] = value
                            connection_data["key_select_mode"] = 2
                        elif key_lower == 'identitiesonly':
                            if value.lower() in ('yes', 'true', '1', 'on'):
                                connection_data["key_select_mode"] = 1
                            elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                                connection_data["key_select_mode"] = 2
                        elif key_lower == 'forwardagent':
                            connection_data["forward_agent"] = value.lower() in ('yes', 'true', '1', 'on')
                        else:
                            _append_extra_config(connection_data, f"{key} {value}")
                    i += 2
                    continue
                elif arg.startswith('-o') and '=' in arg[2:]:
                    key, value = arg[2:].split('=', 1)
                    key_lower = key.lower()
                    value = value.strip()
                    if key_lower == 'identityfile':
                        connection_data["keyfile"] = value
                        connection_data["key_select_mode"] = 2
                    elif key_lower == 'identitiesonly':
                        if value.lower() in ('yes', 'true', '1', 'on'):
                            connection_data["key_select_mode"] = 1
                        elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                            connection_data["key_select_mode"] = 2
                    elif key_lower == 'user':
                        connection_data["username"] = value
                    elif key_lower == 'port':
                        try:
                            connection_data["port"] = int(value)
                        except ValueError:
                            pass
                    elif key_lower == 'forwardagent':
                        connection_data["forward_agent"] = value.lower() in ('yes', 'true', '1', 'on')
                    else:
                        _append_extra_config(connection_data, f"{key} {value}")
                    i += 1
                    continue
                elif arg == '-X':
                    connection_data["x11_forwarding"] = True
                    i += 1
                    continue
                elif arg == '-A':
                    connection_data["forward_agent"] = True
                    i += 1
                    continue
                elif arg == '-C':
                    _append_extra_config(connection_data, "Compression yes")
                    i += 1
                    continue
                elif arg == '-4':
                    _append_extra_config(connection_data, "AddressFamily inet")
                    i += 1
                    continue
                elif arg == '-6':
                    _append_extra_config(connection_data, "AddressFamily inet6")
                    i += 1
                    continue
                elif arg == '-J' and i + 1 < len(args):
                    connection_data["proxy_jump"] = [
                        h.strip() for h in args[i + 1].split(',') if h.strip()
                    ]
                    i += 2
                    continue
                elif arg == '-L' and i + 1 < len(args):
                    rule = _parse_forward_spec(args[i + 1], 'local')
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg == '-R' and i + 1 < len(args):
                    rule = _parse_forward_spec(args[i + 1], 'remote')
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg == '-D' and i + 1 < len(args):
                    rule = _parse_dynamic_spec(args[i + 1])
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg.startswith('-p'):
                    try:
                        connection_data["port"] = int(arg[2:])
                        i += 1
                        continue
                    except ValueError:
                        pass
                elif arg.startswith('-i'):
                    connection_data["keyfile"] = arg[2:]
                    connection_data["key_select_mode"] = 2
                    i += 1
                    continue
                elif not arg.startswith('-'):
                    if not connection_data["host"]:
                        if '@' in arg:
                            username, host = arg.split('@', 1)
                            connection_data["username"] = username
                            connection_data["host"] = host
                            connection_data["hostname"] = host
                            connection_data["nickname"] = host
                        else:
                            connection_data["host"] = arg
                            connection_data["hostname"] = arg
                            connection_data["nickname"] = arg
                    else:
                        connection_data["unparsed_args"].append(arg)
                    i += 1
                else:
                    option_key = arg
                    attached_value = ""
                    if option_key.startswith('--'):
                        option_key, _sep, attached_value = option_key.partition('=')
                    elif option_key.startswith('-') and len(option_key) > 2:
                        option_key, attached_value = option_key[:2], option_key[2:]

                    expects_argument = option_key in SSH_OPTIONS_EXPECTING_ARGUMENT
                    if attached_value:
                        connection_data["unparsed_args"].append(arg)
                        i += 1
                        continue

                    if expects_argument:
                        if i + 1 < len(args) and not args[i + 1].startswith('-'):
                            connection_data["unparsed_args"].extend([arg, args[i + 1]])
                            i += 2
                        else:
                            connection_data["unparsed_args"].append(arg)
                            i += 1
                    else:
                        connection_data["unparsed_args"].append(arg)
                        i += 1
                    continue

            if not connection_data["host"]:
                return None

            if connection_data.get("keyfile") and connection_data.get("key_select_mode", 0) == 0:
                connection_data["key_select_mode"] = 2

            return connection_data

        except Exception:
            # Last-resort fallback for bare user@host that somehow raised
            if '@' in command_text and ' ' not in command_text:
                try:
                    username, host = command_text.split('@', 1)
                    if host:
                        return {
                            "nickname": host,
                            "host": host,
                            "hostname": host,
                            "username": username,
                            "port": 22,
                            "auth_method": 0,
                            "key_select_mode": 0,
                            "quick_connect_command": "",
                            "unparsed_args": [],
                        }
                except Exception:
                    pass
            return None
