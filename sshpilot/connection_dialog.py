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

class ConnectionDialog(Adw.PreferencesDialog):
    """Dialog for adding/editing SSH connections using PreferencesDialog layout"""
    
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
        # PreferencesDialog is modal by nature; just set transient parent
        try:
            self.set_transient_for(parent)
        except Exception:
            pass
        # PreferencesDialog doesn't support set_default_size; rely on content sizing
        
        self.setup_ui()
        try:
            self.add_response("cancel", _("Cancel"))
            self.add_response("save", _("Save"))
            # Mark save as suggested if available
            try:
                from gi.repository import Adw as _Adw
                if hasattr(self, 'set_response_appearance'):
                    self.set_response_appearance("save", _Adw.ResponseAppearance.SUGGESTED)
            except Exception:
                pass
            self.set_close_response("cancel")
            self.connect("response", self.on_response)
            self._has_dialog_responses = True
        except Exception:
            # Fallback path when responses API is unavailable
            self._has_dialog_responses = False
        GLib.idle_add(self.load_connection_data)
        
        # Add ESC key to cancel/close the dialog
        try:
            key_ctrl = Gtk.EventControllerKey()
            if hasattr(key_ctrl, 'set_propagation_phase'):
                key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

            def _on_key_pressed(ctrl, keyval, keycode, state):
                if keyval == Gdk.KEY_Escape:
                    self.on_cancel_clicked(None)
                    return True
                return False

            key_ctrl.connect('key-pressed', _on_key_pressed)
            self.add_controller(key_ctrl)
        except Exception:
            pass
    
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
            # Ensure UI controls exist
            required_attrs = [
                'nickname_row', 'host_row', 'username_row', 'port_row',
                'auth_method_row', 'keyfile_row', 'password_row', 'key_passphrase_row'
            ]
            for attr in required_attrs:
                if not hasattr(self, attr):
                    return
            # Load basic connection data
            if hasattr(self.connection, 'nickname'):
                self.nickname_row.set_text(self.connection.nickname or "")
            if hasattr(self.connection, 'host'):
                self.host_row.set_text(self.connection.host or "")
            if hasattr(self.connection, 'username'):
                self.username_row.set_text(self.connection.username or "")
            if hasattr(self.connection, 'port'):
                try:
                    self.port_row.set_text(str(int(self.connection.port) if self.connection.port else 22))
                except Exception:
                    self.port_row.set_text("22")
            
            # Set authentication method and related fields
            auth_method = getattr(self.connection, 'auth_method', 0)
            self.auth_method_row.set_selected(auth_method)
            self.on_auth_method_changed(self.auth_method_row, None)  # Update UI state
            
            # Get keyfile path from either keyfile or private_key attribute
            keyfile = getattr(self.connection, 'keyfile', None) or getattr(self.connection, 'private_key', None)
            if keyfile:
                # Normalize the keyfile path and ensure it's a string
                keyfile_path = str(keyfile).strip()
                
                # Update the connection's keyfile attribute if it comes from private_key
                if not getattr(self.connection, 'keyfile', None) and hasattr(self.connection, 'private_key'):
                    self.connection.keyfile = keyfile_path
                
                # Only update the UI if we have a valid path
                if keyfile_path and keyfile_path.lower() not in ['select key file or leave empty for auto-detection', '']:
                    logger.debug(f"Setting keyfile path in UI: {keyfile_path}")
                    self.keyfile_row.set_subtitle(keyfile_path)
                else:
                    logger.debug(f"Skipping invalid keyfile path: {keyfile_path}")
            
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
                            try:
                                self.local_port_row.set_text(str(int(rule['listen_port'])))
                            except Exception:
                                self.local_port_row.set_text(str(rule['listen_port']))
                        if hasattr(self, 'remote_host_row') and 'remote_host' in rule:
                            self.remote_host_row.set_text(rule['remote_host'])
                        if hasattr(self, 'remote_port_row') and 'remote_port' in rule:
                            try:
                                self.remote_port_row.set_text(str(int(rule['remote_port'])))
                            except Exception:
                                self.remote_port_row.set_text(str(rule['remote_port']))
                    
                    # Handle remote forwarding
                    elif rule_type == 'remote' and hasattr(self, 'remote_forwarding_enabled'):
                        self.remote_forwarding_enabled.set_active(True)
                        if hasattr(self, 'remote_bind_host_row') and 'listen_addr' in rule:
                            try:
                                self.remote_bind_host_row.set_text(str(rule.get('listen_addr') or 'localhost'))
                            except Exception:
                                pass
                        if hasattr(self, 'remote_bind_port_row') and 'listen_port' in rule:
                            try:
                                self.remote_bind_port_row.set_text(str(int(rule['listen_port'])))
                            except Exception:
                                self.remote_bind_port_row.set_text(str(rule['listen_port']))
                        # Destination (local) host/port
                        if hasattr(self, 'dest_host_row'):
                            try:
                                self.dest_host_row.set_text(
                                    str(rule.get('local_host') or rule.get('remote_host', 'localhost'))
                                )
                            except Exception:
                                pass
                        if hasattr(self, 'dest_port_row'):
                            try:
                                self.dest_port_row.set_text(
                                    str(int(rule.get('local_port') or rule.get('remote_port') or 0))
                                )
                            except Exception:
                                self.dest_port_row.set_text(str(rule.get('local_port') or rule.get('remote_port') or ''))
                    
                    # Handle dynamic forwarding
                    elif rule_type == 'dynamic' and hasattr(self, 'dynamic_forwarding_enabled'):
                        self.dynamic_forwarding_enabled.set_active(True)
                        if hasattr(self, 'dynamic_port_row') and 'listen_port' in rule:
                            try:
                                self.dynamic_port_row.set_text(str(int(rule['listen_port'])))
                            except Exception:
                                self.dynamic_port_row.set_text(str(rule['listen_port']))
                
                # Load the rules into the UI
                self.load_port_forwarding_rules()
                
        except Exception as e:
            logger.error(f"Error loading connection data: {e}")
            self.show_error(_("Failed to load connection data"))
    
    def setup_ui(self):
        """Set up the dialog UI"""
        # Build pages using PreferencesDialog model
        # If responses API is unavailable, we will add a single footer later as fallback

        general_page = Adw.PreferencesPage()
        general_page.set_title(_("Connection"))
        general_page.set_icon_name("network-server-symbolic")
        for group in self.build_connection_groups():
            general_page.add(group)
        self.add(general_page)

        forwarding_page = Adw.PreferencesPage()
        forwarding_page.set_title(_("Advanced"))
        forwarding_page.set_icon_name("network-transmit-receive-symbolic")
        for group in self.build_port_forwarding_groups():
            forwarding_page.add(group)
        self.add(forwarding_page)

        # Add a persistent action bar at the bottom of each page
        try:
            action_group_general = self._build_action_bar_group()
            general_page.add(action_group_general)
            action_group_forward = self._build_action_bar_group()
            forwarding_page.add(action_group_forward)
        except Exception as e:
            logger.debug(f"Failed to add action bars: {e}")
        # After building views, populate existing data if editing
        try:
            self.load_connection_data()
        except Exception as e:
            logger.error(f"Failed to populate connection data: {e}")
    
    def build_connection_groups(self):
        """Build PreferencesGroups for the General page"""
        # Create main container
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)
        page.set_hexpand(True)
        page.set_vexpand(True)
        
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
        
        # Port (match style of fields above using EntryRow)
        self.port_row = Adw.EntryRow(title=_("Port"))
        try:
            entry = self.port_row.get_child()
            if entry and hasattr(entry, 'set_input_purpose'):
                entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if entry and hasattr(entry, 'set_max_length'):
                entry.set_max_length(5)
        except Exception:
            pass
        self.port_row.set_text("22")
        basic_group.add(self.port_row)
        
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
        # Compact, icon-only browse button
        try:
            self.keyfile_btn = Gtk.Button.new_from_icon_name('folder-open-symbolic')
        except Exception:
            self.keyfile_btn = Gtk.Button.new_from_icon_name('document-open-symbolic')
        try:
            self.keyfile_btn.add_css_class('flat')
            self.keyfile_btn.set_valign(Gtk.Align.CENTER)
            self.keyfile_btn.set_halign(Gtk.Align.END)
            self.keyfile_btn.set_tooltip_text(_("Browse"))
        except Exception:
            pass
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
        
        # Remove unused advanced label group from this page
        advanced_group = Adw.PreferencesGroup()
        advanced_group.set_visible(False)
        
        # Local Port Forwarding (moved to Port Forwarding view)
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
        
        local_port_row = Adw.EntryRow()
        local_port_row.set_title(_("Local Port"))
        try:
            local_port_row.set_subtitle(_("Local port to forward"))
        except Exception:
            pass
        try:
            lpe = local_port_row.get_child()
            if lpe and hasattr(lpe, 'set_input_purpose'):
                lpe.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if lpe and hasattr(lpe, 'set_max_length'):
                lpe.set_max_length(5)
        except Exception:
            pass
        local_settings_box.append(local_port_row)
        
        remote_host_row = Adw.EntryRow()
        remote_host_row.set_title(_("Target Host"))
        entry = remote_host_row.get_child()
        if entry and hasattr(entry, 'set_placeholder_text'):
            entry.set_placeholder_text("localhost")
        remote_host_row.set_show_apply_button(False)
        local_settings_box.append(remote_host_row)
        
        remote_port_row = Adw.EntryRow()
        remote_port_row.set_title(_("Target Port"))
        try:
            remote_port_row.set_subtitle(_("Port on remote host"))
        except Exception:
            pass
        try:
            rpe = remote_port_row.get_child()
            if rpe and hasattr(rpe, 'set_input_purpose'):
                rpe.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if rpe and hasattr(rpe, 'set_max_length'):
                rpe.set_max_length(5)
        except Exception:
            pass
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
        
        # group kept for structure but hidden in this view
        local_forwarding_group.set_visible(False)
        
        # Remote Port Forwarding (moved)
        remote_forwarding_group = Adw.PreferencesGroup(title=_("Remote Port Forwarding"))
        
        # Enable toggle for remote port forwarding
        self.remote_forwarding_enabled = Adw.SwitchRow()
        self.remote_forwarding_enabled.set_title(_("Remote Port Forwarding"))
        self.remote_forwarding_enabled.set_subtitle(_("Forward a remote port to a local host"))
        remote_forwarding_group.add(self.remote_forwarding_enabled)
        
        # Remote port forwarding settings (RemoteHost, RemotePort -> DestinationHost, DestinationPort)
        remote_settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        remote_settings_box.set_margin_start(12)
        remote_settings_box.set_margin_end(12)
        remote_settings_box.set_margin_bottom(12)
        
        remote_bind_host_row = Adw.EntryRow()
        remote_bind_host_row.set_title(_("Remote host (optional)"))
        rbh_entry = remote_bind_host_row.get_child()
        if rbh_entry and hasattr(rbh_entry, 'set_placeholder_text'):
            rbh_entry.set_placeholder_text("localhost")
        remote_bind_host_row.set_show_apply_button(False)
        remote_bind_host_row.set_text("localhost")
        remote_settings_box.append(remote_bind_host_row)
        
        remote_bind_port_row = Adw.EntryRow()
        remote_bind_port_row.set_title(_("Remote port"))
        try:
            rbpe = remote_bind_port_row.get_child()
            if rbpe and hasattr(rbpe, 'set_input_purpose'):
                rbpe.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if rbpe and hasattr(rbpe, 'set_max_length'):
                rbpe.set_max_length(5)
        except Exception:
            pass
        remote_settings_box.append(remote_bind_port_row)
        
        dest_host_row = Adw.EntryRow()
        dest_host_row.set_title(_("Destination host"))
        dest_entry = dest_host_row.get_child()
        if dest_entry and hasattr(dest_entry, 'set_placeholder_text'):
            dest_entry.set_placeholder_text("localhost")
        dest_host_row.set_show_apply_button(False)
        dest_host_row.set_text("localhost")
        remote_settings_box.append(dest_host_row)

        dest_port_row = Adw.EntryRow()
        dest_port_row.set_title(_("Destination port"))
        try:
            # Align subtitle to previous implementation wording
            dest_port_row.set_subtitle(_("Local port to forward"))
        except Exception:
            pass
        try:
            dpe = dest_port_row.get_child()
            if dpe and hasattr(dpe, 'set_input_purpose'):
                dpe.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if dpe and hasattr(dpe, 'set_max_length'):
                dpe.set_max_length(5)
        except Exception:
            pass
        remote_settings_box.append(dest_port_row)
        
        # Add settings box to group
        remote_forwarding_group.add(remote_settings_box)
        
        # Store references to the rows for saving
        self.remote_bind_host_row = remote_bind_host_row
        self.remote_bind_port_row = remote_bind_port_row
        self.dest_host_row = dest_host_row
        self.dest_port_row = dest_port_row
        self.remote_settings_box = remote_settings_box  # Store reference to the settings box
        
        # Connect toggle to show/hide settings
        self.remote_forwarding_enabled.connect('notify::active', self.on_forwarding_toggled, remote_settings_box)
        
        # Initially hide settings if not enabled
        remote_settings_box.set_visible(False)
        
        remote_forwarding_group.set_visible(False)
        
        # Dynamic Port Forwarding (moved)
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

        dynamic_bind_row = Adw.EntryRow()
        dynamic_bind_row.set_title(_("Bind address (optional)"))
        try:
            dbe = dynamic_bind_row.get_child()
            if dbe and hasattr(dbe, 'set_placeholder_text'):
                dbe.set_placeholder_text("127.0.0.1")
        except Exception:
            pass
        dynamic_settings_box.append(dynamic_bind_row)

        dynamic_port_row = Adw.EntryRow()
        dynamic_port_row.set_title(_("Local Port"))
        try:
            dpe2 = dynamic_port_row.get_child()
            if dpe2 and hasattr(dpe2, 'set_input_purpose'):
                dpe2.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if dpe2 and hasattr(dpe2, 'set_max_length'):
                dpe2.set_max_length(5)
        except Exception:
            pass
        dynamic_port_row.set_text("1080")  # Default SOCKS port
        dynamic_settings_box.append(dynamic_port_row)
        
        # Add settings box to group
        dynamic_forwarding_group.add(dynamic_settings_box)
        
        # Store reference for saving
        self.dynamic_bind_row = dynamic_bind_row
        self.dynamic_port_row = dynamic_port_row
        self.dynamic_settings_box = dynamic_settings_box  # Store reference to the settings box
        
        # Connect toggle to show/hide settings
        self.dynamic_forwarding_enabled.connect('notify::active', self.on_forwarding_toggled, dynamic_settings_box)
        
        # Initially hide settings if not enabled
        dynamic_settings_box.set_visible(False)
        
        dynamic_forwarding_group.set_visible(False)
        
        # X11 Forwarding moved to Port Forwarding view
        
        # Return groups for PreferencesPage
        return [basic_group, auth_group, advanced_group]
    
    def build_port_forwarding_groups(self):
        """Build PreferencesGroups for the Advanced page (Port Forwarding first, X11 last)"""
        # Create main container
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)
        page.set_hexpand(True)
        page.set_vexpand(True)
        
        # X11 Forwarding group (will be placed last)
        self.x11_row = Adw.SwitchRow(
            title=_("X11 Forwarding"), 
            subtitle=_("Forward X11 connections for GUI applications")
        )
        x11_group = Adw.PreferencesGroup(title=_("X11 Forwarding"))
        x11_group.add(self.x11_row)
        
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
        
        # Return groups for PreferencesPage: Port forwarding first, X11 last
        return [rules_group, about_group, x11_group]
        
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
                description = _("Remote {remote_host}:{remote_port} → {dest_host}:{dest_port}").format(
                    remote_host=rule.get('listen_addr', 'localhost'),
                    remote_port=rule.get('listen_port', ''),
                    dest_host=rule.get('local_host') or rule.get('remote_host', ''),
                    dest_port=rule.get('local_port') or rule.get('remote_port', '')
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
        """Open file chooser to browse for SSH key file.
        Prefer Adw.FileDialog when available, otherwise use Gtk.FileChooserDialog.
        """
        # Prefer Adw.FileDialog if available in this libadwaita build
        try:
            if hasattr(Adw, 'FileDialog'):
                file_dialog = Adw.FileDialog(title=_("Select SSH Key File"))
                # Default folder to ~/.ssh
                try:
                    ssh_dir = os.path.expanduser('~/.ssh')
                    if os.path.isdir(ssh_dir):
                        file_dialog.set_initial_folder(Gio.File.new_for_path(ssh_dir))
                except Exception:
                    pass

                def _on_chosen(dlg, result):
                    try:
                        file = dlg.open_finish(result)
                        if file:
                            self.keyfile_row.set_subtitle(file.get_path())
                    except Exception:
                        pass
                parent_obj = self.get_transient_for() if hasattr(self, 'get_transient_for') else None
                file_dialog.open(parent_obj, None, _on_chosen)
                return
        except Exception:
            pass

    def _build_action_bar_group(self):
        """Build a bottom-aligned action bar with Cancel/Save."""
        actions_group = Adw.PreferencesGroup()
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        actions_box.set_halign(Gtk.Align.END)
        try:
            cancel_btn = Gtk.Button(label=_("Cancel"))
            save_btn = Gtk.Button(label=_("Save"))
            cancel_btn.add_css_class('flat')
            save_btn.add_css_class('suggested-action')
        except Exception:
            cancel_btn = Gtk.Button(label="Cancel")
            save_btn = Gtk.Button(label="Save")
        cancel_btn.connect('clicked', self.on_cancel_clicked)
        save_btn.connect('clicked', self.on_save_clicked)
        actions_box.append(cancel_btn)
        actions_box.append(save_btn)
        actions_group.add(actions_box)
        return actions_group

        # Fallback to Gtk.FileChooserDialog
        dialog = Gtk.FileChooserDialog(
            title=_("Select SSH Key File"),
            action=Gtk.FileChooserAction.OPEN,
        )
        # Parent must be a Gtk.Window; PreferencesDialog is not one.
        try:
            parent_win = self.get_transient_for()
            if isinstance(parent_win, Gtk.Window):
                dialog.set_transient_for(parent_win)
        except Exception:
            pass
        dialog.set_modal(True)
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("Open"), Gtk.ResponseType.ACCEPT)
        # Default to ~/.ssh directory when available
        try:
            ssh_dir = os.path.expanduser('~/.ssh')
            if os.path.isdir(ssh_dir):
                try:
                    dialog.set_current_folder(Gio.File.new_for_path(ssh_dir))
                except Exception:
                    try:
                        dialog.set_current_folder(ssh_dir)
                    except Exception:
                        try:
                            dialog.set_current_folder_uri(Gio.File.new_for_path(ssh_dir).get_uri())
                        except Exception:
                            pass
        except Exception:
            pass
        
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
        logger.info(f"Edit port forwarding rule clicked: {rule}")
        self._open_rule_editor(existing_rule=rule)
    
    def on_add_forwarding_rule_clicked(self, button):
        """Handle add port forwarding rule button click"""
        logger.info("Add port forwarding rule clicked")
        self._open_rule_editor(existing_rule=None)

    def _open_rule_editor(self, existing_rule=None):
        """Open a small Gtk.Dialog to add/edit a forwarding rule (compatible across lib versions)."""
        # Create dialog
        parent_win = self.get_transient_for() if hasattr(self, 'get_transient_for') else None
        dialog = Gtk.Dialog(title=_("Port Forwarding Rule"), transient_for=parent_win, modal=True)
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("Save"), Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        content.append(box)

        # Type selector
        type_model = Gtk.StringList()
        type_model.append(_("Local"))
        type_model.append(_("Remote"))
        type_model.append(_("Dynamic"))
        type_row = Adw.ComboRow()
        type_row.set_title(_("Type"))
        type_row.set_model(type_model)

        listen_addr_row = Adw.EntryRow(title=_("Bind address (optional)"))
        listen_port_row = Adw.EntryRow()
        listen_port_row.set_title(_("Local port"))
        try:
            lpe2 = listen_port_row.get_child()
            if lpe2 and hasattr(lpe2, 'set_input_purpose'):
                lpe2.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if lpe2 and hasattr(lpe2, 'set_max_length'):
                lpe2.set_max_length(5)
        except Exception:
            pass

        remote_host_row = Adw.EntryRow(title=_("Host"))
        remote_port_row = Adw.EntryRow()
        remote_port_row.set_title(_("Port"))
        try:
            rpe2 = remote_port_row.get_child()
            if rpe2 and hasattr(rpe2, 'set_input_purpose'):
                rpe2.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if rpe2 and hasattr(rpe2, 'set_max_length'):
                rpe2.set_max_length(5)
        except Exception:
            pass

        # Pack rows
        group = Adw.PreferencesGroup()
        group.add(type_row)
        group.add(listen_addr_row)
        group.add(listen_port_row)
        group.add(remote_host_row)
        group.add(remote_port_row)
        box.append(group)

        # Populate when editing
        if existing_rule:
            t = existing_rule.get('type', 'local')
            type_row.set_selected({'local':0,'remote':1,'dynamic':2}.get(t,0))
            listen_addr_row.set_text(str(existing_rule.get('listen_addr', 'localhost')))
            try:
                listen_port_row.set_text(str(int(existing_rule.get('listen_port', 0) or 0)))
            except Exception:
                listen_port_row.set_text(str(existing_rule.get('listen_port', '')))
            if t == 'remote':
                # For remote rules, the destination is local_host/local_port
                remote_host_row.set_text(str(existing_rule.get('local_host', 'localhost')))
                try:
                    remote_port_row.set_text(str(int(existing_rule.get('local_port', 0) or 0)))
                except Exception:
                    remote_port_row.set_text(str(existing_rule.get('local_port', '')))
            else:
                remote_host_row.set_text(str(existing_rule.get('remote_host', 'localhost')))
                try:
                    remote_port_row.set_text(str(int(existing_rule.get('remote_port', 0) or 0)))
                except Exception:
                    remote_port_row.set_text(str(existing_rule.get('remote_port', '')))
        else:
            type_row.set_selected(0)
            # Sane defaults for a new rule
            listen_addr_row.set_text('127.0.0.1')
            listen_port_row.set_text('8080')
            remote_host_row.set_text('localhost')
            remote_port_row.set_text('22')

        # Avoid shadowing translation function '_' by using a local alias
        t = _
        def _sync_visibility(*args):
            idx = type_row.get_selected()
            # Apply label set per type
            if idx == 0:
                # Local
                listen_addr_row.set_visible(False)
                listen_port_row.set_title(t("Local Port"))
                remote_host_row.set_visible(True)
                remote_host_row.set_title(t("Target Host"))
                remote_port_row.set_visible(True)
                remote_port_row.set_title(t("Target Port"))
            elif idx == 1:
                # Remote
                listen_addr_row.set_visible(True)
                listen_addr_row.set_title(t("Remote host (optional)"))
                listen_port_row.set_title(t("Remote port"))
                remote_host_row.set_visible(True)
                remote_host_row.set_title(t("Destination host"))
                remote_port_row.set_visible(True)
                remote_port_row.set_title(t("Destination port"))
            else:
                # Dynamic
                listen_addr_row.set_visible(True)
                listen_addr_row.set_title(t("Bind address (optional)"))
                listen_port_row.set_title(t("Local port"))
                remote_host_row.set_visible(False)
                remote_port_row.set_visible(False)
            # Apply smart defaults when switching types and fields are empty
            try:
                if idx == 0:  # Local
                    if not listen_addr_row.get_text().strip():
                        listen_addr_row.set_text('127.0.0.1')
                    try:
                        if int((listen_port_row.get_text() or '0').strip() or '0') == 0:
                            listen_port_row.set_text('8080')
                    except Exception:
                        listen_port_row.set_text('8080')
                    if not remote_host_row.get_text().strip():
                        remote_host_row.set_text('localhost')
                    try:
                        if int((remote_port_row.get_text() or '0').strip() or '0') == 0:
                            remote_port_row.set_text('22')
                    except Exception:
                        remote_port_row.set_text('22')
                elif idx == 1:  # Remote
                    if not listen_addr_row.get_text().strip():
                        listen_addr_row.set_text('localhost')
                    try:
                        if int((listen_port_row.get_text() or '0').strip() or '0') == 0:
                            listen_port_row.set_text('8080')
                    except Exception:
                        listen_port_row.set_text('8080')
                    if not remote_host_row.get_text().strip():
                        remote_host_row.set_text('localhost')
                    try:
                        if int((remote_port_row.get_text() or '0').strip() or '0') == 0:
                            remote_port_row.set_text('22')
                    except Exception:
                        remote_port_row.set_text('22')
                else:  # Dynamic
                    if not listen_addr_row.get_text().strip():
                        listen_addr_row.set_text('127.0.0.1')
                    try:
                        if int((listen_port_row.get_text() or '0').strip() or '0') == 0:
                            listen_port_row.set_text('1080')
                    except Exception:
                        listen_port_row.set_text('1080')
            except Exception:
                pass
        type_row.connect('notify::selected', _sync_visibility)
        _sync_visibility()

        # Run dialog
        resp = dialog.run() if hasattr(dialog, 'run') else None
        # GTK4 dialogs don't block run; use response signal fallback
        if resp is None:
            def _on_resp(dlg, response_id):
                if response_id == Gtk.ResponseType.OK:
                    self._save_rule_from_editor(existing_rule, type_row, listen_addr_row, listen_port_row, remote_host_row, remote_port_row)
                dlg.destroy()
            dialog.connect('response', _on_resp)
            dialog.show()
        else:
            if resp == Gtk.ResponseType.OK:
                self._save_rule_from_editor(existing_rule, type_row, listen_addr_row, listen_port_row, remote_host_row, remote_port_row)
            dialog.destroy()

    def _save_rule_from_editor(self, existing_rule, type_row, listen_addr_row, listen_port_row, remote_host_row, remote_port_row):
        idx = type_row.get_selected()
        rtype = 'local' if idx == 0 else ('remote' if idx == 1 else 'dynamic')
        listen_addr = listen_addr_row.get_text().strip() or '127.0.0.1'
        try:
            listen_port = int((listen_port_row.get_text() or '0').strip() or '0')
        except Exception:
            listen_port = 0
        if listen_port <= 0:
            self.show_error(_("Please enter a valid listen port (> 0)"))
            return
        rule = {
            'type': rtype,
            'enabled': True,
            'listen_addr': listen_addr,
            'listen_port': listen_port,
        }
        if rtype == 'local':
            # LocalForward: [listen_addr:]listen_port remote_host:remote_port
            rule['remote_host'] = remote_host_row.get_text().strip() or 'localhost'
            try:
                rule['remote_port'] = int((remote_port_row.get_text() or '0').strip() or '0')
            except Exception:
                rule['remote_port'] = 0
        elif rtype == 'remote':
            # RemoteForward: [listen_addr:]listen_port local_host:local_port
            rule['local_host'] = remote_host_row.get_text().strip() or 'localhost'
            try:
                rule['local_port'] = int((remote_port_row.get_text() or '0').strip() or '0')
            except Exception:
                rule['local_port'] = 0

        if not hasattr(self, 'forwarding_rules') or self.forwarding_rules is None:
            self.forwarding_rules = []

        if existing_rule and existing_rule in self.forwarding_rules:
            idx_existing = self.forwarding_rules.index(existing_rule)
            self.forwarding_rules[idx_existing] = rule
        else:
            self.forwarding_rules.append(rule)

        self.load_port_forwarding_rules()

    def _autosave_forwarding_changes(self):
        """Disabled autosave to avoid log floods; saving occurs on dialog Save."""
        return
    
    def on_cancel_clicked(self, button):
        """Handle cancel button click"""
        self.close()
    
    def on_save_clicked(self, *_args):
        """Handle save button click or dialog save response"""
        # Validate required fields
        if not self.nickname_row.get_text().strip():
            self.show_error(_("Please enter a nickname for this connection"))
            return
            
        if not self.host_row.get_text().strip():
            self.show_error(_("Please enter a hostname or IP address"))
            return
        
        # Initialize forwarding_rules list if needed
        if not hasattr(self, 'forwarding_rules') or self.forwarding_rules is None:
            self.forwarding_rules = []
        
        # Persist exactly what is in the editor list (enabled rules only) and sanitize
        forwarding_rules = self._sanitize_forwarding_rules(
            [dict(r) for r in self.forwarding_rules if r.get('enabled', True)]
        )
        try:
            logger.info(
                "ConnectionDialog save: %d forwarding rules before sanitize, %d after sanitize",
                len(self.forwarding_rules or []), len(forwarding_rules or [])
            )
            logger.debug("Forwarding rules (sanitized): %s", forwarding_rules)
        except Exception:
            pass
        
        # Gather connection data
        connection_data = {
            'nickname': self.nickname_row.get_text().strip(),
            'host': self.host_row.get_text().strip(),
            'username': self.username_row.get_text().strip(),
            'port': int(self.port_row.get_text().strip() or '22'),
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
            # Perform an immediate write via the manager as a safety net
            try:
                if getattr(self, 'parent_window', None) is not None and hasattr(self.parent_window, 'connection_manager'):
                    logger.info("Saving connection immediately via manager from dialog (rules=%d)", len(forwarding_rules))
                    self.parent_window.connection_manager.update_connection(self.connection, connection_data)
            except Exception as e:
                logger.error(f"Immediate save via manager failed: {e}")
            
        # Emit signal with connection data
        self.emit('connection-saved', connection_data)
        self.close()

    def _sanitize_forwarding_rules(self, rules):
        """Validate and normalize forwarding rules before saving.
        - Ensure listen_addr defaults to 127.0.0.1 (or 0.0.0.0 for remote if provided as such)
        - Ensure listen_port > 0
        - For local/remote: ensure remote_host non-empty and remote_port > 0
        Invalid rules are dropped silently.
        """
        sanitized = []
        for r in rules or []:
            try:
                rtype = r.get('type')
                listen_addr = (r.get('listen_addr') or '').strip() or '127.0.0.1'
                listen_port = int(r.get('listen_port') or 0)
                if listen_port <= 0:
                    continue
                if rtype == 'local':
                    remote_host = (r.get('remote_host') or '').strip() or 'localhost'
                    remote_port = int(r.get('remote_port') or 0)
                    if remote_port <= 0:
                        continue
                    sanitized.append({
                        'type': 'local',
                        'enabled': True,
                        'listen_addr': listen_addr,
                        'listen_port': listen_port,
                        'remote_host': remote_host,
                        'remote_port': remote_port,
                    })
                elif rtype == 'remote':
                    local_host = (r.get('local_host') or r.get('remote_host') or '').strip() or 'localhost'
                    local_port = int(r.get('local_port') or r.get('remote_port') or 0)
                    if local_port <= 0:
                        continue
                    sanitized.append({
                        'type': 'remote',
                        'enabled': True,
                        'listen_addr': listen_addr,
                        'listen_port': listen_port,
                        'local_host': local_host,
                        'local_port': local_port,
                    })
                elif rtype == 'dynamic':
                    sanitized.append({
                        'type': 'dynamic',
                        'enabled': True,
                        'listen_addr': listen_addr,
                        'listen_port': listen_port,
                    })
            except Exception:
                # Skip malformed rule
                pass
        return sanitized

    def on_response(self, dialog, response_id):
        if str(response_id) == 'save':
            self.on_save_clicked()
        else:
            self.close()
    
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
                        'listen_port': int((self.local_port_row.get_text() or '0').strip() or '0'),
                        'remote_host': self.remote_host_row.get_text().strip() or 'localhost',
                        'remote_port': int((self.remote_port_row.get_text() or '0').strip() or '0')
                    })
                elif rule_type == 'remote' and hasattr(self, 'remote_bind_host_row') and hasattr(self, 'remote_bind_port_row') and hasattr(self, 'dest_host_row') and hasattr(self, 'dest_port_row'):
                    rule.update({
                        'listen_addr': self.remote_bind_host_row.get_text().strip() or 'localhost',
                        'listen_port': int((self.remote_bind_port_row.get_text() or '0').strip() or '0'),
                        'local_host': self.dest_host_row.get_text().strip() or 'localhost',
                        'local_port': int((self.dest_port_row.get_text() or '0').strip() or '0')
                    })
                elif rule_type == 'dynamic' and hasattr(self, 'dynamic_port_row'):
                    rule.update({
                        'listen_addr': (self.dynamic_bind_row.get_text().strip() if hasattr(self, 'dynamic_bind_row') else '') or '127.0.0.1',
                        'listen_port': int((self.dynamic_port_row.get_text() or '0').strip() or '0')
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
