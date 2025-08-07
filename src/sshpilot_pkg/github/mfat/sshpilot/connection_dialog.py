"""
Connection Dialog for sshPilot
Dialog for adding/editing SSH connections
"""

import os
import logging
import gettext
from typing import Optional, Dict, Any

from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gdk

# Initialize gettext
try:
    from . import gettext as _
except ImportError:
    # Fallback for when gettext is not available
    _ = lambda s: s

logger = logging.getLogger(__name__)

class ConnectionDialog(Adw.Window):
    """Dialog for adding/editing SSH connections"""
    
    __gtype_name__ = 'ConnectionDialog'
    
    __gsignals__ = {
        'connection-saved': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }
    
    def __init__(self, parent, connection=None):
        super().__init__()
        
        self.parent_window = parent
        self.connection = connection
        self.is_editing = connection is not None
        
        self.set_title('Edit Connection' if self.is_editing else 'New Connection')
        self.set_default_size(600, 700)
        self.set_modal(True)
        self.set_resizable(True)
        self.set_transient_for(parent)
        self.set_size_request(500, 500)  # Minimum size
        
        self.setup_ui()
        self.load_connection_data()
    
    def on_auth_method_changed(self, combo_row, param):
        """Handle authentication method change"""
        is_key_based = combo_row.get_selected() == 0  # 0 is for key-based auth
        
        # Show/hide key file and passphrase fields for key-based auth
        if hasattr(self, 'keyfile_row'):
            self.keyfile_row.set_visible(is_key_based)
        if hasattr(self, 'key_passphrase_row'):
            self.key_passphrase_row.set_visible(is_key_based)
            
        # Show/hide password field for password-based auth
        if hasattr(self, 'password_row'):
            self.password_row.set_visible(not is_key_based)
    
    def load_connection_data(self):
        """Load connection data into the dialog fields"""
        if not self.is_editing or not self.connection:
            return
        
        try:
            # Load basic connection data
            if hasattr(self.connection, 'nickname'):
                self.nickname_row.set_text(self.connection.nickname or "")
            if hasattr(self.connection, 'host'):
                self.host_row.set_text(self.connection.host or "")
            if hasattr(self.connection, 'username'):
                self.username_row.set_text(self.connection.username or "")
            if hasattr(self.connection, 'port'):
                self.port_row.set_value(int(self.connection.port) if self.connection.port else 22)
            
            # Set authentication method and related fields
            auth_method = getattr(self.connection, 'auth_method', 0)
            self.auth_method_row.set_selected(auth_method)
            self.on_auth_method_changed(self.auth_method_row, None)  # Update UI state
            
            if hasattr(self.connection, 'keyfile') and self.connection.keyfile:
                self.keyfile_row.set_subtitle(self.connection.keyfile)
            
            if hasattr(self.connection, 'password') and self.connection.password:
                self.password_row.set_text(self.connection.password)
                
            if hasattr(self.connection, 'key_passphrase') and self.connection.key_passphrase:
                self.key_passphrase_row.set_text(self.connection.key_passphrase)
            
            # Set X11 forwarding
            self.x11_row.set_active(getattr(self.connection, 'x11_forwarding', False))
            
            # Initialize forwarding rules list if it doesn't exist
            if not hasattr(self, 'forwarding_rules'):
                self.forwarding_rules = []
                
            # Initialize forwarding_rules if it doesn't exist
            if not hasattr(self, 'forwarding_rules'):
                self.forwarding_rules = []
                
            # Initialize forwarding_rules if it doesn't exist
            if not hasattr(self, 'forwarding_rules'):
                self.forwarding_rules = []
                
            # Load port forwarding rules
            if hasattr(self.connection, 'forwarding_rules') and self.connection.forwarding_rules:
                self.forwarding_rules = self.connection.forwarding_rules
                logger.debug(f"Loaded forwarding rules: {self.forwarding_rules}")
                
                # Reset all toggles and hide settings boxes first
                toggle_map = {
                    'local_forwarding_enabled': ('local_settings_box', 'local'),
                    'remote_forwarding_enabled': ('remote_settings_box', 'remote'),
                    'dynamic_forwarding_enabled': ('dynamic_settings_box', 'dynamic')
                }
                
                for toggle_name, (box_name, rule_type) in toggle_map.items():
                    if hasattr(self, toggle_name) and hasattr(self, box_name):
                        # Initialize toggle state
                        toggle = getattr(self, toggle_name)
                        box = getattr(self, box_name)
                        
                        # Check if we have a rule of this type
                        has_rule = any(r.get('type') == rule_type and r.get('enabled', True) 
                                     for r in self.forwarding_rules)
                        
                        # Set toggle state and box visibility
                        toggle.set_active(has_rule)
                        box.set_visible(has_rule)
                
                # Update UI based on saved rules
                for rule in self.forwarding_rules:
                    if not rule.get('enabled', True):
                        continue
                        
                    rule_type = rule.get('type')
                    
                    # Handle local forwarding
                    if rule_type == 'local' and hasattr(self, 'local_forwarding_enabled'):
                        self.local_forwarding_enabled.set_active(True)
                        if hasattr(self, 'local_port_row') and 'listen_port' in rule:
                            self.local_port_row.set_value(rule['listen_port'])
                        if hasattr(self, 'remote_host_row') and 'remote_host' in rule:
                            self.remote_host_row.set_text(rule['remote_host'])
                        if hasattr(self, 'remote_port_row') and 'remote_port' in rule:
                            self.remote_port_row.set_value(rule['remote_port'])
                    
                    # Handle remote forwarding
                    elif rule_type == 'remote' and hasattr(self, 'remote_forwarding_enabled'):
                        self.remote_forwarding_enabled.set_active(True)
                        if hasattr(self, 'remote_bind_port_row') and 'listen_port' in rule:
                            self.remote_bind_port_row.set_value(rule['listen_port'])
                        if hasattr(self, 'local_host_row') and 'remote_host' in rule:
                            self.local_host_row.set_text(rule['remote_host'])
                        if hasattr(self, 'local_bind_port_row') and 'remote_port' in rule:
                            self.local_bind_port_row.set_value(rule['remote_port'])
                    
                    # Handle dynamic forwarding
                    elif rule_type == 'dynamic' and hasattr(self, 'dynamic_forwarding_enabled'):
                        self.dynamic_forwarding_enabled.set_active(True)
                        if hasattr(self, 'dynamic_port_row') and 'listen_port' in rule:
                            self.dynamic_port_row.set_value(rule['listen_port'])
                
                # Load the rules into the UI
                self.load_port_forwarding_rules()
                
        except Exception as e:
            logger.error(f"Error loading connection data: {e}")
            self.show_error(_("Failed to load connection data"))
    
    def setup_ui(self):
        """Set up the dialog UI"""
        # Main container - using Adw.ToolbarView for better layout
        self.main_box = Adw.ToolbarView()
        self.set_content(self.main_box)
        
        # Header Bar
        header_bar = Adw.HeaderBar()
        header_bar.set_show_title(False)
        
        # Create tab view
        self.tab_view = Adw.TabView()
        self.tab_view.set_hexpand(True)
        self.tab_view.set_vexpand(True)
        
        # Create tab bar for the header
        tab_bar = Adw.TabBar()
        tab_bar.set_view(self.tab_view)
        header_bar.set_title_widget(tab_bar)
        
        # Cancel button
        self.cancel_button = Gtk.Button(label=_("Cancel"))
        self.cancel_button.connect("clicked", self.on_cancel_clicked)
        header_bar.pack_start(self.cancel_button)
        
        # Save button
        self.save_button = Gtk.Button(label=_("Save"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self.on_save_clicked)
        header_bar.pack_end(self.save_button)
        
        # Add header bar to the window
        self.main_box.add_top_bar(header_bar)
        
        # Create a scrolled window for the content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        
        # Create tab pages
        self.setup_connection_page()
        self.setup_port_forwarding_page()
        
        # Add the tab view to the scrolled window
        scrolled.set_child(self.tab_view)
        
        # Add the scrolled window to the main box
        self.main_box.set_content(scrolled)
    
    def setup_connection_page(self):
        """Set up the connection settings page"""
        # Create main container
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)
        page.set_hexpand(True)
        page.set_vexpand(True)
        
        # Create a scrolled window for the content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(page)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        
        # Create a viewport for the scrolled window
        viewport = Gtk.Viewport()
        viewport.set_child(scrolled)
        
        # Add page to tab view
        self.connection_page = self.tab_view.append(viewport)
        self.connection_page.set_title(_("Connection"))
        self.connection_page.set_icon(Gio.ThemedIcon.new("network-server-symbolic"))
        self.connection_page.set_tooltip(_("Connection settings"))
        
        # Basic Settings Group
        basic_group = Adw.PreferencesGroup(title=_("Basic Settings"))
        
        # Nickname
        self.nickname_row = Adw.EntryRow(title=_("Nickname"))
        basic_group.add(self.nickname_row)
        
        # Host
        self.host_row = Adw.EntryRow(title=_("Host"))
        basic_group.add(self.host_row)
        
        # Username
        self.username_row = Adw.EntryRow(title=_("Username"))
        basic_group.add(self.username_row)
        
        # Port
        self.port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.port_row.set_title(_("Port"))
        self.port_row.set_value(22)
        basic_group.add(self.port_row)
        
        page.append(basic_group)
        
        # Authentication Group
        auth_group = Adw.PreferencesGroup(title=_("Authentication"))
        
        # Authentication Method
        auth_model = Gtk.StringList()
        auth_model.append(_("Key-based (recommended)"))
        auth_model.append(_("Password"))
        
        self.auth_method_row = Adw.ComboRow()
        self.auth_method_row.set_title(_("Authentication Method"))
        self.auth_method_row.set_model(auth_model)
        self.auth_method_row.connect("notify::selected", self.on_auth_method_changed)
        auth_group.add(self.auth_method_row)
        
        # Keyfile
        self.keyfile_row = Adw.ActionRow(title=_("SSH Key"), subtitle=_("Select key file or leave empty for auto-detection"))
        self.keyfile_btn = Gtk.Button(label=_("Browse"))
        self.keyfile_btn.connect("clicked", lambda *_: self.browse_for_key_file())
        self.keyfile_row.add_suffix(self.keyfile_btn)
        self.keyfile_row.set_activatable(False)
        auth_group.add(self.keyfile_row)
        
        # Key Passphrase
        self.key_passphrase_row = Adw.PasswordEntryRow(title=_("Key Passphrase"))
        self.key_passphrase_row.set_show_apply_button(False)
        auth_group.add(self.key_passphrase_row)
        
        # Password
        self.password_row = Adw.PasswordEntryRow(title=_("Password"))
        self.password_row.set_show_apply_button(False)
        self.password_row.set_visible(False)
        auth_group.add(self.password_row)
        
        page.append(auth_group)
        
        # Advanced Group
        advanced_group = Adw.PreferencesGroup(title=_("Advanced"))
        
        # Port Forwarding
        port_forwarding_expander = Adw.ExpanderRow(title=_("Port Forwarding"))
        port_forwarding_expander.set_subtitle(_("Configure port forwarding rules"))
        port_forwarding_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        port_forwarding_box.set_margin_top(12)
        port_forwarding_box.set_margin_bottom(12)
        port_forwarding_box.set_margin_start(12)
        port_forwarding_box.set_margin_end(12)
        
        # Store port forwarding settings as instance variables
        self.port_forwarding_rules = {}
        
        # Local Port Forwarding
        local_forwarding_group = Adw.PreferencesGroup(title=_("Local Port Forwarding"))
        
        # Enable toggle for local port forwarding
        self.local_forwarding_enabled = Adw.SwitchRow()
        self.local_forwarding_enabled.set_title(_("Local Port Forwarding"))
        self.local_forwarding_enabled.set_subtitle(_("Forward a local port to a remote host"))
        local_forwarding_group.add(self.local_forwarding_enabled)
        
        # Local port forwarding settings
        local_settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        local_settings_box.set_margin_start(12)
        local_settings_box.set_margin_end(12)
        local_settings_box.set_margin_bottom(12)
        
        local_port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        local_port_row.set_title(_("Local Port"))
        local_port_row.set_subtitle(_("Local port to forward"))
        local_settings_box.append(local_port_row)
        
        remote_host_row = Adw.EntryRow()
        remote_host_row.set_title(_("Remote Host"))
        entry = remote_host_row.get_child()
        if entry and hasattr(entry, 'set_placeholder_text'):
            entry.set_placeholder_text("localhost")
        remote_host_row.set_show_apply_button(False)
        local_settings_box.append(remote_host_row)
        
        remote_port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        remote_port_row.set_title(_("Remote Port"))
        remote_port_row.set_subtitle(_("Port on remote host"))
        local_settings_box.append(remote_port_row)
        
        # Add settings box to group
        local_forwarding_group.add(local_settings_box)
        
        # Store references to the rows for saving
        self.local_port_row = local_port_row
        self.remote_host_row = remote_host_row
        self.remote_port_row = remote_port_row
        self.local_settings_box = local_settings_box  # Store reference to the settings box
        
        # Connect toggle to show/hide settings
        self.local_forwarding_enabled.connect('notify::active', self.on_forwarding_toggled, local_settings_box)
        
        # Initially hide settings if not enabled
        local_settings_box.set_visible(False)
        
        port_forwarding_box.append(local_forwarding_group)
        
        # Remote Port Forwarding
        remote_forwarding_group = Adw.PreferencesGroup(title=_("Remote Port Forwarding"))
        
        # Enable toggle for remote port forwarding
        self.remote_forwarding_enabled = Adw.SwitchRow()
        self.remote_forwarding_enabled.set_title(_("Remote Port Forwarding"))
        self.remote_forwarding_enabled.set_subtitle(_("Forward a remote port to a local host"))
        remote_forwarding_group.add(self.remote_forwarding_enabled)
        
        # Remote port forwarding settings
        remote_settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        remote_settings_box.set_margin_start(12)
        remote_settings_box.set_margin_end(12)
        remote_settings_box.set_margin_bottom(12)
        
        remote_bind_port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        remote_bind_port_row.set_title(_("Remote Port"))
        remote_bind_port_row.set_subtitle(_("Port on remote host"))
        remote_settings_box.append(remote_bind_port_row)
        
        local_host_row = Adw.EntryRow()
        local_host_row.set_title(_("Local Host"))
        local_entry = local_host_row.get_child()
        if local_entry and hasattr(local_entry, 'set_placeholder_text'):
            local_entry.set_placeholder_text("localhost")
        local_host_row.set_show_apply_button(False)
        remote_settings_box.append(local_host_row)
        
        local_bind_port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        local_bind_port_row.set_title(_("Local Port"))
        local_bind_port_row.set_subtitle(_("Local port to forward"))
        remote_settings_box.append(local_bind_port_row)
        
        # Add settings box to group
        remote_forwarding_group.add(remote_settings_box)
        
        # Store references to the rows for saving
        self.remote_bind_port_row = remote_bind_port_row
        self.local_host_row = local_host_row
        self.local_bind_port_row = local_bind_port_row
        self.remote_settings_box = remote_settings_box  # Store reference to the settings box
        
        # Connect toggle to show/hide settings
        self.remote_forwarding_enabled.connect('notify::active', self.on_forwarding_toggled, remote_settings_box)
        
        # Initially hide settings if not enabled
        remote_settings_box.set_visible(False)
        
        port_forwarding_box.append(remote_forwarding_group)
        
        # Dynamic Port Forwarding (SOCKS)
        dynamic_forwarding_group = Adw.PreferencesGroup(title=_("Dynamic Port Forwarding (SOCKS)"))
        
        # Enable toggle for dynamic port forwarding
        self.dynamic_forwarding_enabled = Adw.SwitchRow()
        self.dynamic_forwarding_enabled.set_title(_("Dynamic Port Forwarding"))
        self.dynamic_forwarding_enabled.set_subtitle(_("Create a SOCKS proxy on local port"))
        dynamic_forwarding_group.add(self.dynamic_forwarding_enabled)
        
        # Dynamic port forwarding settings
        dynamic_settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        dynamic_settings_box.set_margin_start(12)
        dynamic_settings_box.set_margin_end(12)
        dynamic_settings_box.set_margin_bottom(12)
        
        dynamic_port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        dynamic_port_row.set_title(_("Local Port"))
        dynamic_port_row.set_subtitle(_("Local SOCKS proxy port"))
        dynamic_port_row.set_value(1080)  # Default SOCKS port
        dynamic_settings_box.append(dynamic_port_row)
        
        # Add settings box to group
        dynamic_forwarding_group.add(dynamic_settings_box)
        
        # Store reference for saving
        self.dynamic_port_row = dynamic_port_row
        self.dynamic_settings_box = dynamic_settings_box  # Store reference to the settings box
        
        # Connect toggle to show/hide settings
        self.dynamic_forwarding_enabled.connect('notify::active', self.on_forwarding_toggled, dynamic_settings_box)
        
        # Initially hide settings if not enabled
        dynamic_settings_box.set_visible(False)
        
        port_forwarding_box.append(dynamic_forwarding_group)
        
        # Add the box to the expander
        port_forwarding_expander.add_row(port_forwarding_box)
        
        # Add port forwarding expander to advanced group
        advanced_group.add(port_forwarding_expander)
        
        # X11 Forwarding
        self.x11_row = Adw.SwitchRow(
            title=_("X11 Forwarding"), 
            subtitle=_("Forward X11 connections for GUI applications")
        )
        advanced_group.add(self.x11_row)
        
        page.append(advanced_group)
        
        scrolled.set_child(page)
    
    def setup_port_forwarding_page(self):
        """Set up the port forwarding page"""
        # Create main container
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)
        page.set_hexpand(True)
        page.set_vexpand(True)
        
        # Create a scrolled window for the content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(page)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        
        # Port Forwarding Rules Group
        rules_group = Adw.PreferencesGroup(
            title=_("Port Forwarding Rules"),
            description=_("Add, edit, or remove port forwarding rules for this connection")
        )
        
        # Add button with icon
        add_button = Gtk.Button(
            label=_("Add Rule"),
            halign=Gtk.Align.START,
            margin_top=6,
            margin_bottom=6
        )
        add_button.set_icon_name("list-add-symbolic")
        add_button.connect("clicked", self.on_add_forwarding_rule_clicked)
        rules_group.add(add_button)
        
        # Rules list
        self.rules_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        rules_group.add(self.rules_list)
        
        # Add a placeholder if no rules
        self.placeholder = Gtk.Label(
            label=_("No port forwarding rules configured"),
            margin_top=12,
            margin_bottom=12
        )
        self.placeholder.add_css_class("dim-label")
        self.rules_list.append(self.placeholder)
        
        # About Port Forwarding Group
        about_group = Adw.PreferencesGroup(
            title=_("About Port Forwarding"),
            description=_(
                "Port forwarding allows you to securely tunnel network connections.\n\n"
                "• <b>Local Forwarding</b>: Forward a remote port to your local machine\n"
                "• <b>Remote Forwarding</b>: Forward a local port to the remote machine\n"
                "• <b>Dynamic Forwarding</b>: Create a SOCKS proxy on your local machine"
            )
        )
        
        # Add groups to the page
        page.append(rules_group)
        page.append(about_group)
        
        # Create a viewport for the scrolled window
        viewport = Gtk.Viewport()
        viewport.set_child(scrolled)
        
        # Add page to tab view
        self.port_forwarding_page = self.tab_view.append(viewport)
        self.port_forwarding_page.set_title(_("Port Forwarding"))
        self.port_forwarding_page.set_icon(Gio.ThemedIcon.new("network-transmit-receive-symbolic"))
        self.port_forwarding_page.set_tooltip(_("Port forwarding settings"))
        
        # Initialize empty rules list if it doesn't exist
        if not hasattr(self, 'forwarding_rules'):
            self.forwarding_rules = []
        
        # Load any existing rules if editing
        if self.is_editing and self.connection and hasattr(self.connection, 'forwarding_rules'):
            self.load_port_forwarding_rules()
    
    def load_port_forwarding_rules(self):
        """Load port forwarding rules from the connection and update UI"""
        if not hasattr(self, 'rules_list') or not hasattr(self, 'forwarding_rules'):
            return
            
        # Clear existing rules UI
        while self.rules_list.get_first_child():
            self.rules_list.remove(self.rules_list.get_first_child())
        
        # Show placeholder if no rules
        if not self.forwarding_rules:
            self.rules_list.append(self.placeholder)
            return
        
        # Hide placeholder since we have rules
        if self.placeholder.get_parent():
            self.placeholder.unparent()
        
        # Process each forwarding rule
        for rule in self.forwarding_rules:
            if not rule.get('enabled', True):
                continue
                
            rule_type = rule.get('type', '')
            
            # Create a row for the rule
            row = Adw.ActionRow()
            row.set_selectable(False)
            
            # Set appropriate icon and title based on rule type
            if rule_type == 'local':
                row.set_title(_("Local Port Forwarding"))
                row.add_prefix(Gtk.Image.new_from_icon_name("network-transmit-receive-symbolic"))
                description = _("Local port {local_port} → {remote_host}:{remote_port}").format(
                    local_port=rule.get('listen_port', ''),
                    remote_host=rule.get('remote_host', ''),
                    remote_port=rule.get('remote_port', '')
                )
            elif rule_type == 'remote':
                row.set_title(_("Remote Port Forwarding"))
                row.add_prefix(Gtk.Image.new_from_icon_name("network-receive-symbolic"))
                description = _("Remote port {remote_port} → {local_host}:{local_port}").format(
                    remote_port=rule.get('listen_port', ''),
                    local_host=rule.get('remote_host', ''),
                    local_port=rule.get('remote_port', '')
                )
            elif rule_type == 'dynamic':
                row.set_title(_("Dynamic Port Forwarding (SOCKS)"))
                row.add_prefix(Gtk.Image.new_from_icon_name("network-workgroup-symbolic"))
                description = _("SOCKS proxy on port {port}").format(
                    port=rule.get('listen_port', '')
                )
            else:
                continue
                
            # Add description
            row.set_subtitle(description)
            
            # Add delete button
            delete_button = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                css_classes=["flat", "error"]
            )
            delete_button.connect("clicked", self.on_delete_forwarding_rule_clicked, rule)
            row.add_suffix(delete_button)
            
            # Add edit button
            edit_button = Gtk.Button(
                icon_name="document-edit-symbolic",
                valign=Gtk.Align.CENTER,
                css_classes=["flat"]
            )
            edit_button.connect("clicked", self.on_edit_forwarding_rule_clicked, rule)
            row.add_suffix(edit_button)
            
            # Add the row to the list
            self.rules_list.append(row)
        
        # Show the rules list
        self.rules_list.show()
    
    def browse_for_key_file(self):
        """Open file chooser to browse for SSH key file"""
        dialog = Gtk.FileChooserNative(
            title=_("Select SSH Key File"),
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN
        )
        
        # Set filters
        filter_ssh = Gtk.FileFilter()
        filter_ssh.set_name(_("SSH Private Keys"))
        filter_ssh.add_pattern("id_rsa")
        filter_ssh.add_pattern("id_dsa")
        filter_ssh.add_pattern("id_ecdsa")
        filter_ssh.add_pattern("id_ed25519")
        filter_ssh.add_pattern("*.pem")
        filter_ssh.add_pattern("*.key")
        dialog.add_filter(filter_ssh)
        
        filter_any = Gtk.FileFilter()
        filter_any.set_name(_("All Files"))
        filter_any.add_pattern("*")
        dialog.add_filter(filter_any)
        
        dialog.connect("response", self.on_key_file_selected)
        dialog.show()
    
    def on_key_file_selected(self, dialog, response):
        """Handle selected key file from file chooser"""
        if response == Gtk.ResponseType.ACCEPT:
            key_file = dialog.get_file()
            if key_file:
                self.keyfile_row.set_subtitle(key_file.get_path())
        dialog.destroy()
    
    def on_delete_forwarding_rule_clicked(self, button, rule):
        """Handle delete port forwarding rule button click"""
        if not hasattr(self, 'forwarding_rules'):
            return
            
        # Remove the rule from the list
        self.forwarding_rules = [r for r in self.forwarding_rules if r != rule]
        
        # Reload the rules UI
        self.load_port_forwarding_rules()
        
        logger.info(f"Deleted port forwarding rule: {rule}")
    
    def on_edit_forwarding_rule_clicked(self, button, rule):
        """Handle edit port forwarding rule button click"""
        # Implementation for editing an existing port forwarding rule
        logger.info(f"Edit port forwarding rule clicked: {rule}")
        # This would open a dialog to edit the existing rule
        
        # For now, we'll just remove the old rule and add a new one
        # This is a placeholder - in a real implementation, you'd want to open an edit dialog
        if hasattr(self, 'forwarding_rules'):
            self.forwarding_rules = [r for r in self.forwarding_rules if r != rule]
            self.load_port_forwarding_rules()
    
    def on_add_forwarding_rule_clicked(self, button):
        """Handle add port forwarding rule button click"""
        # Implementation for adding a new port forwarding rule
        logger.info("Add port forwarding rule clicked")
        # This would open a dialog to configure the new rule
        
        # For now, we'll just add a sample rule
        if not hasattr(self, 'forwarding_rules'):
            self.forwarding_rules = []
            
        new_rule = {
            'type': 'local',
            'enabled': True,
            'listen_addr': 'localhost',
            'listen_port': 8080,
            'remote_host': 'localhost',
            'remote_port': 80
        }
        
        self.forwarding_rules.append(new_rule)
        self.load_port_forwarding_rules()
    
    def on_cancel_clicked(self, button):
        """Handle cancel button click"""
        self.destroy()
    
    def on_save_clicked(self, button):
        """Handle save button click"""
        # Validate required fields
        if not self.nickname_row.get_text().strip():
            self.show_error(_("Please enter a nickname for this connection"))
            return
            
        if not self.host_row.get_text().strip():
            self.show_error(_("Please enter a hostname or IP address"))
            return
        
        # Initialize forwarding_rules if it doesn't exist
        if not hasattr(self, 'forwarding_rules'):
            self.forwarding_rules = []
        
        # Start with existing disabled rules
        forwarding_rules = [r for r in getattr(self, 'forwarding_rules', []) if not r.get('enabled', True)]
        
        # Local port forwarding
        if hasattr(self, 'local_forwarding_enabled') and self.local_forwarding_enabled.get_active():
            local_rule = {
                'type': 'local',
                'enabled': True,
                'listen_addr': 'localhost',
                'listen_port': int(self.local_port_row.get_value() if hasattr(self, 'local_port_row') else 0),
                'remote_host': (self.remote_host_row.get_text().strip() 
                              if hasattr(self, 'remote_host_row') and self.remote_host_row.get_text().strip() 
                              else 'localhost'),
                'remote_port': int(self.remote_port_row.get_value() if hasattr(self, 'remote_port_row') else 0)
            }
            forwarding_rules.append(local_rule)
            logger.info(f"Saving local port forwarding rule: {local_rule}")
        
        # Remote port forwarding
        if hasattr(self, 'remote_forwarding_enabled') and self.remote_forwarding_enabled.get_active():
            remote_rule = {
                'type': 'remote',
                'enabled': True,
                'listen_addr': '0.0.0.0',
                'listen_port': int(self.remote_bind_port_row.get_value() 
                                 if hasattr(self, 'remote_bind_port_row') else 0),
                'remote_host': (self.local_host_row.get_text().strip() 
                              if hasattr(self, 'local_host_row') and self.local_host_row.get_text().strip() 
                              else 'localhost'),
                'remote_port': int(self.local_bind_port_row.get_value() 
                                 if hasattr(self, 'local_bind_port_row') else 0)
            }
            forwarding_rules.append(remote_rule)
            logger.info(f"Saving remote port forwarding rule: {remote_rule}")
        
        # Dynamic port forwarding
        if hasattr(self, 'dynamic_forwarding_enabled') and self.dynamic_forwarding_enabled.get_active():
            dynamic_rule = {
                'type': 'dynamic',
                'enabled': True,
                'listen_addr': 'localhost',
                'listen_port': int(self.dynamic_port_row.get_value() 
                                 if hasattr(self, 'dynamic_port_row') else 0)
            }
            forwarding_rules.append(dynamic_rule)
            logger.info(f"Saving dynamic port forwarding rule: {dynamic_rule}")
            
        # Keep any existing disabled rules
        for rule in getattr(self, 'forwarding_rules', []):
            if not rule.get('enabled', True):
                forwarding_rules.append(rule)
        
        # Gather connection data
        connection_data = {
            'nickname': self.nickname_row.get_text().strip(),
            'host': self.host_row.get_text().strip(),
            'username': self.username_row.get_text().strip(),
            'port': int(self.port_row.get_value()),
            'auth_method': self.auth_method_row.get_selected(),
            'keyfile': self.keyfile_row.get_subtitle() if hasattr(self.keyfile_row, 'get_subtitle') else "",
            'key_passphrase': self.key_passphrase_row.get_text(),
            'password': self.password_row.get_text(),
            'x11_forwarding': self.x11_row.get_active(),
            'forwarding_rules': forwarding_rules
        }
        
        # Update the connection object with new data if editing
        if self.is_editing and self.connection:
            self.connection.data.update(connection_data)
            # Explicitly update forwarding rules to ensure they're fresh
            self.connection.forwarding_rules = forwarding_rules
            
        # Emit signal with connection data
        self.emit('connection-saved', connection_data)
        self.destroy()
    
    def on_forwarding_toggled(self, switch, param, settings_box):
        """Handle toggling of port forwarding settings visibility and state"""
        is_active = switch.get_active()
        settings_box.set_visible(is_active)
        
        # Initialize forwarding_rules if it doesn't exist
        if not hasattr(self, 'forwarding_rules'):
            self.forwarding_rules = []
            
        # Determine the rule type based on the switch
        rule_type = None
        if switch == self.local_forwarding_enabled:
            rule_type = 'local'
        elif switch == self.remote_forwarding_enabled:
            rule_type = 'remote'
        elif switch == self.dynamic_forwarding_enabled:
            rule_type = 'dynamic'
            
        if rule_type:
            # Only update the rule if it doesn't exist or if we're disabling it
            existing_rule = next((r for r in self.forwarding_rules if r.get('type') == rule_type), None)
            
            if not is_active:
                # If disabling, just remove the rule
                self.forwarding_rules = [r for r in self.forwarding_rules if r.get('type') != rule_type]
            elif not existing_rule or not existing_rule.get('enabled', True):
                # If enabling and no existing rule or rule is disabled, add a new one
                rule = {'type': rule_type, 'enabled': True}
                
                # Set default values based on rule type
                if rule_type == 'local' and hasattr(self, 'local_port_row') and hasattr(self, 'remote_host_row') and hasattr(self, 'remote_port_row'):
                    rule.update({
                        'listen_addr': 'localhost',
                        'listen_port': int(self.local_port_row.get_value() or 0),
                        'remote_host': self.remote_host_row.get_text().strip() or 'localhost',
                        'remote_port': int(self.remote_port_row.get_value() or 0)
                    })
                elif rule_type == 'remote' and hasattr(self, 'remote_bind_port_row') and hasattr(self, 'local_host_row') and hasattr(self, 'local_bind_port_row'):
                    rule.update({
                        'listen_addr': '0.0.0.0',
                        'listen_port': int(self.remote_bind_port_row.get_value() or 0),
                        'remote_host': self.local_host_row.get_text().strip() or 'localhost',
                        'remote_port': int(self.local_bind_port_row.get_value() or 0)
                    })
                elif rule_type == 'dynamic' and hasattr(self, 'dynamic_port_row'):
                    rule.update({
                        'listen_addr': 'localhost',
                        'listen_port': int(self.dynamic_port_row.get_value())
                    })
                
                self.forwarding_rules.append(rule)
                logger.debug(f"Updated {rule_type} forwarding rule: {rule}")
            
            # Update the rules list in the UI
            self.load_port_forwarding_rules()
    
    def show_error(self, message):
        """Show error message"""
        dialog = Adw.MessageDialog.new(
            self,
            _("Error"),
            message
        )
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present()
