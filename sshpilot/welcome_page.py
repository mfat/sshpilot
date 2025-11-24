"""Welcome page widget for sshPilot."""

import gi
import shlex
import logging

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, Gdk

from gettext import gettext as _

from .connection_manager import Connection
from .platform_utils import is_macos


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
        icon = Gtk.Image.new_from_icon_name('io.github.mfat.sshpilot')
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
        saved_layout = self.config.get_setting('ui.welcome_page_layout', 'cards')
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
        if use_cards:
            self.layout_stack.set_visible_child_name('cards')
            self.layout_toggle.set_icon_name('view-list-symbolic')
            self.layout_toggle.set_tooltip_text(_('Switch to list view'))
        else:
            self.layout_stack.set_visible_child_name('rows')
            self.layout_toggle.set_icon_name('view-grid-symbolic')
            self.layout_toggle.set_tooltip_text(_('Switch to grid view'))
    
    def _build_cards_layout(self, current_shortcuts):
        """Build the cards grid layout"""
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
        quick_connect_btn.set_size_request(160, 160)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(16)
        card_box.set_margin_end(16)
        card_box.set_margin_top(16)
        card_box.set_margin_bottom(16)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = Gtk.Image.new_from_icon_name('network-server-symbolic')
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
        new_connection_btn.set_size_request(160, 160)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(16)
        card_box.set_margin_end(16)
        card_box.set_margin_top(16)
        card_box.set_margin_bottom(16)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = Gtk.Image.new_from_icon_name('list-add-symbolic')
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
        edit_config_btn.set_size_request(160, 160)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(16)
        card_box.set_margin_end(16)
        card_box.set_margin_top(16)
        card_box.set_margin_bottom(16)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = Gtk.Image.new_from_icon_name('document-edit-symbolic')
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
        local_terminal_btn.set_size_request(160, 160)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(16)
        card_box.set_margin_end(16)
        card_box.set_margin_top(16)
        card_box.set_margin_bottom(16)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = Gtk.Image.new_from_icon_name('utilities-terminal-symbolic')
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
        shortcuts_btn.set_size_request(160, 160)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(16)
        card_box.set_margin_end(16)
        card_box.set_margin_top(16)
        card_box.set_margin_bottom(16)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = Gtk.Image.new_from_icon_name('preferences-desktop-keyboard-symbolic')
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
        
        # Preferences card
        preferences_accel = self._get_action_accel_display(current_shortcuts, 'preferences')
        preferences_btn = Gtk.Button()
        preferences_btn.set_can_focus(False)
        preferences_btn.add_css_class('card')
        preferences_btn.set_size_request(160, 160)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(16)
        card_box.set_margin_end(16)
        card_box.set_margin_top(16)
        card_box.set_margin_bottom(16)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = Gtk.Image.new_from_icon_name('preferences-system-symbolic')
        prefix_img.set_can_focus(False)
        prefix_img.set_pixel_size(32)
        card_box.append(prefix_img)
        
        title_label = Gtk.Label(label=_('Preferences'))
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.add_css_class('title-4')
        card_box.append(title_label)
        
        if preferences_accel:
            shortcut_label = Gtk.Label(label=preferences_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcut_label.set_halign(Gtk.Align.CENTER)
            card_box.append(shortcut_label)
        
        preferences_btn.set_child(card_box)
        preferences_btn.connect('clicked', lambda *_: self.window.show_preferences())
        cards_grid.append(preferences_btn)
        
        # Online Documentation card
        help_accel = self._get_action_accel_display(current_shortcuts, 'help')
        help_btn = Gtk.Button()
        help_btn.set_can_focus(False)
        help_btn.add_css_class('card')
        help_btn.set_size_request(160, 160)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(16)
        card_box.set_margin_end(16)
        card_box.set_margin_top(16)
        card_box.set_margin_bottom(16)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = Gtk.Image.new_from_icon_name('help-browser-symbolic')
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
        
        # New Group card
        create_group_accel = self._get_action_accel_display(current_shortcuts, 'create-group')
        create_group_btn = Gtk.Button()
        create_group_btn.set_can_focus(False)
        create_group_btn.add_css_class('card')
        create_group_btn.set_size_request(160, 160)
        
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card_box.set_margin_start(16)
        card_box.set_margin_end(16)
        card_box.set_margin_top(16)
        card_box.set_margin_bottom(16)
        card_box.set_halign(Gtk.Align.CENTER)
        card_box.set_valign(Gtk.Align.CENTER)
        
        prefix_img = Gtk.Image.new_from_icon_name('folder-new-symbolic')
        prefix_img.set_can_focus(False)
        prefix_img.set_pixel_size(32)
        card_box.append(prefix_img)
        
        title_label = Gtk.Label(label=_('New Group'))
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.add_css_class('title-4')
        card_box.append(title_label)
        
        if create_group_accel:
            shortcut_label = Gtk.Label(label=create_group_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcut_label.set_halign(Gtk.Align.CENTER)
            card_box.append(shortcut_label)
        
        create_group_btn.set_child(card_box)
        create_group_btn.connect('clicked', lambda *_: self.window.create_group_action.activate(None))
        cards_grid.append(create_group_btn)
        
        return cards_grid
    
    def _build_action_rows_layout(self, current_shortcuts):
        """Build the action rows layout"""
        # Wrap in clamp to constrain width
        clamp = Adw.Clamp()
        clamp.set_maximum_size(600)
        clamp.set_tightening_threshold(400)
        clamp.set_vexpand(False)
        clamp.set_can_focus(False)
        
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        
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
        quick_connect_row.set_subtitle(_('Connect instantly using an SSH command'))
        quick_connect_row.set_activatable(True)
        quick_connect_row.set_can_focus(False)
        prefix_img = Gtk.Image.new_from_icon_name('network-server-symbolic')
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
        new_connection_row.set_subtitle(_('Create and save a new SSH connection profile'))
        new_connection_row.set_activatable(True)
        new_connection_row.set_can_focus(False)
        prefix_img = Gtk.Image.new_from_icon_name('list-add-symbolic')
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
        edit_config_row.set_subtitle(_('Directly edit your SSH configuration file') + f' ({config_location})')
        edit_config_row.set_activatable(True)
        edit_config_row.set_can_focus(False)
        prefix_img = Gtk.Image.new_from_icon_name('document-edit-symbolic')
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
        local_terminal_row.set_subtitle(_('Work on your local machine without connecting to a server'))
        local_terminal_row.set_activatable(True)
        local_terminal_row.set_can_focus(False)
        prefix_img = Gtk.Image.new_from_icon_name('utilities-terminal-symbolic')
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
        shortcuts_row.set_subtitle(_('View keyboard shortcuts'))
        shortcuts_row.set_activatable(True)
        shortcuts_row.set_can_focus(False)
        prefix_img = Gtk.Image.new_from_icon_name('preferences-desktop-keyboard-symbolic')
        prefix_img.set_can_focus(False)
        shortcuts_row.add_prefix(prefix_img)
        if shortcuts_accel:
            shortcut_label = Gtk.Label(label=shortcuts_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            shortcuts_row.add_suffix(shortcut_label)
        shortcuts_row.connect('activated', lambda *_: self.window.show_shortcuts_window())
        help_group.add(shortcuts_row)
        
        # Preferences action row
        preferences_accel = self._get_action_accel_display(current_shortcuts, 'preferences')
        preferences_row = Adw.ActionRow()
        preferences_row.set_title(_('Preferences'))
        preferences_row.set_subtitle(_('Customize SSH Pilot and modify settings'))
        preferences_row.set_activatable(True)
        preferences_row.set_can_focus(False)
        prefix_img = Gtk.Image.new_from_icon_name('preferences-system-symbolic')
        prefix_img.set_can_focus(False)
        preferences_row.add_prefix(prefix_img)
        if preferences_accel:
            shortcut_label = Gtk.Label(label=preferences_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            preferences_row.add_suffix(shortcut_label)
        preferences_row.connect('activated', lambda *_: self.window.show_preferences())
        help_group.add(preferences_row)
        
        # Online help action row
        help_accel = self._get_action_accel_display(current_shortcuts, 'help')
        help_row = Adw.ActionRow()
        help_row.set_title(_('Online Documentation'))
        help_row.set_subtitle(_('Visit the wiki for guides and troubleshooting'))
        help_row.set_activatable(True)
        help_row.set_can_focus(False)
        prefix_img = Gtk.Image.new_from_icon_name('help-browser-symbolic')
        prefix_img.set_can_focus(False)
        help_row.add_prefix(prefix_img)
        if help_accel:
            shortcut_label = Gtk.Label(label=help_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            help_row.add_suffix(shortcut_label)
        help_row.connect('activated', lambda *_: self.open_online_help())
        help_group.add(help_row)
        
        # New Group action row
        create_group_accel = self._get_action_accel_display(current_shortcuts, 'create-group')
        create_group_row = Adw.ActionRow()
        create_group_row.set_title(_('New Group'))
        create_group_row.set_subtitle(_('Create a new connection group'))
        create_group_row.set_activatable(True)
        create_group_row.set_can_focus(False)
        prefix_img = Gtk.Image.new_from_icon_name('folder-new-symbolic')
        prefix_img.set_can_focus(False)
        create_group_row.add_prefix(prefix_img)
        if create_group_accel:
            shortcut_label = Gtk.Label(label=create_group_accel)
            shortcut_label.add_css_class('dim-label')
            shortcut_label.set_can_focus(False)
            create_group_row.add_suffix(shortcut_label)
        create_group_row.connect('activated', lambda *_: self.window.create_group_action.activate(None))
        help_group.add(create_group_row)
        
        # Set container as child of clamp
        clamp.set_child(container)
        
        return clamp
    
    def _on_layout_toggle_changed(self, toggle):
        """Handle layout toggle change"""
        if toggle.get_active():
            # Cards view
            self.layout_stack.set_visible_child_name('cards')
            toggle.set_icon_name('view-list-symbolic')
            toggle.set_tooltip_text(_('Switch to list view'))
            # Save preference
            self.config.set_setting('ui.welcome_page_layout', 'cards')
        else:
            # List view
            self.layout_stack.set_visible_child_name('rows')
            toggle.set_icon_name('view-grid-symbolic')
            toggle.set_tooltip_text(_('Switch to grid view'))
            # Save preference
            self.config.set_setting('ui.welcome_page_layout', 'rows')
    
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
        import webbrowser
        try:
            webbrowser.open('https://github.com/mfat/sshpilot/wiki')
        except Exception as e:
            # Fallback: show a dialog with the URL
            dialog = Adw.MessageDialog.new(
                self.window,
                "Online Help",
                "Visit the SSH Pilot documentation at:\nhttps://github.com/mfat/sshpilot/wiki"
            )
            dialog.add_response("ok", "OK")
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_modal(True)
            dialog.set_transient_for(self.window)
            dialog.present()
    
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

            # Initialize connection data with defaults
            connection_data = {
                "nickname": "",
                "host": "",
                "username": "",
                "port": 22,
                "auth_method": 0,  # Key-based auth
                "key_select_mode": 0,  # Try all keys
                "keyfile": "",
                "certificate": "",
                "x11_forwarding": False,
                "local_port_forwards": [],
                "remote_port_forwards": [],
                "dynamic_forwards": [],
                "quick_connect_command": quick_connect_command,
                "unparsed_args": [],
            }

            i = 0
            while i < len(args):
                arg = args[i]

                # Handle options with values
                if arg == '-p' and i + 1 < len(args):
                    try:
                        connection_data["port"] = int(args[i + 1])
                        i += 2
                        continue
                    except ValueError:
                        pass
                elif arg == '-i' and i + 1 < len(args):
                    connection_data["keyfile"] = args[i + 1]
                    connection_data["key_select_mode"] = 2  # Use specific key without forcing IdentitiesOnly by default
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    # Handle SSH options like -o "UserKnownHostsFile=/dev/null"
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
                    i += 1
                    continue
                elif arg == '-X':
                    connection_data["x11_forwarding"] = True
                    i += 1
                    continue
                elif arg == '-L' and i + 1 < len(args):
                    # Local port forwarding: -L [bind_address:]port:host:hostport
                    forward_spec = args[i + 1]
                    connection_data["local_port_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg == '-R' and i + 1 < len(args):
                    # Remote port forwarding: -R [bind_address:]port:host:hostport
                    forward_spec = args[i + 1]
                    connection_data["remote_port_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg == '-D' and i + 1 < len(args):
                    # Dynamic port forwarding: -D [bind_address:]port
                    forward_spec = args[i + 1]
                    connection_data["dynamic_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg.startswith('-p'):
                    # Handle -p2222 format (no space)
                    try:
                        connection_data["port"] = int(arg[2:])
                        i += 1
                        continue
                    except ValueError:
                        pass
                elif arg.startswith('-i'):
                    # Handle -i/path/to/key format (no space)
                    connection_data["keyfile"] = arg[2:]
                    connection_data["key_select_mode"] = 2
                    i += 1
                    continue
                elif not arg.startswith('-'):
                    # This should be the host specification (user@host)
                    if not connection_data["host"]:
                        if '@' in arg:
                            username, host = arg.split('@', 1)
                            connection_data["username"] = username
                            connection_data["host"] = host
                            connection_data["nickname"] = host
                        else:
                            # Just hostname, no username
                            connection_data["host"] = arg
                            connection_data["nickname"] = arg
                    else:
                        connection_data["unparsed_args"].append(arg)
                    i += 1
                else:

                    # Unknown option. Determine whether it normally expects an argument.
                    option_key = arg
                    attached_value = ""
                    if option_key.startswith('--'):
                        option_key, _, attached_value = option_key.partition('=')
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
        
        # Set dialog properties
        self.set_modal(True)
        self.set_transient_for(parent_window)
        self.set_title("Quick Connect")
        
        # Create content area
        content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)
        
        # Add description
        description = Gtk.Label()
        description.set_text("Enter SSH command or connection details:")
        description.set_halign(Gtk.Align.START)
        content_area.append(description)
        
        # Create entry field
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("ssh -p 2222 user@host")
        self.entry.set_hexpand(True)
        self.entry.connect('activate', self.on_connect)
        content_area.append(self.entry)

        # Add content to dialog
        self.set_extra_child(content_area)

        # Add response buttons
        self.add_response("cancel", "Cancel")
        self.add_response("connect", "Connect")
        self.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        
        # Connect response signal
        self.connect('response', self.on_response)

        # Focus the entry when dialog is shown
        self.entry.grab_focus()

    def on_response(self, dialog, response):
        """Handle dialog response"""
        if response == "connect":
            self.on_connect()
        self.destroy()

    def on_connect(self, *args):
        """Handle connect button or Enter key"""
        text = self.entry.get_text().strip()
        if not text:
            return
        
        # Parse the SSH command
        connection_data = self._parse_ssh_command(text)
        if connection_data:
            connection = Connection(connection_data)
            self.parent_window.terminal_manager.connect_to_host(connection, force_new=False)
            self.destroy()

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

            # Initialize connection data with defaults
            connection_data = {
                "nickname": "",
                "host": "",
                "username": "",
                "port": 22,
                "auth_method": 0,  # Key-based auth
                "key_select_mode": 0,  # Try all keys
                "keyfile": "",
                "certificate": "",
                "x11_forwarding": False,
                "local_port_forwards": [],
                "remote_port_forwards": [],
                "dynamic_forwards": [],
                "quick_connect_command": quick_connect_command,
                "unparsed_args": [],
            }

            i = 0
            while i < len(args):
                arg = args[i]

                # Handle options with values
                if arg == '-p' and i + 1 < len(args):
                    try:
                        connection_data["port"] = int(args[i + 1])
                        i += 2
                        continue
                    except ValueError:
                        pass
                elif arg == '-i' and i + 1 < len(args):
                    connection_data["keyfile"] = args[i + 1]
                    connection_data["key_select_mode"] = 2  # Use specific key without forcing IdentitiesOnly by default
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    # Handle SSH options like -o "UserKnownHostsFile=/dev/null"
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
                    i += 1
                    continue
                elif arg == '-X':
                    connection_data["x11_forwarding"] = True
                    i += 1
                    continue
                elif arg == '-L' and i + 1 < len(args):
                    # Local port forwarding: -L [bind_address:]port:host:hostport
                    forward_spec = args[i + 1]
                    connection_data["local_port_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg == '-R' and i + 1 < len(args):
                    # Remote port forwarding: -R [bind_address:]port:host:hostport
                    forward_spec = args[i + 1]
                    connection_data["remote_port_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg == '-D' and i + 1 < len(args):
                    # Dynamic port forwarding: -D [bind_address:]port
                    forward_spec = args[i + 1]
                    connection_data["dynamic_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg.startswith('-p'):
                    # Handle -p2222 format (no space)
                    try:
                        connection_data["port"] = int(arg[2:])
                        i += 1
                        continue
                    except ValueError:
                        pass
                elif arg.startswith('-i'):
                    # Handle -i/path/to/key format (no space)
                    connection_data["keyfile"] = arg[2:]
                    connection_data["key_select_mode"] = 2
                    i += 1
                    continue
                elif not arg.startswith('-'):
                    # This should be the host specification (user@host)
                    if not connection_data["host"]:
                        if '@' in arg:
                            username, host = arg.split('@', 1)
                            connection_data["username"] = username
                            connection_data["host"] = host
                            connection_data["nickname"] = host
                        else:
                            # Just hostname, no username
                            connection_data["host"] = arg
                            connection_data["nickname"] = arg
                    else:
                        connection_data["unparsed_args"].append(arg)
                    i += 1
                else:

                    # Unknown option. Determine whether it normally expects an argument.
                    option_key = arg
                    attached_value = ""
                    if option_key.startswith('--'):
                        option_key, _, attached_value = option_key.partition('=')
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
