"""
Main Window for sshPilot
Primary UI with connection list, tabs, and terminal management
"""

import os
import logging
from typing import Optional, Dict, Any

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gdk, Pango

from gettext import gettext as _

from .connection_manager import ConnectionManager, Connection
from .terminal import TerminalWidget
from .config import Config
from .key_manager import KeyManager, SSHKey
from .port_forwarding_ui import PortForwardingRules
from .connection_dialog import ConnectionDialog

logger = logging.getLogger(__name__)

class ConnectionRow(Gtk.ListBoxRow):
    """Row widget for connection list"""
    
    def __init__(self, connection: Connection):
        super().__init__()
        self.connection = connection
        
        # Create main box
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        
        # Connection icon
        icon = Gtk.Image.new_from_icon_name('computer-symbolic')
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        box.append(icon)
        
        # Connection info
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        
        # Nickname label
        self.nickname_label = Gtk.Label()
        self.nickname_label.set_markup(f"<b>{connection.nickname}</b>")
        self.nickname_label.set_halign(Gtk.Align.START)
        info_box.append(self.nickname_label)
        
        # Host info label
        self.host_label = Gtk.Label()
        self.host_label.set_text(f"{connection.username}@{connection.host}")
        self.host_label.set_halign(Gtk.Align.START)
        self.host_label.add_css_class('dim-label')
        info_box.append(self.host_label)
        
        box.append(info_box)
        
        # Connection status indicator
        self.status_icon = Gtk.Image.new_from_icon_name('network-offline-symbolic')
        self.status_icon.set_pixel_size(16)  # GTK4 uses pixel size instead of IconSize
        box.append(self.status_icon)
        
        self.set_child(box)
        self.set_selectable(True)  # Make the row selectable for keyboard navigation
        
        # Update status
        self.update_status()

    def update_status(self):
        """Update connection status display"""
        if self.connection.is_connected:
            self.status_icon.set_from_icon_name('network-idle-symbolic')
            self.status_icon.set_tooltip_text('Connected')
        else:
            self.status_icon.set_from_icon_name('network-offline-symbolic')
            self.status_icon.set_tooltip_text('Disconnected')
    
    def update_display(self):
        """Update the display with current connection data"""
        # Update the labels with current connection data
        if hasattr(self.connection, 'nickname') and hasattr(self, 'nickname_label'):
            self.nickname_label.set_markup(f"<b>{self.connection.nickname}</b>")
        
        if hasattr(self.connection, 'username') and hasattr(self.connection, 'host') and hasattr(self, 'host_label'):
            port_text = f":{self.connection.port}" if hasattr(self.connection, 'port') and self.connection.port != 22 else ""
            self.host_label.set_text(f"{self.connection.username}@{self.connection.host}{port_text}")
        
        self.update_status()

    def show_error(self, message):
        """Show error message"""
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading='Error',
            body=message,
        )
        dialog.add_response('ok', 'OK')
        dialog.set_default_response('ok')
        dialog.present()

class WelcomePage(Gtk.Box):
    """Welcome page shown when no tabs are open"""
    
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.set_margin_start(48)
        self.set_margin_end(48)
        self.set_margin_top(48)
        self.set_margin_bottom(48)
        
        # Welcome icon
        icon = Gtk.Image.new_from_icon_name('network-server-symbolic')
        icon.set_icon_size(Gtk.IconSize.LARGE)
        icon.set_pixel_size(64)
        self.append(icon)
        
        # Welcome title
        title = Gtk.Label()
        title.set_markup('<span size="x-large"><b>Welcome to sshPilot</b></span>')
        title.set_halign(Gtk.Align.CENTER)
        self.append(title)
        
        # Welcome message
        message = Gtk.Label()
        message.set_text('Manage your SSH connections with ease')
        message.set_halign(Gtk.Align.CENTER)
        message.add_css_class('dim-label')
        self.append(message)
        
        # Shortcuts box
        shortcuts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        shortcuts_box.set_halign(Gtk.Align.CENTER)
        
        shortcuts_title = Gtk.Label()
        shortcuts_title.set_markup('<b>Keyboard Shortcuts</b>')
        shortcuts_box.append(shortcuts_title)
        
        shortcuts = [
            ('Ctrl+N', 'New Connection'),
            ('Ctrl+L', 'Toggle Connection List'),
            ('Ctrl+Shift+K', 'Generate SSH Key'),
            ('Enter', 'Connect to Selected Host'),
        ]
        
        for shortcut, description in shortcuts:
            shortcut_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            
            key_label = Gtk.Label()
            key_label.set_markup(f'<tt>{shortcut}</tt>')
            key_label.set_width_chars(15)
            key_label.set_halign(Gtk.Align.START)
            shortcut_box.append(key_label)
            
            desc_label = Gtk.Label()
            desc_label.set_text(description)
            desc_label.set_halign(Gtk.Align.START)
            shortcut_box.append(desc_label)
            
            shortcuts_box.append(shortcut_box)
        
        self.append(shortcuts_box)

class PreferencesWindow(Adw.PreferencesWindow):
    """Preferences dialog window"""
    
    def __init__(self, parent_window, config):
        super().__init__()
        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.config = config
        
        # Set window properties
        self.set_title("Preferences")
        self.set_default_size(600, 500)
        
        # Initialize the preferences UI
        self.setup_preferences()
    
    def setup_preferences(self):
        """Set up preferences UI with current values"""
        try:
            # Create Terminal preferences page
            terminal_page = Adw.PreferencesPage()
            terminal_page.set_title("Terminal")
            terminal_page.set_icon_name("utilities-terminal-symbolic")
            
            # Terminal appearance group
            appearance_group = Adw.PreferencesGroup()
            appearance_group.set_title("Appearance")
            
            # Font selection row
            self.font_row = Adw.ActionRow()
            self.font_row.set_title("Font")
            current_font = self.config.get_setting('terminal-font', 'Monospace 12')
            self.font_row.set_subtitle(current_font)
            
            font_button = Gtk.Button()
            font_button.set_label("Choose")
            font_button.connect('clicked', self.on_font_button_clicked)
            self.font_row.add_suffix(font_button)
            
            appearance_group.add(self.font_row)
            
            # Terminal color scheme
            self.color_scheme_row = Adw.ComboRow()
            self.color_scheme_row.set_title("Color Scheme")
            self.color_scheme_row.set_subtitle("Terminal color theme")
            
            color_schemes = Gtk.StringList()
            color_schemes.append("Default")
            color_schemes.append("Solarized Dark")
            color_schemes.append("Solarized Light")
            color_schemes.append("Monokai")
            color_schemes.append("Dracula")
            color_schemes.append("Nord")
            self.color_scheme_row.set_model(color_schemes)
            
            # Set current color scheme from config
            current_scheme_key = self.config.get_setting('terminal-color-scheme', 'default')
            reverse_mapping = self.get_reverse_theme_mapping()
            current_scheme_display = reverse_mapping.get(current_scheme_key, 'Default')
            
            scheme_names = ["Default", "Solarized Dark", "Solarized Light", "Monokai", "Dracula", "Nord"]
            try:
                current_index = scheme_names.index(current_scheme_display)
                self.color_scheme_row.set_selected(current_index)
            except ValueError:
                self.color_scheme_row.set_selected(0)  # Default to first option
            
            self.color_scheme_row.connect('notify::selected', self.on_color_scheme_changed)
            
            appearance_group.add(self.color_scheme_row)
            terminal_page.add(appearance_group)
            
            # Create Interface preferences page
            interface_page = Adw.PreferencesPage()
            interface_page.set_title("Interface")
            interface_page.set_icon_name("applications-graphics-symbolic")
            
            # Behavior group
            behavior_group = Adw.PreferencesGroup()
            behavior_group.set_title("Behavior")
            
            # Confirm before disconnecting
            self.confirm_disconnect_switch = Adw.SwitchRow()
            self.confirm_disconnect_switch.set_title("Confirm before disconnecting")
            self.confirm_disconnect_switch.set_subtitle("Show a confirmation dialog when disconnecting from a host")
            self.confirm_disconnect_switch.set_active(
                self.config.get_setting('confirm-disconnect', True)
            )
            self.confirm_disconnect_switch.connect('notify::active', self.on_confirm_disconnect_changed)
            behavior_group.add(self.confirm_disconnect_switch)
            
            interface_page.add(behavior_group)
            
            # Appearance group
            interface_appearance_group = Adw.PreferencesGroup()
            interface_appearance_group.set_title("Appearance")
            
            # Theme selection
            self.theme_row = Adw.ComboRow()
            self.theme_row.set_title("Application Theme")
            self.theme_row.set_subtitle("Choose light, dark, or follow system theme")
            
            themes = Gtk.StringList()
            themes.append("Follow System")
            themes.append("Light")
            themes.append("Dark")
            self.theme_row.set_model(themes)
            self.theme_row.set_selected(0)  # Default to follow system
            self.theme_row.connect('notify::selected', self.on_theme_changed)
            
            interface_appearance_group.add(self.theme_row)
            interface_page.add(interface_appearance_group)
            
            # Window group
            window_group = Adw.PreferencesGroup()
            window_group.set_title("Window")
            
            # Remember window size switch
            remember_size_switch = Adw.SwitchRow()
            remember_size_switch.set_title("Remember Window Size")
            remember_size_switch.set_subtitle("Restore window size on startup")
            remember_size_switch.set_active(True)
            
            # Auto focus terminal switch
            auto_focus_switch = Adw.SwitchRow()
            auto_focus_switch.set_title("Auto Focus Terminal")
            auto_focus_switch.set_subtitle("Focus terminal when connecting")
            auto_focus_switch.set_active(True)
            
            window_group.add(remember_size_switch)
            window_group.add(auto_focus_switch)
            interface_page.add(window_group)
            
            # Add pages to the preferences window
            self.add(terminal_page)
            self.add(interface_page)
            
            logger.info("Preferences window initialized")
        except Exception as e:
            logger.error(f"Failed to setup preferences: {e}")
    
    def on_font_button_clicked(self, button):
        """Handle font button click"""
        logger.info("Font button clicked")
        
        # Create font chooser dialog
        font_dialog = Gtk.FontDialog()
        font_dialog.set_title("Choose Terminal Font (Monospace Recommended)")
        
        # Set current font (get from config or default)
        current_font = self.config.get_setting('terminal-font', 'Monospace 12')
        font_desc = Pango.FontDescription.from_string(current_font)
        
        def on_font_selected(dialog, result):
            try:
                font_desc = dialog.choose_font_finish(result)
                if font_desc:
                    font_string = font_desc.to_string()
                    self.font_row.set_subtitle(font_string)
                    logger.info(f"Font selected: {font_string}")
                    
                    # Save to config
                    self.config.set_setting('terminal-font', font_string)
                    
                    # Apply to all active terminals
                    self.apply_font_to_terminals(font_string)
                    
            except Exception as e:
                logger.warning(f"Font selection cancelled or failed: {e}")
        
        font_dialog.choose_font(self, None, None, on_font_selected)
    
    def apply_font_to_terminals(self, font_string):
        """Apply font to all active terminal widgets"""
        try:
            parent_window = self.get_transient_for()
            if parent_window and hasattr(parent_window, 'active_terminals'):
                font_desc = Pango.FontDescription.from_string(font_string)
                for terminal in parent_window.active_terminals.values():
                    if hasattr(terminal, 'vte'):
                        terminal.vte.set_font(font_desc)
                logger.info(f"Applied font {font_string} to {len(parent_window.active_terminals)} terminals")
        except Exception as e:
            logger.error(f"Failed to apply font to terminals: {e}")
    
    def on_theme_changed(self, combo_row, param):
        """Handle theme selection change"""
        selected = combo_row.get_selected()
        theme_names = ["Follow System", "Light", "Dark"]
        selected_theme = theme_names[selected] if selected < len(theme_names) else "Follow System"
        
        logger.info(f"Theme changed to: {selected_theme}")
        
        # Apply theme immediately
        style_manager = Adw.StyleManager.get_default()
        
        if selected == 0:  # Follow System
            style_manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
        elif selected == 1:  # Light
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        elif selected == 2:  # Dark
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        
        # TODO: Save theme preference to config
    
    def get_theme_name_mapping(self):
        """Get mapping between display names and config keys"""
        return {
            "Default": "default",
            "Solarized Dark": "solarized_dark", 
            "Solarized Light": "solarized_light",
            "Monokai": "monokai",
            "Dracula": "dracula",
            "Nord": "nord"
        }
    
    def get_reverse_theme_mapping(self):
        """Get mapping from config keys to display names"""
        mapping = self.get_theme_name_mapping()
        return {v: k for k, v in mapping.items()}
    
    def on_color_scheme_changed(self, combo_row, param):
        """Handle terminal color scheme change"""
        selected = combo_row.get_selected()
        scheme_names = ["Default", "Solarized Dark", "Solarized Light", "Monokai", "Dracula", "Nord"]
        selected_scheme = scheme_names[selected] if selected < len(scheme_names) else "Default"
        
        logger.info(f"Terminal color scheme changed to: {selected_scheme}")
        
        # Convert display name to config key
        theme_mapping = self.get_theme_name_mapping()
        config_key = theme_mapping.get(selected_scheme, "default")
        
        # Save to config using the correct key
        self.config.set_setting('terminal-color-scheme', config_key)
        
        # Apply to all active terminals using config key
        self.apply_color_scheme_to_terminals(config_key)
        
    def on_confirm_disconnect_changed(self, switch, *args):
        """Handle confirm disconnect setting change"""
        confirm = switch.get_active()
        logger.info(f"Confirm before disconnect setting changed to: {confirm}")
        self.config.set_setting('confirm-disconnect', confirm)
    
    def apply_color_scheme_to_terminals(self, scheme_key):
        """Apply color scheme to all active terminal widgets"""
        try:
            parent_window = self.get_transient_for()
            
            if parent_window and hasattr(parent_window, 'active_terminals'):
                for terminal in parent_window.active_terminals.values():
                    if hasattr(terminal, 'apply_theme'):
                        # Use the terminal's apply_theme method which will get the theme from config
                        terminal.apply_theme(scheme_key)
                
                logger.info(f"Applied color scheme {scheme_key} to {len(parent_window.active_terminals)} terminals")
        except Exception as e:
            logger.error(f"Failed to apply color scheme to terminals: {e}")

class MainWindow(Adw.ApplicationWindow):
    """Main application window"""
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        # Initialize managers
        self.connection_manager = ConnectionManager()
        self.config = Config()
        self.key_manager = KeyManager()
        
        # UI state
        self.active_terminals = {}  # connection -> terminal_widget
        self.connection_rows = {}   # connection -> row_widget
        
        # Set up window
        self.setup_window()
        self.setup_ui()
        self.setup_connections()
        self.setup_signals()
        
        # Add action for activating connections
        self.activate_action = Gio.SimpleAction.new('activate-connection', None)
        self.activate_action.connect('activate', self.on_activate_connection)
        self.add_action(self.activate_action)
        
        # Connect to close request signal
        self.connect('close-request', self.on_close_request)
        
        # Start with welcome view (tab view setup already shows welcome initially)
        
        logger.info("Main window initialized")

    def setup_window(self):
        """Configure main window properties"""
        self.set_title('sshPilot')
        self.set_icon_name('io.github.mfat.sshpilot')
        
        # Load window geometry
        geometry = self.config.get_window_geometry()
        self.set_default_size(geometry['width'], geometry['height'])
        
        # Connect window state signals
        self.connect('notify::default-width', self.on_window_size_changed)
        self.connect('notify::default-height', self.on_window_size_changed)

    def setup_ui(self):
        """Set up the user interface"""
        # Create main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Create header bar
        self.header_bar = Adw.HeaderBar()
        self.header_bar.set_title_widget(Gtk.Label(label="sshPilot"))
        
        # Add window controls (minimize, maximize, close)
        self.header_bar.set_show_start_title_buttons(True)
        self.header_bar.set_show_end_title_buttons(True)
        
        # Add header bar to main container
        main_box.append(self.header_bar)
        
        # Create main layout
        self.split_view = Adw.OverlaySplitView()
        self.split_view.set_sidebar_width_fraction(0.25)
        self.split_view.set_min_sidebar_width(200)
        self.split_view.set_max_sidebar_width(400)
        self.split_view.set_vexpand(True)
        
        # Create sidebar
        self.setup_sidebar()
        
        # Create main content area
        self.setup_content_area()
        
        # Add split view to main container
        main_box.append(self.split_view)
        
        # Set as window content
        self.set_content(main_box)

    def setup_sidebar(self):
        """Set up the sidebar with connection list"""
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Sidebar header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_start(12)
        header.set_margin_end(12)
        header.set_margin_top(12)
        header.set_margin_bottom(6)
        
        # Title
        title_label = Gtk.Label()
        title_label.set_markup('<b>Connections</b>')
        title_label.set_halign(Gtk.Align.START)
        title_label.set_hexpand(True)
        header.append(title_label)
        
        # Add connection button
        add_button = Gtk.Button.new_from_icon_name('list-add-symbolic')
        add_button.set_tooltip_text('Add Connection (Ctrl+N)')
        add_button.connect('clicked', self.on_add_connection_clicked)
        header.append(add_button)
        
        sidebar_box.append(header)
        
        # Connection list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        
        self.connection_list = Gtk.ListBox()
        self.connection_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        
        # Connect signals
        self.connection_list.connect('row-selected', self.on_connection_selected)  # For button sensitivity
        self.connection_list.connect('row-activated', self.on_connection_activated)  # For Enter key/double-click
        
        # Make sure the connection list is focusable and can receive key events
        self.connection_list.set_focusable(True)
        self.connection_list.set_can_focus(True)
        self.connection_list.set_focus_on_click(True)
        self.connection_list.set_activate_on_single_click(False)  # Require double-click to activate
        
        # Set up drag and drop for reordering
        self.setup_connection_list_dnd()
        
        scrolled.set_child(self.connection_list)
        sidebar_box.append(scrolled)
        
        # Sidebar toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_start(6)
        toolbar.set_margin_end(6)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(6)
        toolbar.add_css_class('toolbar')
        
        # Edit button
        self.edit_button = Gtk.Button.new_from_icon_name('document-edit-symbolic')
        self.edit_button.set_tooltip_text('Edit Connection')
        self.edit_button.set_sensitive(False)
        self.edit_button.connect('clicked', self.on_edit_connection_clicked)
        toolbar.append(self.edit_button)
        
        # Delete button
        self.delete_button = Gtk.Button.new_from_icon_name('user-trash-symbolic')
        self.delete_button.set_tooltip_text('Delete Connection')
        self.delete_button.set_sensitive(False)
        self.delete_button.connect('clicked', self.on_delete_connection_clicked)
        toolbar.append(self.delete_button)
        
        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        toolbar.append(spacer)
        
        # Menu button
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name('open-menu-symbolic')
        menu_button.set_tooltip_text('Menu')
        menu_button.set_menu_model(self.create_menu())
        toolbar.append(menu_button)
        
        sidebar_box.append(toolbar)
        
        self.split_view.set_sidebar(sidebar_box)

    def setup_content_area(self):
        """Set up the main content area with stack for tabs and welcome view"""
        # Create stack to switch between welcome view and tab view
        self.content_stack = Gtk.Stack()
        self.content_stack.set_hexpand(True)
        self.content_stack.set_vexpand(True)
        
        # Create welcome/help view
        self.welcome_view = WelcomePage()
        self.content_stack.add_named(self.welcome_view, "welcome")
        
        # Create tab view
        self.tab_view = Adw.TabView()
        self.tab_view.set_hexpand(True)
        self.tab_view.set_vexpand(True)
        
        # Connect tab signals
        self.tab_view.connect('close-page', self.on_tab_close)
        self.tab_view.connect('page-attached', self.on_tab_attached)
        self.tab_view.connect('page-detached', self.on_tab_detached)
        
        # Create tab bar
        self.tab_bar = Adw.TabBar()
        self.tab_bar.set_view(self.tab_view)
        self.tab_bar.set_autohide(False)
        
        # Create tab content box
        tab_content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tab_content_box.append(self.tab_bar)
        tab_content_box.append(self.tab_view)
        
        self.content_stack.add_named(tab_content_box, "tabs")
        
        # Start with welcome view visible
        self.content_stack.set_visible_child_name("welcome")
        
        self.split_view.set_content(self.content_stack)

    def setup_connection_list_dnd(self):
        """Set up drag and drop for connection list reordering"""
        # TODO: Implement drag and drop reordering
        pass

    def create_menu(self):
        """Create application menu"""
        menu = Gio.Menu()
        
        # Add all menu items directly to the main menu
        menu.append('New Connection', 'app.new-connection')
        menu.append('Generate SSH Key', 'app.new-key')
        menu.append('Preferences', 'app.preferences')
        menu.append('About', 'app.about')
        menu.append('Quit', 'app.quit')
        
        return menu

    def setup_connections(self):
        """Load and display existing connections"""
        connections = self.connection_manager.get_connections()
        
        for connection in connections:
            self.add_connection_row(connection)
        
        # Select first connection if available
        if connections:
            first_row = self.connection_list.get_row_at_index(0)
            if first_row:
                self.connection_list.select_row(first_row)

    def setup_signals(self):
        """Connect to manager signals"""
        # Connection manager signals - use connect_after to avoid conflict with GObject.connect
        self.connection_manager.connect_after('connection-added', self.on_connection_added)
        self.connection_manager.connect_after('connection-removed', self.on_connection_removed)
        self.connection_manager.connect_after('connection-status-changed', self.on_connection_status_changed)
        
        # Config signals
        self.config.connect('setting-changed', self.on_setting_changed)

    def add_connection_row(self, connection: Connection):
        """Add a connection row to the list"""
        row = ConnectionRow(connection)
        self.connection_list.append(row)
        self.connection_rows[connection] = row

    def show_welcome_view(self):
        """Show the welcome/help view when no connections are active"""
        self.content_stack.set_visible_child_name("welcome")
        logger.info("Showing welcome view")
    
    def show_tab_view(self):
        """Show the tab view when connections are active"""
        self.content_stack.set_visible_child_name("tabs")
        logger.info("Showing tab view")

    def show_connection_dialog(self, connection: Connection = None):
        """Show connection dialog for adding/editing connections"""
        logger.info(f"Show connection dialog for: {connection}")
        
        # Create connection dialog
        dialog = ConnectionDialog(self, connection)
        dialog.connect('connection-saved', self.on_connection_saved)
        dialog.present()

    def show_key_dialog(self):
        """Show SSH key generation dialog"""
        # TODO: Implement key dialog
        logger.info("Show key generation dialog")

    def show_preferences(self):
        """Show preferences dialog"""
        logger.info("Show preferences dialog")
        try:
            preferences_window = PreferencesWindow(self, self.config)
            preferences_window.present()
        except Exception as e:
            logger.error(f"Failed to show preferences dialog: {e}")

    def show_about_dialog(self):
        """Show about dialog"""
        about = Adw.AboutWindow()
        about.set_transient_for(self)
        about.set_application_name('sshPilot')
        about.set_application_icon('io.github.mfat.sshpilot')
        about.set_version('1.0.0')
        about.set_developer_name('mFat')
        about.set_license_type(Gtk.License.GPL_3_0)
        about.set_website('https://github.com/mfat/sshpilot')
        about.set_issue_url('https://github.com/mfat/sshpilot/issues')
        about.set_copyright(' 2025 mFat')
        about.set_developers(['mFat <newmfat@gmail.com>'])
        about.set_comments('SSH connection manager with integrated terminal')
        
        about.present()

    def toggle_list_focus(self):
        """Toggle focus between connection list and terminal"""
        if self.connection_list.has_focus():
            # Focus current terminal
            current_page = self.tab_view.get_selected_page()
            if current_page:
                child = current_page.get_child()
                if hasattr(child, 'vte'):
                    child.vte.grab_focus()
        else:
            # Focus connection list
            self.connection_list.grab_focus()

    def connect_to_host(self, connection: Connection):
        """Connect to SSH host and create terminal tab"""
        if connection in self.active_terminals:
            # Already connected, activate existing tab
            terminal = self.active_terminals[connection]
            page = self.tab_view.get_page(terminal)
            if page is not None:
                self.tab_view.set_selected_page(page)
                return
            else:
                # Terminal exists but not in tab view, remove from active terminals
                logger.warning(f"Terminal for {connection.nickname} not found in tab view, removing from active terminals")
                del self.active_terminals[connection]
        
        # Create new terminal
        terminal = TerminalWidget(connection, self.config, self.connection_manager)
        
        # Connect signals
        terminal.connect('connection-established', self.on_terminal_connected)
        terminal.connect('connection-failed', lambda w, e: logger.error(f"Connection failed: {e}"))
        terminal.connect('connection-lost', self.on_terminal_disconnected)
        terminal.connect('title-changed', self.on_terminal_title_changed)
        
        # Add to tab view
        page = self.tab_view.append(terminal)
        page.set_title(connection.nickname)
        page.set_icon(Gio.ThemedIcon.new('utilities-terminal-symbolic'))
        
        # Store reference
        self.active_terminals[connection] = terminal
        
        # Switch to tab view when first connection is made
        self.show_tab_view()
        
        # Activate the new tab
        self.tab_view.set_selected_page(page)
        
        # Force set colors after the terminal is added to the UI
        def _set_terminal_colors():
            try:
                # Set colors using RGBA
                fg = Gdk.RGBA()
                fg.parse('rgb(0,0,0)')  # Black
                
                bg = Gdk.RGBA()
                bg.parse('rgb(255,255,255)')  # White
                
                # Set colors using both methods for maximum compatibility
                terminal.vte.set_color_foreground(fg)
                terminal.vte.set_color_background(bg)
                terminal.vte.set_colors(fg, bg, None)
                
                # Force a redraw
                terminal.vte.queue_draw()
                
                # Connect to the SSH server after setting colors
                if not terminal._connect_ssh():
                    logger.error("Failed to establish SSH connection")
                    self.tab_view.close_page(page)
                    if connection in self.active_terminals:
                        del self.active_terminals[connection]
                        
            except Exception as e:
                logger.error(f"Error setting terminal colors: {e}")
                # Still try to connect even if color setting fails
                if not terminal._connect_ssh():
                    logger.error("Failed to establish SSH connection")
                    self.tab_view.close_page(page)
                    if connection in self.active_terminals:
                        del self.active_terminals[connection]
        
        # Schedule the color setting to run after the terminal is fully initialized
        GLib.idle_add(_set_terminal_colors)

    def _on_disconnect_confirmed(self, dialog, response_id, connection):
        """Handle response from disconnect confirmation dialog"""
        dialog.destroy()
        if response_id == 'disconnect' and connection in self.active_terminals:
            terminal = self.active_terminals[connection]
            terminal.disconnect()
    
    def disconnect_from_host(self, connection: Connection):
        """Disconnect from SSH host"""
        if connection not in self.active_terminals:
            return
            
        # Check if confirmation is required
        confirm_disconnect = self.config.get_setting('confirm-disconnect', True)
        
        if confirm_disconnect:
            # Show confirmation dialog
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Disconnect from {}").format(connection.nickname or connection.host),
                body=_("Are you sure you want to disconnect from this host?")
            )
            dialog.add_response('cancel', _("Cancel"))
            dialog.add_response('disconnect', _("Disconnect"))
            dialog.set_response_appearance('disconnect', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('cancel')
            dialog.connect('response', self._on_disconnect_confirmed, connection)
            dialog.present()
        else:
            # Disconnect immediately without confirmation
            terminal = self.active_terminals[connection]
            terminal.disconnect()

    # Signal handlers
    def on_connection_click(self, gesture, n_press, x, y):
        """Handle clicks on the connection list"""
        # Get the row that was clicked
        row = self.connection_list.get_row_at_y(int(y))
        if row is None:
            return
        
        if n_press == 1:  # Single click - just select
            self.connection_list.select_row(row)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        elif n_press == 2:  # Double click - connect
            self.connect_to_host(row.connection)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def on_connection_activated(self, list_box, row):
        """Handle connection activation (Enter key)"""
        if row:
            self.connect_to_host(row.connection)
            

        
    def on_connection_activate(self, list_box, row):
        """Handle connection activation (Enter key or double-click)"""
        if row:
            self.connect_to_host(row.connection)
            return True  # Stop event propagation
        return False
        
    def on_activate_connection(self, action, param):
        """Handle the activate-connection action"""
        row = self.connection_list.get_selected_row()
        if row:
            self.connect_to_host(row.connection)
            
    def on_connection_activated(self, list_box, row):
        """Handle connection activation (double-click)"""
        if row:
            self.connect_to_host(row.connection)

    def on_connection_selected(self, list_box, row):
        """Handle connection list selection change"""
        has_selection = row is not None
        self.edit_button.set_sensitive(has_selection)
        self.delete_button.set_sensitive(has_selection)

    def on_add_connection_clicked(self, button):
        """Handle add connection button click"""
        self.show_connection_dialog()

    def on_edit_connection_clicked(self, button):
        """Handle edit connection button click"""
        selected_row = self.connection_list.get_selected_row()
        if selected_row:
            self.show_connection_dialog(selected_row.connection)

    def on_delete_connection_clicked(self, button):
        """Handle delete connection button click"""
        selected_row = self.connection_list.get_selected_row()
        if not selected_row:
            return
        
        connection = selected_row.connection
        
        # Show confirmation dialog
        dialog = Adw.MessageDialog.new(self, 'Delete Connection?', 
                                     f'Are you sure you want to delete "{connection.nickname}"?')
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('delete', 'Delete')
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        
        dialog.connect('response', self.on_delete_connection_response, connection)
        dialog.present()

    def on_delete_connection_response(self, dialog, response, connection):
        """Handle delete connection dialog response"""
        if response == 'delete':
            # Disconnect if connected
            if connection.is_connected:
                self.disconnect_from_host(connection)
            
            # Remove connection
            self.connection_manager.remove_connection(connection)

    def _on_tab_close_confirmed(self, dialog, response_id, tab_view, page):
        """Handle response from tab close confirmation dialog"""
        dialog.destroy()
        if response_id == 'close':
            self._close_tab(tab_view, page)
        # If cancelled, do nothing - the tab remains open
    
    def _close_tab(self, tab_view, page):
        """Close the tab and clean up resources"""
        if hasattr(page, 'get_child'):
            child = page.get_child()
            if hasattr(child, 'disconnect'):
                # Get the connection associated with this terminal
                for connection, terminal in list(self.active_terminals.items()):
                    if terminal == child:
                        # Disconnect the terminal
                        child.disconnect()
                        # Remove from active terminals
                        if connection in self.active_terminals:
                            del self.active_terminals[connection]
                        break
        
        # Close the tab page
        tab_view.close_page(page)
        
        # Update the UI based on the number of remaining tabs
        GLib.idle_add(self._update_ui_after_tab_close)
    
    def on_tab_close(self, tab_view, page):
        """Handle tab close - THE KEY FIX: Never call close_page ourselves"""
        # Get the connection for this tab
        connection = None
        terminal = None
        if hasattr(page, 'get_child'):
            child = page.get_child()
            if hasattr(child, 'disconnect'):
                for conn, term in list(self.active_terminals.items()):
                    if term == child:
                        connection = conn
                        terminal = term
                        break
        
        if not connection:
            # For non-terminal tabs, allow immediate close
            return False  # Allow the default close behavior
        
        # Check if confirmation is required
        confirm_disconnect = self.config.get_setting('confirm-disconnect', True)
        
        if confirm_disconnect:
            # Store tab view and page as instance variables
            self._pending_close_tab_view = tab_view
            self._pending_close_page = page
            self._pending_close_connection = connection
            self._pending_close_terminal = terminal
            
            # Show confirmation dialog
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Close connection to {}").format(connection.nickname or connection.host),
                body=_("Are you sure you want to close this connection?")
            )
            dialog.add_response('cancel', _("Cancel"))
            dialog.add_response('close', _("Close"))
            dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('cancel')
            
            # Connect to response signal before showing the dialog
            dialog.connect('response', self._on_tab_close_response)
            dialog.present()
            
            # Prevent the default close behavior while we show confirmation
            return True
        else:
            # If no confirmation is needed, just allow the default close behavior.
            # The default handler will close the page, which in turn triggers the
            # terminal disconnection via the page's 'unmap' or 'destroy' signal.
            return False

    def _on_tab_close_response(self, dialog, response_id):
        """Handle the response from the close confirmation dialog."""
        # Retrieve the pending tab info
        tab_view = self._pending_close_tab_view
        page = self._pending_close_page
        terminal = self._pending_close_terminal

        if response_id == 'close':
            # User confirmed, disconnect the terminal. The tab will be removed
            # by the AdwTabView once we finish the close operation.
            if terminal and hasattr(terminal, 'disconnect'):
                terminal.disconnect()
            # Now, tell the tab view to finish closing the page.
            tab_view.close_page_finish(page, True)
            
            # Check if this was the last tab and show welcome screen if needed
            if tab_view.get_n_pages() == 0:
                self.show_welcome_view()
        else:
            # User cancelled, so we reject the close request.
            # This is the critical step that makes the close button work again.
            tab_view.close_page_finish(page, False)

        dialog.destroy()
        # Clear pending state to avoid memory leaks
        self._pending_close_tab_view = None
        self._pending_close_page = None
        self._pending_close_connection = None
        self._pending_close_terminal = None
    
    def on_tab_attached(self, tab_view, page, position):
        """Handle tab attached"""
        pass

    def on_tab_detached(self, tab_view, page, position):
        """Handle tab detached"""
        # Show welcome view if no more tabs are left
        if tab_view.get_n_pages() == 0:
            self.show_welcome_view()

    def on_terminal_connected(self, terminal):
        """Handle terminal connection established"""
        logger.info(f"Terminal connected: {terminal.connection}")

    def on_terminal_disconnected(self, terminal):
        """Handle terminal connection lost"""
        logger.info(f"Terminal disconnected: {terminal.connection}")
        
        # Update connection row status
        if terminal.connection in self.connection_rows:
            row = self.connection_rows[terminal.connection]
            row.update_status()

    def on_terminal_title_changed(self, terminal, title):
        """Handle terminal title change"""
        # Update tab title
        page = self.tab_view.get_page(terminal)
        if page:
            # Use connection nickname with title suffix if available
            base_title = terminal.connection.nickname
            if title and title != terminal.connection.nickname:
                page.set_title(f"{base_title} - {title}")
            else:
                page.set_title(base_title)

    def on_connection_added(self, manager, connection):
        """Handle new connection added"""
        self.add_connection_row(connection)

    def on_connection_removed(self, manager, connection):
        """Handle connection removed"""
        # Remove from UI
        if connection in self.connection_rows:
            row = self.connection_rows[connection]
            self.connection_list.remove(row)
            del self.connection_rows[connection]
        
        # Close terminal if open
        if connection in self.active_terminals:
            terminal = self.active_terminals[connection]
            page = self.tab_view.get_page(terminal)
            self.tab_view.close_page(page)

    def on_connection_status_changed(self, manager, connection, is_connected):
        """Handle connection status change"""
        if connection in self.connection_rows:
            row = self.connection_rows[connection]
            row.update_status()

    def on_setting_changed(self, config, key, value):
        """Handle configuration setting change"""
        logger.debug(f"Setting changed: {key} = {value}")
        
        # Apply relevant changes
        if key.startswith('terminal.'):
            # Update terminal themes/fonts
            for terminal in self.active_terminals.values():
                terminal.apply_theme()

    def on_window_size_changed(self, window, param):
        """Handle window size change"""
        width = self.get_default_size()[0]
        height = self.get_default_size()[1]
        sidebar_width = self.split_view.get_sidebar_width()
        
        self.config.save_window_geometry(width, height, sidebar_width)

    def simple_close_handler(self, window):
        """Handle window close - distinguish between tab close and window close"""
        logger.info("")
        
        try:
            # Check if we have any tabs open
            n_pages = self.tab_view.get_n_pages()
            logger.info(f" Number of tabs: {n_pages}")
            
            # If we have tabs, close all tabs first and then quit
            if n_pages > 0:
                logger.info(" CLOSING ALL TABS FIRST")
                # Close all tabs
                while self.tab_view.get_n_pages() > 0:
                    page = self.tab_view.get_nth_page(0)
                    self.tab_view.close_page(page)
            
            # Now quit the application
            logger.info(" QUITTING APPLICATION")
            app = self.get_application()
            if app:
                app.quit()
                
        except Exception as e:
            logger.error(f" ERROR IN WINDOW CLOSE: {e}")
            # Force quit even if there's an error
            app = self.get_application()
            if app:
                # Call super().quit() directly to avoid recursion
                super(type(app), app).quit()
            else:
                # Fallback: force close the window
                self.close()
    
    def check_active_connections_before_quit(self):
        """Check for active connections before quitting - returns True if safe to quit"""
        if self.active_terminals:
            self.show_quit_confirmation_dialog()
            return False  # Don't quit yet, let dialog handle it
        
        # No active connections, safe to quit
        self.cleanup_and_close()
        return True  # Safe to quit
    
    def do_close_request(self):
        """Handle window close request - show warning if active connections exist"""
        if self.check_active_connections_before_quit():
            return self.cleanup_and_close()
        return False
        
    def on_close_request(self, window):
        """Handle window close request"""
        # If there are active connections, show warning dialog
        if self.active_terminals:
            self.show_quit_confirmation_dialog()
            return True  # Don't close yet, let the dialog handle it
            
        # No active connections, proceed with normal close
        return self.do_close_request()

    def show_quit_confirmation_dialog(self):
        """Show confirmation dialog when quitting with active connections"""
        active_count = len(self.active_terminals)
        connection_names = [conn.nickname for conn in self.active_terminals.keys()]
        
        if active_count == 1:
            message = f"You have 1 active SSH connection to '{connection_names[0]}'."
            detail = "Closing the application will disconnect this connection."
        else:
            message = f"You have {active_count} active SSH connections."
            detail = f"Closing the application will disconnect all connections:\n• " + "\n• ".join(connection_names)
        
        dialog = Adw.AlertDialog()
        dialog.set_heading("Active SSH Connections")
        dialog.set_body(f"{message}\n\n{detail}")
        
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('quit', 'Quit Anyway')
        dialog.set_response_appearance('quit', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        
        dialog.connect('response', self.on_quit_confirmation_response)
        dialog.present(self)
    
    def on_quit_confirmation_response(self, dialog, response):
        """Handle quit confirmation dialog response"""
        if response == 'quit':
            # First, close the dialog to free up UI (use close() for Adw.AlertDialog)
            dialog.close()
            
            # Schedule cleanup and quit on the main loop to ensure proper shutdown
            def _cleanup_and_quit():
                try:
                    logger.info("Starting cleanup before quit...")
                    
                    # 1. First, disconnect all terminals
                    for connection, terminal in list(self.active_terminals.items()):
                        try:
                            logger.debug(f"Disconnecting terminal for {connection.nickname}")
                            if hasattr(terminal, 'disconnect'):
                                terminal.disconnect()
                            if hasattr(terminal, 'close_connection'):
                                terminal.close_connection()
                        except Exception as e:
                            logger.error(f"Error disconnecting terminal {connection.nickname}: {e}", exc_info=True)
                    
                    # 2. Clear active terminals
                    self.active_terminals.clear()
                    
                    # 3. Force cleanup of all processes via process manager
                    try:
                        from sshpilot.terminal import process_manager
                        process_manager.cleanup_all()
                    except Exception as e:
                        logger.error(f"Error in process manager cleanup: {e}", exc_info=True)
                    
                    # 4. Close all tabs
                    while self.tab_view.get_n_pages() > 0:
                        try:
                            page = self.tab_view.get_nth_page(0)
                            if page is not None:
                                self.tab_view.close_page(page)
                        except Exception as e:
                            logger.error(f"Error closing tab: {e}", exc_info=True)
                            break
                    
                    # 5. Force garbage collection
                    import gc
                    gc.collect()
                    
                    # 6. Close window and quit application
                    logger.info("Cleanup complete, quitting application...")
                    self.close()
                    
                    app = self.get_application()
                    if app:
                        # Use GLib to ensure we're on the main thread
                        def _quit():
                            app.quit()
                            return False
                        from gi.repository import GLib
                        GLib.idle_add(_quit)
                    
                except Exception as e:
                    logger.critical(f"Critical error during shutdown: {e}", exc_info=True)
                    # Try to force quit if we get here
                    import os
                    import signal
                    os.kill(os.getpid(), signal.SIGKILL)
                
                return False  # Remove this source from the main loop
            
            # Schedule the cleanup on the main loop with high priority
            from gi.repository import GLib
            GLib.idle_add(_cleanup_and_quit, priority=GLib.PRIORITY_HIGH)
    
    def cleanup_and_close(self):
        """Perform cleanup before closing"""
        try:
            logger.info("Starting application cleanup...")
            
            # Create a copy of active_terminals to avoid modification during iteration
            terminals_to_close = list(self.active_terminals.items())
            
            # First, disconnect all terminals to properly close SSH connections
            for connection, terminal in terminals_to_close:
                try:
                    logger.debug(f"Disconnecting terminal for {connection.nickname}")
                    # Call disconnect() to properly clean up SSH processes
                    if hasattr(terminal, 'disconnect'):
                        terminal.disconnect()
                    # Also call close_connection if it exists
                    if hasattr(terminal, 'close_connection'):
                        terminal.close_connection()
                except Exception as e:
                    logger.error(f"Error disconnecting terminal {connection.nickname}: {e}", exc_info=True)
            
            # Close all tabs
            while self.tab_view.get_n_pages() > 0:
                try:
                    page = self.tab_view.get_nth_page(0)
                    if page is not None:
                        self.tab_view.close_page(page)
                except Exception as e:
                    logger.error(f"Error closing tab: {e}", exc_info=True)
                    break  # Prevent infinite loop if we can't close a tab
            
            # Clear the active terminals dictionary
            self.active_terminals.clear()
            
            # Force garbage collection to ensure all terminal widgets are destroyed
            import gc
            gc.collect()
            
            # Save window geometry before closing
            try:
                width = self.get_default_size()[0]
                height = self.get_default_size()[1]
                sidebar_width = self.split_view.get_sidebar_width() if hasattr(self, 'split_view') else 250
                self.config.save_window_geometry(width, height, sidebar_width)
            except Exception as e:
                logger.error(f"Error saving window geometry: {e}", exc_info=True)
            
            logger.info("Application cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during window close cleanup: {e}", exc_info=True)
            
        # Ensure we clean up any remaining processes through the process manager
        try:
            from sshpilot.terminal import process_manager
            process_manager.cleanup_all()
        except Exception as e:
            logger.error(f"Error during process manager cleanup: {e}", exc_info=True)

    def _cleanup_connection(self, connection, terminal):
        """Clean up connection resources without closing the tab"""
        try:
            # Disconnect the terminal
            if terminal and hasattr(terminal, 'disconnect'):
                terminal.disconnect()
            
            # Remove from active terminals
            if connection and connection in self.active_terminals:
                del self.active_terminals[connection]
            
            # Update UI after cleanup
            GLib.idle_add(self._update_ui_after_tab_close)
            
            logger.info(f"Cleaned up connection: {connection.nickname}")
            
        except Exception as e:
            logger.error(f"Error cleaning up connection: {e}")
    
    def _update_ui_after_tab_close(self):
        """Update the UI after a tab has been closed"""
        # Show welcome view if no more tabs, otherwise show tab view
        if self.tab_view.get_n_pages() == 0:
            self.welcome_view.set_visible(True)
            self.tab_view.set_visible(False)
            self.header_bar.remove(self.tab_switcher)
            self.header_bar.set_title_widget(None)
            self.header_bar.set_title_widget(self.header_title)
        else:
            self.welcome_view.set_visible(False)
            self.tab_view.set_visible(True)
            # Update tab titles in case they've changed
            self._update_tab_titles()
    
    def _update_tab_titles(self):
        """Update tab titles"""
        for page in self.tab_view.get_pages():
            child = page.get_child()
            if hasattr(child, 'connection'):
                page.set_title(child.connection.nickname)
    
    def on_connection_saved(self, dialog, connection_data):
        """Handle connection saved from dialog"""
        try:
            if dialog.is_editing:
                # Update existing connection
                old_connection = dialog.connection
                is_connected = old_connection in self.active_terminals
                
                if self.connection_manager.update_connection(old_connection, connection_data):
                    # Update connection attributes
                    old_connection.nickname = connection_data['nickname']
                    old_connection.host = connection_data['host']
                    old_connection.username = connection_data['username']
                    old_connection.port = connection_data['port']
                    old_connection.keyfile = connection_data['keyfile']
                    old_connection.password = connection_data['password']
                    old_connection.key_passphrase = connection_data['key_passphrase']
                    old_connection.auth_method = connection_data['auth_method']
                    old_connection.x11_forwarding = connection_data['x11_forwarding']
                    
                    # Update UI
                    if old_connection in self.connection_rows:
                        row = self.connection_rows[old_connection]
                        row.update_display()
                    
                    logger.info(f"Updated connection: {old_connection.nickname}")
                    
                    # If the connection is active, ask if user wants to reconnect
                    if is_connected:
                        self._prompt_reconnect(old_connection)
                else:
                    logger.error("Failed to update connection in SSH config")
                
            else:
                # Create new connection
                if self.connection_manager.save_connection(connection_data):
                    # Connection will be added to UI automatically via 'connection-added' signal
                    logger.info(f"Created new connection: {connection_data['nickname']}")
                else:
                    logger.error("Failed to save connection to SSH config")
                
        except Exception as e:
            logger.error(f"Failed to save connection: {e}")
            # Show error dialog
            error_dialog = Adw.MessageDialog.new(self, 'Error', f'Failed to save connection: {e}')
            error_dialog.add_response('ok', 'OK')
            error_dialog.set_default_response('ok')
            error_dialog.present()
            
    def _prompt_reconnect(self, connection):
        """Show a dialog asking if user wants to reconnect with new settings"""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Settings Changed"),
            secondary_text=_("The connection settings have been updated.\n"
                           "Would you like to reconnect with the new settings?"),
        )
        
        dialog.connect("response", self._on_reconnect_response, connection)
        dialog.present()
    
    def _on_reconnect_response(self, dialog, response_id, connection):
        """Handle response from reconnect prompt"""
        dialog.destroy()
        
        if response_id == Gtk.ResponseType.YES and connection in self.active_terminals:
            # Disconnect and reconnect with new settings
            terminal = self.active_terminals[connection]
            # Disconnect first
            terminal.disconnect()
            # Reconnect after a short delay to allow disconnection to complete
            GLib.timeout_add(500, self._reconnect_terminal, connection)
    
    def _reconnect_terminal(self, connection):
        """Reconnect a terminal with updated connection settings"""
        if connection in self.active_terminals:
            terminal = self.active_terminals[connection]
            # Reconnect with new settings
            if not terminal._connect_ssh():
                logger.error("Failed to reconnect with new settings")
                # Remove from active terminals if reconnection fails
                if connection in self.active_terminals:
                    del self.active_terminals[connection]
        return False  # Don't repeat the timeout