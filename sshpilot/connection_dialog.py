"""
Connection Dialog for sshPilot
Dialog for adding/editing SSH connections
"""

import os
import logging
import gettext
import re
import ipaddress
import socket
import subprocess
from typing import Optional, Dict, Any

from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gdk, Pango, PangoFT2
from .port_utils import get_port_checker

# Initialize gettext
try:
    from . import gettext as _
except ImportError:
    # Fallback for when gettext is not available
    _ = lambda s: s

logger = logging.getLogger(__name__)

class ValidationResult:
    def __init__(self, is_valid: bool = True, message: str = "", severity: str = "info"):
        self.is_valid = is_valid
        self.message = message
        self.severity = severity  # "error", "warning", "info"

class SSHConnectionValidator:
    def __init__(self):
        self.reserved_usernames = {
            'root', 'daemon', 'bin', 'sys', 'sync', 'games', 'man', 'lp', 'mail',
            'news', 'uucp', 'proxy', 'www-data', 'backup', 'list', 'irc', 'gnats',
            'nobody', 'systemd-timesync', 'systemd-network', 'systemd-resolve'
        }
        self.common_ssh_ports = {22, 2222, 222, 2022}
        self.system_ports = set(range(1, 1024))
        self.service_ports = {
            21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS",
            80: "HTTP", 110: "POP3", 143: "IMAP", 443: "HTTPS",
            993: "IMAPS", 995: "POP3S", 3389: "RDP", 5432: "PostgreSQL",
            3306: "MySQL", 27017: "MongoDB", 6379: "Redis", 5672: "RabbitMQ"
        }
        self.existing_names: set[str] = set()
        self.valid_tlds = {
            'com','org','net','edu','gov','mil','int','biz','info','name','pro','aero','coop','museum',
            'local','localhost','test','invalid',
            'us','uk','ca','au','de','fr','jp','cn','ru','br','in','it','es','mx','kr','nl','se','no','dk','fi','ch','at','be','ie'
        }

    def set_existing_names(self, names: set[str]):
        self.existing_names = {str(n).strip().lower() for n in (names or set())}

    def validate_connection_name(self, name: str) -> 'ValidationResult':
        if not name or not name.strip():
            return ValidationResult(False, _("Connection name is required"), "error")
        name = name.strip()
        if name.strip().lower() in self.existing_names:
            return ValidationResult(False, _("Nickname already exists"), "error")
        return ValidationResult(True, _("Valid connection name"))

    def _validate_ip_address(self, ip_str: str) -> 'ValidationResult':
        try:
            ip = ipaddress.ip_address(ip_str)
            if ip.is_loopback:
                return ValidationResult(True, _("Loopback address (localhost)"), "info")
            elif ip.is_private:
                return ValidationResult(True, _("Private network address"), "info")
            elif ip.is_multicast:
                return ValidationResult(False, _("Multicast addresses not supported"), "error")
            elif getattr(ip, 'is_reserved', False):
                return ValidationResult(False, _("Reserved IP address"), "error")
            elif ip.version == 4 and str(ip).startswith('169.254.'):
                return ValidationResult(True, _("Link-local address"), "warning")
            return ValidationResult(True, _("Valid IPv{ver} address").format(ver=ip.version))
        except ValueError:
            return ValidationResult(False, _("Invalid IP address format"), "error")

    def _validate_hostname(self, hostname: str) -> 'ValidationResult':
        if len(hostname) > 253:
            return ValidationResult(False, _("Hostname too long (max 253 characters)"), "error")
        # Reject leading/trailing dot and consecutive dots
        if hostname.startswith('.'):
            return ValidationResult(False, _("Hostname cannot start with dot"), "error")
        if hostname.endswith('.'):
            return ValidationResult(False, _("Hostname cannot end with dot"), "error")
        if '..' in hostname:
            return ValidationResult(False, _("Hostname cannot contain consecutive dots"), "error")
        if not re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$', hostname):
            return ValidationResult(False, _("Invalid hostname format"), "error")
        labels = hostname.split('.')
        for label in labels:
            if not label:
                return ValidationResult(False, _("Empty hostname segment"), "error")
            if len(label) > 63:
                return ValidationResult(False, _("Hostname segment too long (max 63 chars)"), "error")
            if label.startswith('-') or label.endswith('-'):
                return ValidationResult(False, _("Hostname segment cannot start/end with hyphen"), "error")
            # Disallow all-digit TLDs and label of only digits for TLD
            # We'll check the last label separately as TLD
        if hostname.lower() in ['localhost', '127.0.0.1', '::1']:
            return ValidationResult(True, _("Local hostname"), "info")
        if '.' not in hostname:
            return ValidationResult(True, _("Consider using fully qualified domain name"), "warning")
        # Validate TLD: must start with a letter, not all-digit
        tld = labels[-1]
        if not re.match(r'^[A-Za-z][A-Za-z0-9-]{1,}$', tld):
            return ValidationResult(False, _("Invalid top-level domain"), "error")
        if re.fullmatch(r'\d+', tld):
            return ValidationResult(False, _("Invalid top-level domain"), "error")
        # Warn if TLD unknown/uncommon (alphabetic, not in list, not 2-letter ccTLD)
        if tld.isalpha() and tld.lower() not in self.valid_tlds and len(tld) != 2:
            return ValidationResult(True, _("Unknown or uncommon top-level domain"), "warning")
        return ValidationResult(True, _("Valid hostname"))

    def validate_hostname(self, hostname: str) -> 'ValidationResult':
        if not hostname or not hostname.strip():
            return ValidationResult(False, _("Hostname is required"), "error")
        hostname = hostname.strip()
        ip_result = self._validate_ip_address(hostname)
        if ip_result.is_valid or not ip_result.message.startswith("Invalid IP"):
            return ip_result
        # If looks like numeric IPv4 but invalid, treat as error explicitly
        if re.fullmatch(r"[0-9.]+", hostname):
            return ValidationResult(False, _("Invalid IPv4 address format"), "error")
        # Pure structural validation (avoid DNS on typing to reduce lag)
        return self._validate_hostname(hostname)

    def validate_port(self, port: str, context: str = "SSH") -> 'ValidationResult':
        if not port or not str(port).strip():
            return ValidationResult(False, _("Port is required"), "error")
        try:
            port_num = int(str(port).strip())
        except ValueError:
            return ValidationResult(False, _("Port must be a number"), "error")
        if not (1 <= port_num <= 65535):
            return ValidationResult(False, _("Port must be between 1-65535"), "error")
        if port_num in self.system_ports:
            if port_num in self.service_ports:
                service = self.service_ports[port_num]
                if context == "SSH" and port_num in self.common_ssh_ports:
                    return ValidationResult(True, _("Standard {svc} port").format(svc=service), "info")
                else:
                    return ValidationResult(True, _("System port for {svc} service").format(svc=service), "warning")
            else:
                return ValidationResult(True, _("System port - requires administrator privileges"), "warning")
        if context == "SSH" and port_num not in self.common_ssh_ports:
            if port_num in self.service_ports:
                service = self.service_ports[port_num]
                return ValidationResult(True, _("Unusual for SSH - typically used for {svc}").format(svc=service), "warning")
            elif port_num > 49152:
                return ValidationResult(True, _("Dynamic port range"), "info")
        return ValidationResult(True, _("Valid port number"))

    def validate_username(self, username: str) -> 'ValidationResult':
        if not username or not username.strip():
            return ValidationResult(False, _("Username is required"), "error")
        return ValidationResult(True, _("Valid username"))

    def verify_key_passphrase(self, key_path: str, passphrase: str) -> bool:
        """Verify that the passphrase matches the private key using ssh-keygen -y"""
        if not key_path or not os.path.exists(key_path):
            return False
        
        try:
            # Run ssh-keygen -y to test the passphrase
            result = subprocess.run([
                'ssh-keygen', '-y', '-P', passphrase, '-f', key_path
            ], capture_output=True, text=True, timeout=10)
            
            # Exit code 0 means the passphrase is valid
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout verifying passphrase for key: {key_path}")
            return False
        except subprocess.CalledProcessError:
            # This shouldn't happen since we're capturing output, but handle it
            return False
        except Exception as e:
            logger.error(f"Error verifying passphrase for key {key_path}: {e}")
            return False

class SSHConfigEntry(GObject.Object):
    """Data model for SSH config entries"""
    
    def __init__(self, key="", value=""):
        super().__init__()
        self.key = key
        self.value = value

class SSHConfigAdvancedTab(Gtk.Box):
    """Advanced SSH Configuration Tab for GTK 4"""
    
    def __init__(self, connection_manager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)
        
        self.connection_manager = connection_manager
        
        # SSH config options list
        self.ssh_options = [
            'AddKeysToAgent', 'AddressFamily', 'BatchMode', 'BindAddress', 'BindInterface',
            'CanonicalDomains', 'CanonicalizeFallbackLocal', 'CanonicalizeHostname',
            'CanonicalizeMaxDots', 'CanonicalizePermittedCNAMEs', 'CASignatureAlgorithms',
            'CertificateFile', 'CheckHostIP', 'Ciphers', 'ClearAllForwardings', 'Compression',
            'ConnectionAttempts', 'ConnectTimeout', 'ControlMaster', 'ControlPath',
            'ControlPersist', 'DynamicForward', 'EnableSSHKeysign', 'EscapeChar',
            'ExitOnForwardFailure', 'FingerprintHash', 'ForwardAgent', 'ForwardX11',
            'ForwardX11Timeout', 'ForwardX11Trusted', 'GatewayPorts', 'GlobalKnownHostsFile',
            'GSSAPIAuthentication', 'GSSAPIClientIdentity', 'GSSAPIDelegateCredentials',
            'GSSAPIKeyExchange', 'GSSAPIRenewalForcesRekey', 'GSSAPIServerIdentity',
            'GSSAPITrustDns', 'HashKnownHosts', 'Host', 'HostbasedAcceptedAlgorithms',
            'HostbasedAuthentication', 'HostKeyAlgorithms', 'HostKeyAlias', 'HostName',
            'IdentitiesOnly', 'IdentityAgent', 'IdentityFile', 'IgnoreUnknown', 'Include',
            'IPQoS', 'KbdInteractiveAuthentication', 'KbdInteractiveDevices', 'KexAlgorithms',
            'KnownHostsCommand', 'LocalCommand', 'LocalForward', 'LogLevel', 'MACs', 'Match',
            'NoHostAuthenticationForLocalhost', 'NumberOfPasswordPrompts', 'PasswordAuthentication',
            'PermitLocalCommand', 'PermitRemoteOpen', 'PKCS11Provider', 'Port',
            'PreferredAuthentications', 'ProxyCommand', 'ProxyJump', 'ProxyUseFdpass',
            'PubkeyAcceptedAlgorithms', 'PubkeyAuthentication', 'RekeyLimit', 'RemoteCommand',
            'RemoteForward', 'RequestTTY', 'RequiredRSASize', 'RevokedHostKeys', 'SecurityKeyProvider',
            'SendEnv', 'ServerAliveCountMax', 'ServerAliveInterval', 'SessionType', 'SetEnv',
            'StdinNull', 'StreamLocalBindMask', 'StreamLocalBindUnlink', 'StrictHostKeyChecking',
            'SyslogFacility', 'TCPKeepAlive', 'Tunnel', 'TunnelDevice', 'UpdateHostKeys',
            'User', 'UserKnownHostsFile', 'UsePrivilegedPort', 'VerifyHostKeyDNS',
            'VisualHostKey', 'XAuthLocation'
        ]
        
        # Store config entries
        self.config_entries = []
        
        self.setup_ui()
        
    def setup_ui(self):
        """Setup the user interface"""
        
        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title = Gtk.Label(label="Advanced SSH Configuration")
        title.set_markup("<b>Advanced SSH Configuration</b>")
        title.set_halign(Gtk.Align.START)
        
        subtitle = Gtk.Label(label="Add custom SSH configuration options")
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        
        header.append(title)
        header.append(subtitle)
        self.append(header)
        
        # Add button (positioned at the top)
        self.add_button = Gtk.Button(label=_("Add SSH Option"))
        self.add_button.set_icon_name("list-add-symbolic")
        self.add_button.set_tooltip_text(_("Add a new SSH configuration option"))
        self.add_button.connect("clicked", self.on_add_option)
        self.add_button.set_halign(Gtk.Align.START)
        self.add_button.set_margin_bottom(12)
        self.append(self.add_button)
        
        # Scrolled window for config entries
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(200)
        scrolled.set_vexpand(True)
        
        # Main content box
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scrolled.set_child(self.content_box)
        
        # Grid header
        header_grid = Gtk.Grid()
        header_grid.set_column_spacing(12)
        header_grid.set_margin_bottom(6)
        
        key_header = Gtk.Label(label="SSH Option")
        key_header.set_markup("<b>Keyword</b>")
        key_header.set_halign(Gtk.Align.START)
        key_header.set_hexpand(True)
        
        value_header = Gtk.Label(label="Value")
        value_header.set_markup("<b>Value</b>")
        value_header.set_halign(Gtk.Align.START)
        value_header.set_hexpand(True)
        
        header_grid.attach(key_header, 0, 0, 1, 1)
        header_grid.attach(value_header, 1, 0, 1, 1)
        
        self.content_box.append(header_grid)
        
        # Empty state label
        self.empty_label = Gtk.Label(label="No custom SSH options configured.\nClick 'Add' button to get started.")
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_justify(Gtk.Justification.CENTER)
        self.empty_label.set_margin_top(24)
        self.empty_label.set_margin_bottom(24)
        self.content_box.append(self.empty_label)
        
        self.append(scrolled)
        
        # Config preview section
        self.setup_config_preview()
        
    def setup_config_preview(self):
        """Setup the SSH config preview section"""
        self.preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.preview_box.set_margin_top(12)
        
        preview_title = Gtk.Label(label="Generated SSH Config")
        preview_title.set_markup("<b>Generated SSH Config</b>")
        preview_title.set_halign(Gtk.Align.START)
        
        # Text view for config preview
        self.config_text_view = Gtk.TextView()
        self.config_text_view.set_editable(False)
        self.config_text_view.set_monospace(True)
        self.config_text_view.add_css_class("monospace")
        self.config_text_view.add_css_class("small-font")
        
        # Apply CSS for smaller font
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
        .small-font {
            font-size: 11px;
        }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        
        preview_scrolled = Gtk.ScrolledWindow()
        preview_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        preview_scrolled.set_min_content_height(150)
        preview_scrolled.set_child(self.config_text_view)
        
        preview_desc = Gtk.Label(label="To edit the SSH config file directly, press the button below.")
        preview_desc.add_css_class("dim-label")
        preview_desc.set_halign(Gtk.Align.START)
        preview_desc.set_wrap(True)
        
        self.preview_box.append(preview_title)
        self.preview_box.append(preview_scrolled)
        self.preview_box.append(preview_desc)
        
        # Always show preview
        self.append(self.preview_box)
        
        

        
    def create_config_entry_row(self):
        """Create a new config entry row"""
        row_grid = Gtk.Grid()
        row_grid.set_column_spacing(12)
        row_grid.set_margin_bottom(6)
        
        # SSH option dropdown (only supported options)
        key_dropdown = Gtk.DropDown()
        key_dropdown.set_hexpand(False)  # Don't expand horizontally
        key_dropdown.set_size_request(200, -1)  # Fixed width of 200px
        
        # Create string list for dropdown
        string_list = Gtk.StringList()
        string_list.append("Select SSH option...")
        for option in self.ssh_options:
            string_list.append(option)
        
        key_dropdown.set_model(string_list)
        
        # Set expression for StringList items to enable search
        expr = Gtk.PropertyExpression.new(Gtk.StringObject, None, "string")
        key_dropdown.set_expression(expr)
        
        key_dropdown.set_selected(0)  # Default to "Select SSH option..."
        
        # Enable search functionality
        key_dropdown.set_enable_search(True)
        
        # Value entry
        value_entry = Gtk.Entry()
        value_entry.set_placeholder_text("Enter value...")
        value_entry.set_hexpand(True)
        value_entry.connect("activate", self.on_value_entry_activate, row_grid)
        
        # Remove button
        remove_button = Gtk.Button()
        remove_button.set_icon_name("user-trash-symbolic")
        remove_button.add_css_class("flat")
        remove_button.add_css_class("error")
        
        # Connect signals
        key_dropdown.connect("notify::selected", self.on_entry_changed)
        value_entry.connect("changed", self.on_entry_changed)
        remove_button.connect("clicked", self.on_remove_option, row_grid)
        
        # Store entries in the grid for easy access
        row_grid.key_dropdown = key_dropdown
        row_grid.value_entry = value_entry
        
        row_grid.attach(key_dropdown, 0, 0, 1, 1)
        row_grid.attach(value_entry, 1, 0, 1, 1)
        row_grid.attach(remove_button, 2, 0, 1, 1)
        
        return row_grid
        
    def on_add_option(self, button):
        """Add a new SSH option entry"""
        entry_row = self.create_config_entry_row()
        self.config_entries.append(entry_row)
        
        # Hide empty label if it's the first entry
        if len(self.config_entries) == 1:
            self.empty_label.set_visible(False)
            
        # Append the new row to the content box
        self.content_box.append(entry_row)
        
        # Only focus on the new value entry if this was triggered by user clicking the add button
        # (not when loading existing data)
        if button is not None:
            entry_row.value_entry.grab_focus()
        
    def on_remove_option(self, button, row_grid):
        """Remove a SSH option entry"""
        self.config_entries.remove(row_grid)
        
        # Check if the widget is still a child of the content box before removing
        if row_grid.get_parent() == self.content_box:
            self.content_box.remove(row_grid)
        
        # Show empty label if no entries left
        if len(self.config_entries) == 0:
            self.empty_label.set_visible(True)
        else:
            self.update_config_preview()
            
    def on_entry_changed(self, widget, pspec=None):
        """Handle entry text changes"""
        self.update_config_preview()
        # Update the parent connection object if we're editing
        self._update_parent_connection()
        
    def on_value_entry_activate(self, entry, row_grid):
        """Handle Enter key press in value entry - move to next row or add new one"""
        current_index = self.config_entries.index(row_grid)
        
        # If this is the last row, add a new one
        if current_index == len(self.config_entries) - 1:
            self.on_add_option(None)
            # Focus on the key entry of the new row
            new_row = self.config_entries[-1]
            new_row.key_dropdown.grab_focus()
        else:
            # Move to the next row's key entry
            next_row = self.config_entries[current_index + 1]
            next_row.key_dropdown.grab_focus()
        
    def update_config_preview(self):
        """Update the SSH config preview"""
        # Get the complete SSH config from the connection manager
        if hasattr(self, 'connection_manager') and self.connection_manager:
            try:
                # Get current connection data from the parent dialog
                parent_dialog = self.get_ancestor(Adw.Window)
                if parent_dialog and hasattr(parent_dialog, 'connection'):
                    connection = parent_dialog.connection
                    if connection:
                        # Generate the complete SSH config
                        extra_config = self.get_extra_ssh_config()
                        logger.debug(f"Advanced tab extra_ssh_config: {extra_config}")
                        
                        config_data = {
                            'nickname': getattr(connection, 'nickname', 'your-host-name'),
                            'host': getattr(connection, 'host', ''),
                            'username': getattr(connection, 'username', ''),
                            'port': getattr(connection, 'port', 22),
                            'auth_method': getattr(connection, 'auth_method', 0),
                            'key_select_mode': getattr(connection, 'key_select_mode', 0),
                            'keyfile': getattr(connection, 'keyfile', ''),
                            'certificate': getattr(connection, 'certificate', ''),
                            'x11_forwarding': getattr(connection, 'x11_forwarding', False),
                            'local_command': getattr(connection, 'local_command', ''),
                            'remote_command': getattr(connection, 'remote_command', ''),
                            'forwarding_rules': getattr(connection, 'forwarding_rules', []),
                            'extra_ssh_config': extra_config
                        }
                        
                        logger.debug(f"Config data for preview: {config_data}")
                        
                        # Generate the complete SSH config block
                        config_text = self.connection_manager.format_ssh_config_entry(config_data)
                        
                        logger.debug(f"Generated config text: {config_text}")
                        
                        buffer = self.config_text_view.get_buffer()
                        buffer.set_text(config_text)
                        return
            except Exception as e:
                logger.error(f"Error updating SSH config preview: {e}")
        
        # Fallback: show only the extra config parameters
        config_lines = []
        
        for row_grid in self.config_entries:
            key = self._get_dropdown_selected_text(row_grid.key_dropdown)
            value = row_grid.value_entry.get_text().strip()
            
            if key and value and key != "Select SSH option...":
                config_lines.append(f"    {key} {value}")
                
        config_text = "Host your-host-name\n" + "\n".join(config_lines)
        
        buffer = self.config_text_view.get_buffer()
        buffer.set_text(config_text)
            
    def get_config_entries(self):
        """Get all valid config entries"""
        entries = []
        for row_grid in self.config_entries:
            key = self._get_dropdown_selected_text(row_grid.key_dropdown)
            value = row_grid.value_entry.get_text().strip()
            
            if key and value and key != "Select SSH option...":
                entries.append((key, value))
                
        return entries
        
    def set_config_entries(self, entries):
        """Set config entries from saved data"""
        logger.debug(f"Setting config entries: {entries}")
        
        # Clear existing entries
        for row_grid in self.config_entries.copy():
            self.on_remove_option(None, row_grid)
            
        # Add new entries
        for key, value in entries:
            logger.debug(f"Adding entry: {key} = {value}")
            self.on_add_option(None)
            row_grid = self.config_entries[-1]
            # Set dropdown to the correct SSH option
            self._set_dropdown_to_option(row_grid.key_dropdown, key)
            row_grid.value_entry.set_text(value)
            logger.debug(f"Set dropdown to {key} and value to {value}")
        
        # Set focus on the add button after loading entries
        if hasattr(self, 'add_button'):
            self.add_button.grab_focus()
            
    def generate_ssh_config(self, hostname="your-host-name"):
        """Generate SSH config block"""
        entries = self.get_config_entries()
        if not entries:
            return ""
            
        config_lines = [f"Host {hostname}"]
        for key, value in entries:
            config_lines.append(f"    {key} {value}")
            
        return "\n".join(config_lines)

    def _get_dropdown_selected_text(self, dropdown):
        """Get the selected text from a dropdown"""
        try:
            selected = dropdown.get_selected()
            if selected > 0:  # Skip the first item which is "Select SSH option..."
                model = dropdown.get_model()
                if model and selected < model.get_n_items():
                    # Gtk.DropDown models provide items as Gtk.StringObject instances in
                    # newer GTK versions.  Some environments may return plain strings
                    # (or other objects) instead, so fall back to ``str()`` when the
                    # "get_string" accessor is not available.
                    item = model.get_item(selected)
                    getter = getattr(item, "get_string", None)
                    if callable(getter):
                        return getter()
                    return str(item)
        except Exception as e:
            logger.debug(f"Error getting dropdown selected text: {e}")
        return ""

    def _set_dropdown_to_option(self, dropdown, option_name):
        """Set dropdown to a specific SSH option"""
        try:
            model = dropdown.get_model()
            if model:
                logger.debug(f"Looking for option '{option_name}' in dropdown model")
                for i in range(1, model.get_n_items()):  # Start from 1 to skip "Select SSH option..."
                    item = model.get_item(i)
                    getter = getattr(item, "get_string", None)
                    model_string = getter() if callable(getter) else str(item)
                    logger.debug(f"Model item {i}: '{model_string}'")
                    # Case-insensitive comparison for SSH options
                    if model_string.lower() == option_name.strip().lower():
                        logger.debug(
                            f"Found option '{option_name}' at index {i} (matched '{model_string}')"
                        )
                        dropdown.set_selected(i)
                        return
                logger.debug(f"Option '{option_name}' not found in dropdown model")
        except Exception as e:
            logger.debug(f"Error setting dropdown to option {option_name}: {e}")



    def get_extra_ssh_config(self):
        """Get extra SSH config as a string for saving"""
        entries = self.get_config_entries()
        if not entries:
            return ""
            
        config_lines = []
        for key, value in entries:
            config_lines.append(f"{key} {value}")
            
        return "\n".join(config_lines)

    def _update_parent_connection(self):
        """Update the parent connection object with current advanced tab data"""
        try:
            parent_dialog = self.get_ancestor(Adw.Window)
            if parent_dialog and hasattr(parent_dialog, 'connection') and parent_dialog.connection:
                extra_config = self.get_extra_ssh_config()
                parent_dialog.connection.extra_ssh_config = extra_config
                # Also update the data dictionary
                if hasattr(parent_dialog.connection, 'data'):
                    parent_dialog.connection.data['extra_ssh_config'] = extra_config
                logger.debug(f"Updated parent connection with extra SSH config: {extra_config}")
        except Exception as e:
            logger.error(f"Error updating parent connection: {e}")

    def set_extra_ssh_config(self, config_string):
        """Set extra SSH config from a string"""
        logger.debug(f"set_extra_ssh_config called with: '{config_string}'")
        
        if not config_string.strip():
            logger.debug("Config string is empty, returning")
            return
            
        entries = []
        for line in config_string.split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                parts = line.split(' ', 1)
                if len(parts) == 2:
                    entries.append((parts[0].strip(), parts[1].strip()))
                elif len(parts) == 1:
                    entries.append((parts[0].strip(), "yes"))
        
        logger.debug(f"Parsed entries: {entries}")
        self.set_config_entries(entries)
        # Update preview after loading data
        self.update_config_preview()
        # Update the parent connection object if we're editing
        self._update_parent_connection()

class ConnectionDialog(Adw.Window):
    """Dialog for adding/editing SSH connections using custom layout with pinned buttons"""
    
    __gtype_name__ = 'ConnectionDialog'
    
    __gsignals__ = {
        'connection-saved': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }
    
    def __init__(self, parent, connection=None, connection_manager=None):
        super().__init__()
        
        self.parent_window = parent
        self.connection = connection
        self.connection_manager = connection_manager
        self.is_editing = connection is not None
        
        self.set_title('Edit Connection' if self.is_editing else 'New Connection')
        # Set modal and transient parent to ensure dialog stays on top
        self.set_modal(True)
        self.set_transient_for(parent)
        # Set default size for better UX
        self.set_default_size(600, 700)
        
        self.validator = SSHConnectionValidator()
        self.validation_results: Dict[str, ValidationResult] = {}
        self._save_buttons = []
        
        self.setup_ui()
        GLib.idle_add(self.load_connection_data)
    
    def setup_ui(self):
        """Set up the dialog UI with pinned buttons"""
        # Create main vertical container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        
        # Create header bar
        header_bar = Adw.HeaderBar()
        header_bar.set_show_end_title_buttons(True)
        main_box.append(header_bar)
        
        # Create scrollable content area
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.set_vexpand(True)
        
        # Create preferences content
        self.preferences_content = self.create_preferences_content()
        scrolled_window.set_child(self.preferences_content)
        
        # Create pinned bottom section with buttons
        bottom_section = self.create_bottom_section()
        
        # Assemble the layout
        main_box.append(scrolled_window)
        main_box.append(bottom_section)
        
        # Set the content
        self.set_content(main_box)
        
        # Install inline validators for key fields
        try:
            self._install_inline_validators()
        except Exception as e:
            logger.debug(f"Failed to install inline validators: {e}")
        # After building views, populate existing data if editing
        try:
            self.load_connection_data()
            # Re-run validations after loading existing values
            try:
                self._run_initial_validation()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Failed to populate connection data: {e}")
    
    def create_preferences_content(self):
        """Create the preferences content with all pages"""
        # Create a notebook for tabbed interface
        notebook = Gtk.Notebook()
        notebook.set_show_tabs(True)
        notebook.set_show_border(False)
        
        # General page
        general_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        general_page.set_margin_top(12)
        general_page.set_margin_bottom(12)
        general_page.set_margin_start(12)
        general_page.set_margin_end(12)
        
        for group in self.build_connection_groups():
            general_page.append(group)
        
        general_label = Gtk.Label(label=_("Connection"))
        notebook.append_page(general_page, general_label)

        # Port Forwarding page
        forwarding_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        forwarding_page.set_margin_top(12)
        forwarding_page.set_margin_bottom(12)
        forwarding_page.set_margin_start(12)
        forwarding_page.set_margin_end(12)
        
        for group in self.build_port_forwarding_groups():
            forwarding_page.append(group)
        
        forwarding_label = Gtk.Label(label=_("Port Forwarding"))
        notebook.append_page(forwarding_page, forwarding_label)

        # Advanced page
        advanced_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        advanced_page.set_margin_top(12)
        advanced_page.set_margin_bottom(12)
        advanced_page.set_margin_start(12)
        advanced_page.set_margin_end(12)
        
        # Create the advanced tab and wrap it in a preferences group
        self.advanced_tab = SSHConfigAdvancedTab(self.connection_manager)
        advanced_group = Adw.PreferencesGroup()
        advanced_group.add(self.advanced_tab)
        advanced_page.append(advanced_group)
        
        advanced_label = Gtk.Label(label=_("Advanced"))
        notebook.append_page(advanced_page, advanced_label)
        
        return notebook
    
    def create_bottom_section(self):
        """Create the pinned bottom section with save/cancel buttons"""
        bottom_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        
        # Visual separator
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        bottom_container.append(separator)
        
        # Button container with proper spacing
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        button_box.set_margin_start(24)
        button_box.set_margin_end(24)
        button_box.set_margin_top(12)
        button_box.set_margin_bottom(12)
        button_box.set_halign(Gtk.Align.END)  # Align buttons to the right
        
        # Cancel button
        self.cancel_button = Gtk.Button(label=_("Cancel"))
        self.cancel_button.connect("clicked", self.on_cancel_clicked)
        
        # Save button with suggested action styling
        self.save_button = Gtk.Button(label=_("Save"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self.on_save_clicked)
        
        button_box.append(self.cancel_button)
        button_box.append(self.save_button)
        
        bottom_container.append(button_box)
        
        # Store reference to save button for validation updates
        self._save_buttons = [self.save_button]
        
        # Setup keyboard shortcuts
        self.setup_keyboard_shortcuts()
        
        return bottom_container
    
    def setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts for common actions"""
        
        shortcut_controller = Gtk.ShortcutController()
        
        # Ctrl+S to save
        save_shortcut = Gtk.Shortcut()
        save_shortcut.set_trigger(Gtk.ShortcutTrigger.parse_string("<Control>s"))
        save_shortcut.set_action(Gtk.CallbackAction.new(
            lambda widget, args: self.on_save_clicked(None)
        ))
        shortcut_controller.add_shortcut(save_shortcut)
        
        # Escape to cancel
        cancel_shortcut = Gtk.Shortcut()
        cancel_shortcut.set_trigger(Gtk.ShortcutTrigger.parse_string("Escape"))
        cancel_shortcut.set_action(Gtk.CallbackAction.new(
            lambda widget, args: self.on_cancel_clicked(None)
        ))
        shortcut_controller.add_shortcut(cancel_shortcut)
        
        self.add_controller(shortcut_controller)
    
    def on_auth_method_changed(self, combo_row, param):
        """Handle authentication method change"""
        is_key_based = combo_row.get_selected() == 0  # 0 is for key-based auth
        
        # Show/hide key file and passphrase fields for key-based auth
        if hasattr(self, 'keyfile_row'):
            self.keyfile_row.set_visible(is_key_based)
        if hasattr(self, 'key_passphrase_row'):
            self.key_passphrase_row.set_visible(is_key_based)
        if hasattr(self, 'key_select_row'):
            self.key_select_row.set_visible(is_key_based)
            
        # Password field is always available since key-based auth can also require a password
        if hasattr(self, 'password_row'):
            self.password_row.set_visible(True)
        if hasattr(self, 'pubkey_auth_row'):
            self.pubkey_auth_row.set_visible(not is_key_based)

        # Also update browse availability per key selection mode
        try:
            self.on_key_select_changed(self.key_select_row, None)
        except Exception:
            pass
        

    def on_key_select_changed(self, combo_row, param):
        """Enable browse button only when 'Use a specific key' is selected."""
        try:
            use_specific = (combo_row.get_selected() == 1) if combo_row else False
        except Exception:
            use_specific = False
        # Enable/disable keyfile browse UI
        try:
            if hasattr(self, 'keyfile_btn'):
                self.keyfile_btn.set_sensitive(use_specific)
            if hasattr(self, 'keyfile_row'):
                self.keyfile_row.set_sensitive(use_specific)
            if hasattr(self, 'key_dropdown'):
                self.key_dropdown.set_sensitive(use_specific)
            if hasattr(self, 'certificate_row'):
                self.certificate_row.set_sensitive(use_specific)
            if hasattr(self, 'cert_dropdown'):
                self.cert_dropdown.set_sensitive(use_specific)
        except Exception:
            pass
        
    

    
            # Read and parse the SSH config file
            current_host = None
            current_block = []
            in_target_host = False
            
            with open(ssh_config_path, 'r') as f:
                for line in f:
                    stripped_line = line.strip()
                    
                    # Skip empty lines and comments
                    if not stripped_line or stripped_line.startswith('#'):
                        if in_target_host:
                            current_block.append(line.rstrip())
                        continue
                    
                    # Check if this is a Host directive
                    if stripped_line.lower().startswith('host '):
                        # If we were in a target host block, we've reached the end
                        if in_target_host:
                            break
                        
                        # Extract host name(s)
                        host_part = stripped_line[5:].strip()
                        host_names = [h.strip() for h in host_part.split()]
                        
                        # Check if our target host is in this Host directive
                        if host_nickname in host_names:
                            current_host = host_nickname
                            in_target_host = True
                            current_block.append(line.rstrip())
                        else:
                            current_host = None
                    elif in_target_host:
                        # We're in the target host block, add this line
                        current_block.append(line.rstrip())
            
            if current_block:
                return '\n'.join(current_block)
            else:
                logger.warning(f"No SSH config block found for host: {host_nickname}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to load SSH config from file: {e}")
            return None
    
    def validate_ssh_config_syntax(self, config_text):
        """Basic SSH config syntax validation"""
        try:
            lines = config_text.strip().split('\n')
            for i, line in enumerate(lines, 1):
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith('#'):
                    continue
                
                # Check for Host directive (should be at start of line)
                if line.startswith('Host '):
                    host_name = line[5:].strip()
                    if not host_name:
                        return False, f"Line {i}: Host directive requires a name"
                
                # Check for indented options (should start with spaces/tabs)
                elif line.startswith(' ') or line.startswith('\t'):
                    # Basic option format check
                    if ' ' not in line.strip():
                        return False, f"Line {i}: Invalid option format"
                    
                    option_parts = line.strip().split(' ', 1)
                    if len(option_parts) < 2:
                        return False, f"Line {i}: Option requires a value"
                
                # Check for non-indented, non-comment lines
                elif not line.startswith('#'):
                    return False, f"Line {i}: Expected 'Host' directive or indented option"
            
            return True, "SSH config syntax is valid"
            
        except Exception as e:
            return False, f"Validation error: {e}"
    
    def _generate_ssh_config_from_settings(self):
        """Generate SSH config block from current connection settings"""
        try:
            # Get current connection data
            nickname = getattr(self, 'nickname_row', None)
            host = getattr(self, 'host_row', None)
            username = getattr(self, 'username_row', None)
            port = getattr(self, 'port_row', None)
            auth_method = getattr(self, 'auth_method_row', None)
            key_select_mode = getattr(self, 'key_select_row', None)
            
            # Get values from UI or use defaults
            nickname_val = nickname.get_text().strip() if nickname else "my-server"
            host_val = host.get_text().strip() if host else "example.com"
            username_val = username.get_text().strip() if username else "user"
            port_val = port.get_text().strip() if port else "22"
            
            # Get authentication settings
            auth_method_val = auth_method.get_selected() if auth_method else 0
            key_select_mode_val = key_select_mode.get_selected() if key_select_mode else 0
            
            # Get keyfile and certificate if available
            keyfile_val = ""
            certificate_val = ""
            if hasattr(self, 'keyfile_row') and self.keyfile_row.get_subtitle():
                keyfile_val = self.keyfile_row.get_subtitle()
            elif hasattr(self, '_selected_keyfile_path') and self._selected_keyfile_path:
                keyfile_val = self._selected_keyfile_path
            elif hasattr(self, 'connection') and self.connection:
                keyfile_val = getattr(self.connection, 'keyfile', '')
            
            # Validate keyfile path - skip placeholder text
            if keyfile_val and keyfile_val.lower() in ['select key file or leave empty for auto-detection', '']:
                keyfile_val = ''
            
            if hasattr(self, 'certificate_row') and self.certificate_row.get_subtitle():
                certificate_val = self.certificate_row.get_subtitle()
            elif hasattr(self, '_selected_cert_path') and self._selected_cert_path:
                certificate_val = self._selected_cert_path
            elif hasattr(self, 'connection') and self.connection:
                certificate_val = getattr(self.connection, 'certificate', '')
            
            # Validate certificate path - skip placeholder text
            if certificate_val and certificate_val.lower() in ['select certificate file (optional)', '']:
                certificate_val = ''
            
            # Build SSH config block
            config_lines = []
            config_lines.append(f"# SSH Config Block for {nickname_val}")
            config_lines.append(f"Host {nickname_val}")
            
            # Add basic connection info
            config_lines.append(f"    HostName {host_val}")
            config_lines.append(f"    User {username_val}")
            
            # Add port if not default
            if port_val and port_val != "22":
                config_lines.append(f"    Port {port_val}")
            
            # Add authentication settings
            password_val = self.password_row.get_text().strip() if hasattr(self, 'password_row') else ''

            if auth_method_val == 0:  # Key-based auth (password optional)
                if key_select_mode_val == 1 and keyfile_val:  # Specific key
                    config_lines.append(f"    IdentityFile {keyfile_val}")
                    config_lines.append("    IdentitiesOnly yes")

                    # Add certificate if specified (validate to skip placeholder text)
                    if certificate_val and certificate_val.lower() not in ['select certificate file (optional)', '']:
                        config_lines.append(f"    CertificateFile {certificate_val}")
                # Add combined authentication if a password is provided
                if password_val:
                    config_lines.append(
                        "    PreferredAuthentications gssapi-with-mic,hostbased,publickey,keyboard-interactive,password"
                    )
                # For automatic key selection, don't add IdentityFile
            else:  # Password auth only
                config_lines.append("    PreferredAuthentications password")
                if self.pubkey_auth_row.get_active():
                    config_lines.append("    PubkeyAuthentication no")
            
            # Add X11 forwarding if enabled
            if hasattr(self, 'x11_row') and self.x11_row.get_active():
                config_lines.append("    ForwardX11 yes")
            
            # Add local command if specified
            if hasattr(self, 'local_command_row') and self.local_command_row.get_text().strip():
                local_cmd = self.local_command_row.get_text().strip()
                config_lines.append("    PermitLocalCommand yes")
                config_lines.append(f"    LocalCommand {local_cmd}")
            
            # Add remote command if specified
            if hasattr(self, 'remote_command_row') and self.remote_command_row.get_text().strip():
                remote_cmd = self.remote_command_row.get_text().strip()
                # Ensure shell stays active after command
                if 'exec $SHELL' not in remote_cmd:
                    remote_cmd = f"{remote_cmd} ; exec $SHELL -l"
                config_lines.append(f"    RemoteCommand {remote_cmd}")
                config_lines.append("    RequestTTY yes")
            
            return '\n'.join(config_lines)
            
        except Exception as e:
            logger.debug(f"Failed to generate SSH config from settings: {e}")
            # Return a basic template if generation fails
            return f"""# SSH Config Block for this connection
# Generated from current settings
Host {getattr(self, 'nickname_row', None).get_text().strip() if hasattr(self, 'nickname_row') else 'my-server'}
    HostName {getattr(self, 'host_row', None).get_text().strip() if hasattr(self, 'host_row') else 'example.com'}
    User {getattr(self, 'username_row', None).get_text().strip() if hasattr(self, 'username_row') else 'user'}
    Port {getattr(self, 'port_row', None).get_text().strip() if hasattr(self, 'port_row') else '22'}"""
    
    def load_connection_data(self):
        """Load connection data into the dialog fields"""
        if not self.is_editing or not self.connection:
            return
        
        try:
            # Ensure UI controls exist
            required_attrs = [
                'nickname_row', 'host_row', 'username_row', 'port_row',
                'auth_method_row', 'keyfile_row', 'password_row', 'key_passphrase_row',
                'pubkey_auth_row'
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
            try:
                self.pubkey_auth_row.set_active(bool(getattr(self.connection, 'pubkey_auth_no', False)))
            except Exception:
                self.pubkey_auth_row.set_active(False)
            
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
                    # Sync the dropdown to match the loaded keyfile
                    self._sync_key_dropdown_with_current_keyfile()
                else:
                    logger.debug(f"Skipping invalid keyfile path: {keyfile_path}")
            
            # Load certificate path if present
            if hasattr(self.connection, 'certificate') and self.connection.certificate:
                cert_path = str(self.connection.certificate).strip()
                if cert_path and cert_path.lower() not in ['select certificate file (optional)', '']:
                    logger.debug(f"Setting certificate path in UI: {cert_path}")
                    self.certificate_row.set_subtitle(cert_path)
                    # Sync the dropdown to match the loaded certificate
                    self._sync_cert_dropdown_with_current_cert()
                else:
                    logger.debug(f"Skipping invalid certificate path: {cert_path}")
            
            if hasattr(self.connection, 'password') and self.connection.password:
                self.password_row.set_text(self.connection.password)
            else:
                # Fallback: fetch from keyring so the dialog shows stored password (masked)
                try:
                    mgr = getattr(self.parent_window, 'connection_manager', None)
                    if mgr and hasattr(self.connection, 'host') and hasattr(self.connection, 'username'):
                        pw = mgr.get_password(self.connection.host, self.connection.username)
                        if pw:
                            self.password_row.set_text(pw)
                except Exception:
                    pass
            # Capture original password value to detect user changes later
            try:
                self._orig_password = self.password_row.get_text()
            except Exception:
                self._orig_password = ""
                
            # Load key passphrase from connection object or from secure storage
            if hasattr(self.connection, 'key_passphrase') and self.connection.key_passphrase:
                self.key_passphrase_row.set_text(self.connection.key_passphrase)
            else:
                # Try to load from secure storage if we have a keyfile
                try:
                    if hasattr(self, 'connection_manager') and self.connection_manager and keyfile:
                        stored_passphrase = self.connection_manager.get_key_passphrase(keyfile)
                        if stored_passphrase:
                            self.key_passphrase_row.set_text(stored_passphrase)
                except Exception as e:
                    logger.debug(f"Failed to load stored passphrase: {e}")

            # Load key selection mode (prefer fresh manager copy by nickname)
            try:
                if hasattr(self, 'key_select_row'):
                    mode = None
                    # Prefer fresh parse from manager if available
                    try:
                        mgr = getattr(self.parent_window, 'connection_manager', None)
                        if mgr and hasattr(self.connection, 'nickname'):
                            fresh = mgr.find_connection_by_nickname(self.connection.nickname)
                            if fresh is not None and hasattr(fresh, 'key_select_mode'):
                                mode = int(getattr(fresh, 'key_select_mode', 0) or 0)
                    except Exception:
                        mode = None
                    if mode is None:
                        try:
                            mode = int(getattr(self.connection, 'key_select_mode', 0) or 0)
                        except Exception:
                            try:
                                mode = int(self.connection.data.get('key_select_mode', 0)) if hasattr(self.connection, 'data') else 0
                            except Exception:
                                mode = 0
                    self.key_select_row.set_selected(0 if mode != 1 else 1)
                    self.on_key_select_changed(self.key_select_row, None)
            except Exception:
                pass
            
            # Set X11 forwarding
            self.x11_row.set_active(getattr(self.connection, 'x11_forwarding', False))
            

            
                        # SSH config block content is now handled by the SSH config editor window
            # No need to load it into an inline editor anymore
            
            # Load extra SSH config into advanced tab
            try:
                extra_config = getattr(self.connection, 'extra_ssh_config', '') or ''
                logger.debug(f"Loading extra SSH config from connection: {extra_config}")
                if hasattr(self, 'advanced_tab'):
                    if extra_config:
                        self.advanced_tab.set_extra_ssh_config(extra_config)
                        logger.debug(f"Set extra SSH config in advanced tab: {extra_config}")
                    # Update the preview to show existing connection data
                    self.advanced_tab.update_config_preview()
            except Exception as e:
                logger.error(f"Error loading extra SSH config: {e}")
            
            # Load commands if present
            try:
                def _display_safe(val: str) -> str:
                    # Show exactly as in config; if user had quoted, keep quotes intact
                    if not isinstance(val, str):
                        return ''
                    return val

                if hasattr(self, 'local_command_row'):
                    local_cmd_val = ''
                    try:
                        local_cmd_val = getattr(self.connection, 'local_command', '') or (
                            self.connection.data.get('local_command') if hasattr(self.connection, 'data') else ''
                        ) or ''
                    except Exception:
                        local_cmd_val = ''
                    self.local_command_row.set_text(_display_safe(local_cmd_val))
                if hasattr(self, 'remote_command_row'):
                    remote_cmd_val = ''
                    try:
                        remote_cmd_val = getattr(self.connection, 'remote_command', '') or (
                            self.connection.data.get('remote_command') if hasattr(self.connection, 'data') else ''
                        ) or ''
                    except Exception:
                        remote_cmd_val = ''
                    self.remote_command_row.set_text(_display_safe(remote_cmd_val))
            except Exception:
                pass
            
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
    

    
    # --- Inline validation helpers ---
    def _apply_validation_to_row(self, row, result):
        try:
            if hasattr(row, 'set_subtitle'):
                row.set_subtitle(result.message or "")
        except Exception:
            pass
        # Tooltips on row and entry
        try:
            if hasattr(row, 'set_tooltip_text'):
                row.set_tooltip_text(result.message or None)
            entry = row.get_child() if hasattr(row, 'get_child') else None
            if entry is not None and hasattr(entry, 'set_tooltip_text'):
                entry.set_tooltip_text(result.message or None)
        except Exception:
            pass
        # CSS classes: clear, then set per severity
        try:
            row.remove_css_class('error')
            row.remove_css_class('warning')
        except Exception:
            pass
        try:
            if hasattr(result, 'is_valid') and not result.is_valid:
                row.add_css_class('error')
            elif hasattr(result, 'severity') and result.severity == 'warning':
                row.add_css_class('warning')
        except Exception:
            pass

    def _update_existing_names_in_validator(self):
        try:
            mgr = getattr(self.parent_window, 'connection_manager', None)
            names = set()
            if mgr and hasattr(mgr, 'connections'):
                # Normalize current connection name (when editing) to exclude it from duplicates
                current_name_norm = ''
                try:
                    if self.is_editing and self.connection:
                        current_name_norm = str(getattr(self.connection, 'nickname', '')).strip().lower()
                except Exception:
                    current_name_norm = ''
                for conn in mgr.connections or []:
                    n = getattr(conn, 'nickname', None)
                    if not n:
                        continue
                    n_norm = str(n).strip().lower()
                    # Exclude the current connection by name (case-insensitive), not by object identity
                    if current_name_norm and n_norm == current_name_norm:
                        continue
                    names.add(str(n))
            # Ensure fresh names after deletions
            try:
                if hasattr(mgr, 'load_ssh_config'):
                    mgr.load_ssh_config()
            except Exception:
                pass
            # Ensure current typed value isn't auto-included incorrectly
            self.validator.set_existing_names(names)
        except Exception:
            pass

    def _validate_field_row(self, field_name: str, row, context: str = "SSH"):
        text = (row.get_text() if hasattr(row, 'get_text') else "")
        if field_name == 'name':
            self._update_existing_names_in_validator()
            result = self.validator.validate_connection_name(text)
        elif field_name == 'hostname':
            raw = (text or '').strip()
            if raw.startswith('[') and raw.endswith(']') and len(raw) > 2:
                raw = raw[1:-1]
            result = self.validator.validate_hostname(raw)
        elif field_name == 'port':
            result = self.validator.validate_port(text, context)
        elif field_name == 'username':
            result = self.validator.validate_username(text)
        else:
            # Default: valid
            class _Dummy:
                is_valid = True
                message = ""
                severity = "info"
            result = _Dummy()
        # Store and apply to UI
        self.validation_results[field_name] = result
        self._apply_validation_to_row(row, result)
        # Update save buttons after each validation
        self._update_save_buttons()
        return result

    def _update_save_buttons(self):
        try:
            has_errors = any(
                (k in self.validation_results and not self.validation_results[k].is_valid)
                for k in ('name', 'hostname', 'port', 'username')
            )
            enabled = not has_errors
            for btn in getattr(self, '_save_buttons', []) or []:
                try:
                    btn.set_sensitive(enabled)
                except Exception:
                    pass
            if hasattr(self, 'set_response_enabled'):
                try:
                    self.set_response_enabled('save', enabled)
                except Exception:
                    pass
        except Exception:
            pass
    def _row_set_message(self, row, message: str, is_error: bool = True):
        try:
            if hasattr(row, 'set_subtitle'):
                row.set_subtitle(message or "")
        except Exception:
            pass
        # Also mirror the message into tooltips for visibility/accessibility
        try:
            if hasattr(row, 'set_tooltip_text'):
                row.set_tooltip_text(message or None)
        except Exception:
            pass
        try:
            entry = row.get_child() if hasattr(row, 'get_child') else None
            if entry is not None and hasattr(entry, 'set_tooltip_text'):
                entry.set_tooltip_text(message or None)
        except Exception:
            pass
        try:
            if is_error:
                row.add_css_class('error')
            else:
                row.remove_css_class('error')
        except Exception:
            pass

    def _row_clear_message(self, row):
        self._row_set_message(row, "", is_error=False)

    def _connect_row_validation(self, row, validator_callable):
        # Prefer notify::text on Adw.EntryRow, fallback to child Gtk.Entry changed
        try:
            row.connect('notify::text', lambda r, p: validator_callable(r))
            return
        except Exception:
            pass
        try:
            entry = row.get_child() if hasattr(row, 'get_child') else None
            if entry is not None:
                entry.connect('changed', lambda e: validator_callable(row))
        except Exception:
            pass

    def _validate_required_row(self, row, label_text: str):
        text = (row.get_text() if hasattr(row, 'get_text') else "").strip()
        if not text:
            self._row_set_message(row, _(f"{label_text} is required"), is_error=True)
            return False
        self._row_clear_message(row)
        return True

    def _is_nickname_taken(self, name: str) -> bool:
        try:
            mgr = getattr(self.parent_window, 'connection_manager', None)
            if mgr is None or not hasattr(mgr, 'connections'):
                return False
            normalized = (name or '').strip().lower()
            current_name_norm = ''
            try:
                if self.is_editing and self.connection:
                    current_name_norm = str(getattr(self.connection, 'nickname', '')).strip().lower()
            except Exception:
                current_name_norm = ''
            for conn in getattr(mgr, 'connections', []) or []:
                other_name = getattr(conn, 'nickname', None)
                if not other_name:
                    continue
                other_norm = str(other_name).strip().lower()
                # Skip the same connection object and also skip the current connection name when editing
                if current_name_norm and (conn is self.connection or other_norm == current_name_norm):
                    continue
                if other_norm == normalized:
                    return True
        except Exception:
            return False
        return False

    def _validate_nickname_row(self, row):
        text = (row.get_text() if hasattr(row, 'get_text') else "").strip()
        if not text:
            self._row_set_message(row, _("Nickname is required"), is_error=True)
            return False
        if self._is_nickname_taken(text):
            self._row_set_message(row, _("Nickname already exists"), is_error=True)
            return False
        self._row_clear_message(row)
        return True

    def _validate_host_row(self, row, allow_empty: bool = False):
        text = (row.get_text() if hasattr(row, 'get_text') else "").strip()
        if not text:
            if allow_empty:
                self._row_clear_message(row)
                return True
            self._row_set_message(row, _("Host is required"), is_error=True)
            return False
        # Support bracketed IPv6 like [::1]
        text_unbr = text[1:-1] if (text.startswith('[') and text.endswith(']') and len(text) > 2) else text
        lower = text_unbr.lower()
        if lower in ("localhost",):
            self._row_clear_message(row)
            return True
        try:
            ipaddress.ip_address(text_unbr)
            self._row_clear_message(row)
            return True
        except Exception:
            # digits/dots but not valid ip → error
            if re.fullmatch(r"[0-9.]+", text_unbr):
                self._row_set_message(row, _("Invalid IPv4 address"), is_error=True)
                return False
            # RFC1123-ish hostname
            hostname_regex = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$")
            if not hostname_regex.match(text_unbr):
                self._row_set_message(row, _("Invalid hostname"), is_error=True)
                return False
        self._row_clear_message(row)
        return True

    def _validate_port_row(self, row, label_text: str = "Port"):
        text = (row.get_text() if hasattr(row, 'get_text') else "").strip()
        if not text:
            self._row_set_message(row, _(f"{label_text} is required"), is_error=True)
            return False
        try:
            value = int(text)
            if value < 1 or value > 65535:
                self._row_set_message(row, _("Port must be between 1 and 65535"), is_error=True)
                return False
            # Clear errors; we are not styling warnings inline
            self._row_clear_message(row)
            return True
        except Exception:
            self._row_set_message(row, _("Port must be a number"), is_error=True)
            return False

    def _install_inline_validators(self):
        # General page fields
        if hasattr(self, 'nickname_row'):
            self._connect_row_validation(self.nickname_row, lambda r: self._validate_field_row('name', r))
        if hasattr(self, 'username_row'):
            self._connect_row_validation(self.username_row, lambda r: self._validate_field_row('username', r))
        if hasattr(self, 'host_row'):
            self._connect_row_validation(self.host_row, lambda r: self._validate_field_row('hostname', r))
        if hasattr(self, 'port_row'):
            self._connect_row_validation(self.port_row, lambda r: self._validate_field_row('port', r, context="SSH"))
        # Local forwarding
        if hasattr(self, 'local_port_row'):
            self._connect_row_validation(self.local_port_row, lambda r: self._validate_port_row(r, _("Local Port")))
        if hasattr(self, 'remote_host_row'):
            self._connect_row_validation(self.remote_host_row, lambda r: self._validate_host_row(r, allow_empty=False))
        if hasattr(self, 'remote_port_row'):
            self._connect_row_validation(self.remote_port_row, lambda r: self._validate_port_row(r, _("Target Port")))
        # Remote forwarding
        if hasattr(self, 'remote_bind_host_row'):
            self._connect_row_validation(self.remote_bind_host_row, lambda r: self._validate_host_row(r, allow_empty=True))
        if hasattr(self, 'remote_bind_port_row'):
            self._connect_row_validation(self.remote_bind_port_row, lambda r: self._validate_port_row(r, _("Remote port")))
        if hasattr(self, 'dest_host_row'):
            self._connect_row_validation(self.dest_host_row, lambda r: self._validate_host_row(r, allow_empty=False))
        if hasattr(self, 'dest_port_row'):
            self._connect_row_validation(self.dest_port_row, lambda r: self._validate_port_row(r, _("Destination port")))
        # Dynamic forwarding
        if hasattr(self, 'dynamic_bind_row'):
            self._connect_row_validation(self.dynamic_bind_row, lambda r: self._validate_host_row(r, allow_empty=True))
        if hasattr(self, 'dynamic_port_row'):
            self._connect_row_validation(self.dynamic_port_row, lambda r: self._validate_port_row(r, _("Local Port")))

    def _populate_detected_keys(self):
        """Populate key dropdown with detected private keys and a Browse item (reuse KeyManager.discover_keys)."""
        try:
            keys = []
            parent = getattr(self, 'parent_window', None) or None
            if parent and hasattr(parent, 'key_manager') and parent.key_manager:
                keys = parent.key_manager.discover_keys() or []
            names = []
            paths = []
            for k in keys:
                try:
                    rel = os.path.relpath(
                        k.private_path,
                        str(parent.key_manager.ssh_dir) if parent else None,
                    )
                    names.append(rel)
                    paths.append(k.private_path)
                except Exception:
                    pass
            # Add placeholder when none
            if not names:
                names.append(_("No keys detected"))
                paths.append("")
            # Add browse option
            names.append(_("Browse…"))
            paths.append("__BROWSE__")
            self._key_paths = paths
            model = Gtk.StringList()
            for n in names:
                model.append(n)
            self.key_dropdown.set_model(model)
            # Preselect currently set keyfile if present
            preselect_idx = 0
            try:
                current_path = None
                if hasattr(self, '_selected_keyfile_path') and self._selected_keyfile_path:
                    current_path = self._selected_keyfile_path
                elif hasattr(self.keyfile_row, 'get_subtitle'):
                    current_path = self.keyfile_row.get_subtitle() or None
                if (not current_path) and hasattr(self, 'connection') and self.connection:
                    current_path = getattr(self.connection, 'keyfile', None)
                if current_path and current_path in paths:
                    preselect_idx = paths.index(current_path)
            except Exception:
                preselect_idx = 0
            try:
                self.key_dropdown.set_selected(preselect_idx)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Could not populate detected keys: {e}")
                
    def _populate_detected_certificates(self):
        """Populate certificate dropdown with detected certificate files."""
        try:
            certificates = []
            names = []
            paths = []
            
            # Look for certificate files in ~/.ssh directory
            ssh_dir = os.path.expanduser("~/.ssh")
            if os.path.exists(ssh_dir) and os.path.isdir(ssh_dir):
                for filename in os.listdir(ssh_dir):
                    if filename.endswith('-cert.pub'):
                        cert_path = os.path.join(ssh_dir, filename)
                        if os.path.isfile(cert_path):
                            certificates.append(cert_path)
                            names.append(filename)
                            paths.append(cert_path)
            
            # Add placeholder when none
            if not names:
                names.append(_("No certificates detected"))
                paths.append("")
            
            # Add browse option
            names.append(_("Browse…"))
            paths.append("__BROWSE__")
            
            self._cert_paths = paths
            model = Gtk.StringList()
            for n in names:
                model.append(n)
            self.cert_dropdown.set_model(model)
            
            # Preselect certificate that matches the selected key if available
            preselect_idx = 0
            try:
                current_key_path = None
                if hasattr(self, '_selected_keyfile_path') and self._selected_keyfile_path:
                    current_key_path = self._selected_keyfile_path
                elif hasattr(self.keyfile_row, 'get_subtitle'):
                    current_key_path = self.keyfile_row.get_subtitle() or None
                if (not current_key_path) and hasattr(self, 'connection') and self.connection:
                    current_key_path = getattr(self.connection, 'keyfile', None)
                
                # Try to find matching certificate
                if current_key_path:
                    key_basename = os.path.basename(current_key_path)
                    # Remove common extensions to get base name
                    for ext in ['.pub', '.key', '.pem', '.rsa', '.dsa', '.ecdsa', '.ed25519']:
                        if key_basename.endswith(ext):
                            key_basename = key_basename[:-len(ext)]
                            break
                    
                    # Look for matching certificate
                    expected_cert_name = f"{key_basename}-cert.pub"
                    expected_cert_path = os.path.join(os.path.dirname(current_key_path), expected_cert_name)
                    
                    if expected_cert_path in paths:
                        preselect_idx = paths.index(expected_cert_path)
                        logger.debug(f"Auto-selected matching certificate: {expected_cert_name}")
            except Exception:
                preselect_idx = 0
            
            try:
                self.cert_dropdown.set_selected(preselect_idx)
            except Exception:
                pass
                
        except Exception as e:
            logger.debug(f"Could not populate detected certificates: {e}")
    
    def _auto_select_matching_certificate(self, key_path):
        """Auto-select certificate that matches the selected key"""
        try:
            if not hasattr(self, 'cert_dropdown') or not hasattr(self, '_cert_paths'):
                return
                
            # Get the base name of the key file
            key_basename = os.path.basename(key_path)
            # Remove common extensions to get base name
            for ext in ['.pub', '.key', '.pem', '.rsa', '.dsa', '.ecdsa', '.ed25519']:
                if key_basename.endswith(ext):
                    key_basename = key_basename[:-len(ext)]
                    break
            
            # Look for matching certificate
            expected_cert_name = f"{key_basename}-cert.pub"
            expected_cert_path = os.path.join(os.path.dirname(key_path), expected_cert_name)
            
            if expected_cert_path in self._cert_paths:
                cert_idx = self._cert_paths.index(expected_cert_path)
                self.cert_dropdown.set_selected(cert_idx)
                self._selected_cert_path = expected_cert_path
                if hasattr(self.certificate_row, 'set_subtitle'):
                    self.certificate_row.set_subtitle(expected_cert_path)
                logger.debug(f"Auto-selected matching certificate: {expected_cert_name}")
        except Exception as e:
            logger.debug(f"Failed to auto-select matching certificate: {e}")
                
    def _sync_key_dropdown_with_current_keyfile(self):
        """Sync the key dropdown selection with the current keyfile path"""
        try:
            if not hasattr(self, 'key_dropdown') or not hasattr(self, '_key_paths'):
                return
                
            # Get current keyfile path
            current_path = None
            if hasattr(self, '_selected_keyfile_path') and self._selected_keyfile_path:
                current_path = self._selected_keyfile_path
            elif hasattr(self.keyfile_row, 'get_subtitle'):
                current_path = self.keyfile_row.get_subtitle() or None
            if (not current_path) and hasattr(self, 'connection') and self.connection:
                current_path = getattr(self.connection, 'keyfile', None)
                
            # Find matching index in dropdown
            if current_path and current_path in self._key_paths:
                preselect_idx = self._key_paths.index(current_path)
                logger.debug(f"Syncing dropdown to keyfile: {current_path} (index {preselect_idx})")
                self.key_dropdown.set_selected(preselect_idx)
            else:
                # If the key is not in the dropdown, add it and then select it
                if current_path and hasattr(self, '_key_paths') and hasattr(self, 'key_dropdown'):
                    self._key_paths.append(current_path)
                    model = self.key_dropdown.get_model()
                    if model:
                        filename = os.path.basename(current_path)
                        model.append(filename)
                        preselect_idx = len(self._key_paths) - 1
                        logger.debug(f"Added external key to dropdown: {filename} (path: {current_path}, index {preselect_idx})")
                        self.key_dropdown.set_selected(preselect_idx)
                else:
                    logger.debug(f"Could not find keyfile '{current_path}' in dropdown paths")
        except Exception as e:
            logger.debug(f"Failed to sync key dropdown: {e}")

    def _run_initial_validation(self):
        try:
            if hasattr(self, 'nickname_row'):
                self._validate_field_row('name', self.nickname_row)
            if hasattr(self, 'username_row'):
                self._validate_field_row('username', self.username_row)
            if hasattr(self, 'host_row'):
                self._validate_field_row('hostname', self.host_row)
            if hasattr(self, 'port_row'):
                self._validate_field_row('port', self.port_row, context="SSH")
        except Exception:
            pass

    def _focus_row(self, row):
        try:
            if hasattr(self, 'present'):
                self.present()
        except Exception:
            pass
        try:
            widget = row.get_child() if hasattr(row, 'get_child') else row
            if hasattr(widget, 'grab_focus'):
                widget.grab_focus()
        except Exception:
            pass

    def _validate_all_required_for_save(self) -> Optional[Gtk.Widget]:
        """Validate all visible fields; return the first invalid row (or None)."""
        # General
        if hasattr(self, 'nickname_row'):
            res = self._validate_field_row('name', self.nickname_row)
            if not res.is_valid:
                return self.nickname_row
        if hasattr(self, 'username_row'):
            res = self._validate_field_row('username', self.username_row)
            if not res.is_valid:
                return self.username_row
        if hasattr(self, 'host_row'):
            res = self._validate_field_row('hostname', self.host_row)
            if not res.is_valid:
                return self.host_row
        if hasattr(self, 'port_row'):
            res = self._validate_field_row('port', self.port_row, context="SSH")
            if not res.is_valid:
                return self.port_row
        # Local forwarding
        if hasattr(self, 'local_forwarding_enabled') and self.local_forwarding_enabled.get_active():
            if hasattr(self, 'local_port_row') and not self._validate_port_row(self.local_port_row, _("Local Port")):
                return self.local_port_row
            if hasattr(self, 'remote_host_row') and not self._validate_host_row(self.remote_host_row, allow_empty=False):
                return self.remote_host_row
            if hasattr(self, 'remote_port_row') and not self._validate_port_row(self.remote_port_row, _("Target Port")):
                return self.remote_port_row
        # Remote forwarding
        if hasattr(self, 'remote_forwarding_enabled') and self.remote_forwarding_enabled.get_active():
            if hasattr(self, 'remote_bind_host_row') and not self._validate_host_row(self.remote_bind_host_row, allow_empty=True):
                return self.remote_bind_host_row
            if hasattr(self, 'remote_bind_port_row') and not self._validate_port_row(self.remote_bind_port_row, _("Remote port")):
                return self.remote_bind_port_row
            if hasattr(self, 'dest_host_row') and not self._validate_host_row(self.dest_host_row, allow_empty=False):
                return self.dest_host_row
            if hasattr(self, 'dest_port_row') and not self._validate_port_row(self.dest_port_row, _("Destination port")):
                return self.dest_port_row
        # Dynamic forwarding
        if hasattr(self, 'dynamic_forwarding_enabled') and self.dynamic_forwarding_enabled.get_active():
            if hasattr(self, 'dynamic_bind_row') and not self._validate_host_row(self.dynamic_bind_row, allow_empty=True):
                return self.dynamic_bind_row
            if hasattr(self, 'dynamic_port_row') and not self._validate_port_row(self.dynamic_port_row, _("Local Port")):
                return self.dynamic_port_row
        return None
    
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
        # Default to key-based for new connections
        try:
            self.auth_method_row.set_selected(0)
        except Exception:
            pass
        auth_group.add(self.auth_method_row)

        # Key selection mode for key-based auth
        key_select_model = Gtk.StringList()
        key_select_model.append(_("Automatic"))
        key_select_model.append(_("Use a specific key"))
        self.key_select_row = Adw.ComboRow()
        self.key_select_row.set_title(_("Key selection"))
        self.key_select_row.set_model(key_select_model)
        # default: Auto (try all available keys)
        self.key_select_row.set_selected(0)
        self.key_select_row.connect("notify::selected", self.on_key_select_changed)
        auth_group.add(self.key_select_row)
        
        # Keyfile dropdown with detected keys and an inline Browse item
        self.keyfile_row = Adw.ActionRow(title=_("SSH Key"), subtitle=_("Select key file or leave empty for auto-detection"))
        # Build dropdown items from detected keys
        self.key_dropdown = Gtk.DropDown()
        self.key_dropdown.set_hexpand(True)
        # Populate via helper
        self._key_paths = []
        self._populate_detected_keys()

        def _on_key_selected(drop, _param):
            try:
                idx = drop.get_selected()
                if idx < 0 or idx >= len(getattr(self, '_key_paths', [])):
                    return
                path = self._key_paths[idx]
                if path == "__BROWSE__":
                    # Revert selection to previous if any
                    try:
                        drop.set_selected(0)
                    except Exception:
                        pass
                    self.browse_for_key_file()
                elif path:
                    self._selected_keyfile_path = path
                    if hasattr(self.keyfile_row, 'set_subtitle'):
                        self.keyfile_row.set_subtitle(path)
                    
                    # Update passphrase field for the selected key
                    self._update_passphrase_for_key(path)
                    
                    # Auto-select matching certificate if available
                    self._auto_select_matching_certificate(path)
                    
            except Exception:
                pass
        try:
            self.key_dropdown.connect('notify::selected', _on_key_selected)
        except Exception:
            pass

        # Pack dropdown and add to row
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(self.key_dropdown)
        self.keyfile_row.add_suffix(box)
        self.keyfile_row.set_activatable(False)
        auth_group.add(self.keyfile_row)

        # Certificate dropdown for key-based auth with specific key
        self.certificate_row = Adw.ActionRow(title=_("SSH Certificate"), subtitle=_("Select certificate file (optional)"))
        # Build dropdown items from detected certificates
        self.cert_dropdown = Gtk.DropDown()
        self.cert_dropdown.set_hexpand(True)
        # Populate via helper
        self._cert_paths = []
        self._populate_detected_certificates()

        def _on_cert_selected(drop, _param):
            try:
                idx = drop.get_selected()
                if idx < 0 or idx >= len(getattr(self, '_cert_paths', [])):
                    return
                path = self._cert_paths[idx]
                if path == "__BROWSE__":
                    # Revert selection to previous if any
                    try:
                        drop.set_selected(0)
                    except Exception:
                        pass
                    self.browse_for_certificate_file()
                elif path:
                    self._selected_cert_path = path
                    if hasattr(self.certificate_row, 'set_subtitle'):
                        self.certificate_row.set_subtitle(path)
                    
            except Exception:
                pass
        try:
            self.cert_dropdown.connect('notify::selected', _on_cert_selected)
        except Exception:
                pass

        # Pack dropdown and add to row
        cert_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cert_box.append(self.cert_dropdown)
        self.certificate_row.add_suffix(cert_box)
        self.certificate_row.set_activatable(False)
        auth_group.add(self.certificate_row)

        # Initialize key UI sensitivity for new connections
        try:
            # Ensure visibility/sensitivity matches defaults
            self.on_auth_method_changed(self.auth_method_row, None)
            self.on_key_select_changed(self.key_select_row, None)
        except Exception:
            pass
        
        # Key Passphrase
        self.key_passphrase_row = Adw.PasswordEntryRow(title=_("Key Passphrase"))
        self.key_passphrase_row.set_show_apply_button(False)
        auth_group.add(self.key_passphrase_row)
        
        # Password
        self.password_row = Adw.PasswordEntryRow(title=_("Password"))
        self.password_row.set_show_apply_button(False)
        # Always visible; optional for key-based auth
        self.password_row.set_visible(True)
        auth_group.add(self.password_row)

        # Disable pubkey authentication toggle for password auth
        self.pubkey_auth_row = Adw.SwitchRow()
        self.pubkey_auth_row.set_title(_("Disable public key authentication (force password only)"))
        self.pubkey_auth_row.set_active(False)
        auth_group.add(self.pubkey_auth_row)
        

        
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
        
        # Button box for actions
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        button_box.set_halign(Gtk.Align.START)
        button_box.set_margin_top(6)
        button_box.set_margin_bottom(6)
        
        # Add rule button
        self.add_rule_button = Gtk.Button(label=_("Add Rule"))
        self.add_rule_button.set_icon_name("list-add-symbolic")
        self.add_rule_button.set_tooltip_text(_("Add a new port forwarding rule"))
        self.add_rule_button.connect("clicked", self.on_add_forwarding_rule_clicked)
        button_box.append(self.add_rule_button)
        
        # Port info button
        self.port_info_button = Gtk.Button(label=_("View Port Info"))
        self.port_info_button.set_icon_name("network-transmit-receive-symbolic")
        self.port_info_button.set_tooltip_text(_("View information about currently used ports and potential conflicts"))
        self.port_info_button.connect("clicked", self.on_view_port_info_clicked)
        self.port_info_button.add_css_class("flat")
        button_box.append(self.port_info_button)
        
        rules_group.add(button_box)
        
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
        
        # Commands Group (LocalCommand / RemoteCommand)
        commands_group = Adw.PreferencesGroup(
            title=_("Connection Commands"),
            description=_(
                "Run a command automatically on connect.\n\n"
                "• Local Command: Runs on your machine after connection (requires PermitLocalCommand).\n"
                "• Remote Command: Runs on the remote host (uses RequestTTY for interactive shell)."
            )
        )
        self.local_command_row = Adw.EntryRow(title=_("Local Command"))
        try:
            self.local_command_row.set_subtitle(_("Executed locally after connect"))
        except Exception:
            pass
        self.remote_command_row = Adw.EntryRow(title=_("Remote Command"))
        try:
            self.remote_command_row.set_subtitle(_("Executed on remote; TTY requested for interactivity"))
        except Exception:
            pass
        commands_group.add(self.local_command_row)
        commands_group.add(self.remote_command_row)

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
        
        # Return groups for PreferencesPage: Port forwarding first, commands, about, X11 last
        return [rules_group, commands_group, about_group, x11_group]
        
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
        """Open file chooser to browse for SSH key file (Gtk.FileChooserDialog)."""
        try:
            dialog = Gtk.FileChooserDialog(
                title=_("Select SSH Key File"),
                action=Gtk.FileChooserAction.OPEN,
            )
            # Parent must be a Gtk.Window; PreferencesDialog is not one. Try to set if available
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

            # No filters: list all files in ~/.ssh

            dialog.connect("response", self.on_key_file_selected)
            dialog.show()
        except Exception as e:
            logger.error(f"Failed to open key file chooser: {e}")

    def browse_for_certificate_file(self):
        """Open file chooser to browse for SSH certificate file."""
        try:
            dialog = Gtk.FileChooserDialog(
                title=_("Select SSH Certificate File"),
                action=Gtk.FileChooserAction.OPEN,
            )
            # Parent must be a Gtk.Window; PreferencesDialog is not one. Try to set if available
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

            # Add filter for certificate files
            cert_filter = Gtk.FileFilter()
            cert_filter.set_name(_("SSH Certificate Files"))
            cert_filter.add_pattern("*-cert.pub")
            cert_filter.add_pattern("*.pub")
            dialog.add_filter(cert_filter)

            # Add filter for all files
            all_filter = Gtk.FileFilter()
            all_filter.set_name(_("All Files"))
            all_filter.add_pattern("*")
            dialog.add_filter(all_filter)

            dialog.connect("response", self.on_certificate_file_selected)
            dialog.show()
        except Exception as e:
            logger.error(f"Failed to open certificate file chooser: {e}")


    
    def on_key_file_selected(self, dialog, response):
        """Handle selected key file from file chooser"""
        if response == Gtk.ResponseType.ACCEPT:
            key_file = dialog.get_file()
            if key_file:
                key_path = key_file.get_path()
                self.keyfile_row.set_subtitle(key_path)
                
                # Add the browsed key to the dropdown if it's not already there
                if hasattr(self, '_key_paths') and key_path not in self._key_paths:
                    self._key_paths.append(key_path)
                    # Update the dropdown model with just the filename
                    if hasattr(self, 'key_dropdown'):
                        model = self.key_dropdown.get_model()
                        if model:
                            filename = os.path.basename(key_path)
                            model.append(filename)
                
                # Set the selected keyfile path
                self._selected_keyfile_path = key_path
                
                # Sync the dropdown to select the browsed key
                self._sync_key_dropdown_with_current_keyfile()
        dialog.destroy()
    
    def on_certificate_file_selected(self, dialog, response):
        """Handle selected certificate file from file chooser"""
        if response == Gtk.ResponseType.ACCEPT:
            cert_file = dialog.get_file()
            if cert_file:
                cert_path = cert_file.get_path()
                self.certificate_row.set_subtitle(cert_path)
                
                # Add the browsed certificate to the dropdown if it's not already there
                if hasattr(self, '_cert_paths') and cert_path not in self._cert_paths:
                    self._cert_paths.append(cert_path)
                    # Update the dropdown model with just the filename
                    if hasattr(self, 'cert_dropdown'):
                        model = self.cert_dropdown.get_model()
                        if model:
                            filename = os.path.basename(cert_path)
                            model.append(filename)
                
                # Set the selected certificate path
                self._selected_cert_path = cert_path
                
                # Sync the dropdown to select the browsed certificate
                self._sync_cert_dropdown_with_current_cert()
        dialog.destroy()
    
    def _sync_cert_dropdown_with_current_cert(self):
        """Sync the certificate dropdown selection with the current certificate path"""
        try:
            if not hasattr(self, 'cert_dropdown') or not hasattr(self, '_cert_paths'):
                return
                
            # Get current certificate path
            current_path = None
            if hasattr(self, '_selected_cert_path') and self._selected_cert_path:
                current_path = self._selected_cert_path
            elif hasattr(self.certificate_row, 'get_subtitle'):
                current_path = self.certificate_row.get_subtitle() or None
            if (not current_path) and hasattr(self, 'connection') and self.connection:
                current_path = getattr(self.connection, 'certificate', None)
                
            # Find matching index in dropdown
            if current_path and current_path in self._cert_paths:
                preselect_idx = self._cert_paths.index(current_path)
                logger.debug(f"Syncing certificate dropdown to: {current_path} (index {preselect_idx})")
                self.cert_dropdown.set_selected(preselect_idx)
            else:
                # If the certificate is not in the dropdown, add it and then select it
                if current_path and hasattr(self, '_cert_paths') and hasattr(self, 'cert_dropdown'):
                    self._cert_paths.append(current_path)
                    model = self.cert_dropdown.get_model()
                    if model:
                        filename = os.path.basename(current_path)
                        model.append(filename)
                        preselect_idx = len(self._cert_paths) - 1
                        logger.debug(f"Added external certificate to dropdown: {filename} (path: {current_path}, index {preselect_idx})")
                        self.cert_dropdown.set_selected(preselect_idx)
                else:
                    logger.debug(f"Could not find certificate '{current_path}' in dropdown paths")
        except Exception as e:
            logger.debug(f"Failed to sync certificate dropdown: {e}")
    
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
    
    def on_view_port_info_clicked(self, button):
        """Handle view port info button click"""
        self._show_port_info_dialog()

    def _open_rule_editor(self, existing_rule=None):
        """Open an Adw.Window to add/edit a forwarding rule."""
        # Create Adw.Window
        dialog = Adw.Window()
        dialog.set_title(_("Port Forwarding Rule Editor"))
        dialog.set_default_size(500, -1)  # 500px width, auto height
        dialog.set_modal(True)
        dialog.set_transient_for(self)

        # Create content box
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)

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
        
        # Create header bar with buttons
        header_bar = Adw.HeaderBar()
        header_bar.set_show_end_title_buttons(True)
        
        # Add buttons to header bar
        cancel_button = Gtk.Button(label=_("Cancel"))
        cancel_button.add_css_class("flat")
        header_bar.pack_start(cancel_button)
        
        save_button = Gtk.Button(label=_("Save"))
        save_button.add_css_class("suggested-action")
        header_bar.pack_end(save_button)
        
        # Create main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header_bar)
        main_box.append(box)
        
        # Set content for Adw.Window
        dialog.set_content(main_box)

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

        # Handle button clicks
        def _on_cancel_clicked(button):
            dialog.destroy()
        
        def _on_save_clicked(button):
            self._save_rule_from_editor(existing_rule, type_row, listen_addr_row, listen_port_row, remote_host_row, remote_port_row)
            dialog.destroy()
        
        cancel_button.connect('clicked', _on_cancel_clicked)
        save_button.connect('clicked', _on_save_clicked)
        
        # Show the window
        dialog.present()

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
        
        # Check for port conflicts (for local and dynamic forwarding)
        if rtype in ['local', 'dynamic']:
            try:
                port_checker = get_port_checker()
                conflicts = port_checker.get_port_conflicts([listen_port], listen_addr)
                
                if conflicts:
                    port, port_info = conflicts[0]
                    conflict_msg = _("Port {port} is already in use").format(port=port)
                    if port_info.process_name:
                        conflict_msg += _(" by {process} (PID: {pid})").format(
                            process=port_info.process_name, 
                            pid=port_info.pid
                        )
                    
                    # Suggest alternative port
                    alt_port = port_checker.find_available_port(listen_port, listen_addr)
                    if alt_port:
                        conflict_msg += _("\n\nSuggested alternative: port {alt_port}").format(alt_port=alt_port)
                    
                    # Show error dialog with conflict information
                    self.show_error(conflict_msg)
                    return
                    
            except Exception as e:
                logger.debug(f"Could not check port conflict for {listen_port}: {e}")
                # Continue without port checking if there's an error
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
    
    def _show_port_info_dialog(self):
        """Show a window with current port information"""
        # Create Adw.Window
        parent_win = self.get_transient_for() if hasattr(self, 'get_transient_for') else None
        dialog = Adw.Window()
        dialog.set_title(_("Port Information"))
        dialog.set_default_size(600, 400)
        dialog.set_modal(True)
        if parent_win:
            dialog.set_transient_for(parent_win)
        
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, 
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12
        )
        
        # Header with refresh button
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_label = Gtk.Label()
        header_label.set_markup(f"<b>{_('Currently Listening Ports')}</b>")
        header_label.set_halign(Gtk.Align.START)
        header_label.set_hexpand(True)
        header_box.append(header_label)
        
        refresh_button = Gtk.Button()
        refresh_button.set_icon_name("view-refresh-symbolic")
        refresh_button.set_tooltip_text(_("Refresh port information"))
        header_box.append(refresh_button)
        
        box.append(header_box)
        
        # Scrolled window for port list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        
        # Port list
        port_list = Gtk.ListBox()
        port_list.set_selection_mode(Gtk.SelectionMode.NONE)
        port_list.add_css_class("boxed-list")
        scrolled.set_child(port_list)
        
        box.append(scrolled)
        
        def refresh_port_info():
            """Refresh the port information display"""
            # Clear existing items
            while row := port_list.get_first_child():
                port_list.remove(row)
            
            try:
                port_checker = get_port_checker()
                ports = port_checker.get_listening_ports(refresh=True)
                
                if not ports:
                    # Show empty state
                    empty_row = Adw.ActionRow()
                    empty_row.set_title(_("No listening ports found"))
                    empty_row.set_subtitle(_("All ports appear to be available"))
                    port_list.append(empty_row)
                    return
                
                # Sort ports by port number
                ports.sort(key=lambda p: p.port)
                
                for port_info in ports:
                    row = Adw.ActionRow()
                    
                    # Title: Port and protocol
                    title = f"{_('Port')} {port_info.port}/{port_info.protocol.upper()}"
                    if port_info.address != "0.0.0.0":
                        title += f" ({port_info.address})"
                    row.set_title(title)
                    
                    # Subtitle: Process information
                    if port_info.process_name and port_info.pid:
                        subtitle = f"{port_info.process_name} (PID: {port_info.pid})"
                    elif port_info.process_name:
                        subtitle = port_info.process_name
                    elif port_info.pid:
                        subtitle = f"PID: {port_info.pid}"
                    else:
                        subtitle = _("Unknown process")
                    
                    row.set_subtitle(subtitle)
                    
                    # Add icon based on port type
                    if port_info.port < 1024:
                        icon = Gtk.Image.new_from_icon_name("security-high-symbolic")
                        icon.set_tooltip_text(_("System port (requires root)"))
                    else:
                        icon = Gtk.Image.new_from_icon_name("network-transmit-receive-symbolic")
                    
                    row.add_prefix(icon)
                    port_list.append(row)
                    
            except Exception as e:
                logger.error(f"Error refreshing port info: {e}")
                error_row = Adw.ActionRow()
                error_row.set_title(_("Error loading port information"))
                error_row.set_subtitle(str(e))
                port_list.append(error_row)
        
        # Connect refresh button
        refresh_button.connect("clicked", lambda *_: refresh_port_info())
        
        # Create header bar with close button
        header_bar = Adw.HeaderBar()
        header_bar.set_show_end_title_buttons(True)
        
        close_button = Gtk.Button(label=_("Close"))
        close_button.add_css_class("flat")
        header_bar.pack_end(close_button)
        
        # Create main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header_bar)
        main_box.append(box)
        
        # Set content for Adw.Window
        dialog.set_content(main_box)
        
        # Load initial data
        refresh_port_info()
        
        # Handle close button
        def _on_close_clicked(button):
            dialog.destroy()
        
        close_button.connect('clicked', _on_close_clicked)
        
        # Show the window
        dialog.present()

    def _autosave_forwarding_changes(self):
        """Disabled autosave to avoid log floods; saving occurs on dialog Save."""
        return
    
    def on_cancel_clicked(self, button):
        """Handle cancel button click"""
        self.close()
    
    def on_save_clicked(self, *_args):
        """Handle save button click or dialog save response"""
        # Block save and focus the first invalid field if any inline validation fails
        invalid_row = None
        try:
            invalid_row = self._validate_all_required_for_save()
        except Exception:
            invalid_row = None
        if invalid_row is not None:
            try:
                self._focus_row(invalid_row)
            except Exception:
                pass
            return
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
        
        # Persist exactly what is in the editor list (enabled rules only) - no sanitization
        forwarding_rules = [dict(r) for r in self.forwarding_rules if r.get('enabled', True)]
        try:
            logger.info(
                "ConnectionDialog save: %d forwarding rules collected, keyfile: '%s'",
                len(forwarding_rules or []), keyfile_value
            )
            logger.debug("Forwarding rules: %s", forwarding_rules)
        except Exception:
            pass
        
        # Detect if password text was changed by user during this edit session
        try:
            password_changed = (self.password_row.get_text() != getattr(self, '_orig_password', None))
        except Exception:
            password_changed = False

        # Resolve keyfile from dropdown/browse/subtitle/existing
        try:
            keyfile_value = ''
            if hasattr(self, 'key_dropdown') and hasattr(self, '_key_paths'):
                sel = self.key_dropdown.get_selected()
                if 0 <= sel < len(self._key_paths):
                    pth = self._key_paths[sel]
                    if pth and pth != '__BROWSE__':
                        keyfile_value = pth
            if (not keyfile_value) and hasattr(self, '_selected_keyfile_path') and self._selected_keyfile_path:
                keyfile_value = str(self._selected_keyfile_path)
            if (not keyfile_value) and hasattr(self.keyfile_row, 'get_subtitle'):
                keyfile_value = self.keyfile_row.get_subtitle() or ''
            if (not keyfile_value) and self.is_editing and hasattr(self, 'connection') and self.connection:
                keyfile_value = str(getattr(self.connection, 'keyfile', '') or '')
        except Exception:
            keyfile_value = ''

        # Verify passphrase before proceeding with save
        key_passphrase = self.key_passphrase_row.get_text()
        
        if keyfile_value and keyfile_value != "Select key file" and key_passphrase:
            # Verify the passphrase matches the private key
            if not self.validator.verify_key_passphrase(keyfile_value, key_passphrase):
                self.show_error(_("The passphrase you entered is invalid for this key. Please try again."))
                return

        # Store key passphrase in secret storage if provided
        if keyfile_value and keyfile_value != "Select key file":
            try:
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    if hasattr(self.connection_manager, 'store_key_passphrase'):
                        if key_passphrase:
                            # Store new or modified passphrase (already verified above)
                            self.connection_manager.store_key_passphrase(keyfile_value, key_passphrase)
                        elif hasattr(self.connection_manager, 'delete_key_passphrase'):
                            # User cleared the field - remove stored passphrase
                            self.connection_manager.delete_key_passphrase(keyfile_value)
            except Exception as e:
                logger.warning(f"Failed to store/delete key passphrase: {e}")

        # Get certificate path
        certificate_value = ''
        try:
            if hasattr(self, 'cert_dropdown') and hasattr(self, '_cert_paths'):
                sel = self.cert_dropdown.get_selected()
                if 0 <= sel < len(self._cert_paths):
                    pth = self._cert_paths[sel]
                    if pth and pth != '__BROWSE__':
                        certificate_value = pth
            if (not certificate_value) and hasattr(self, '_selected_cert_path') and self._selected_cert_path:
                certificate_value = str(self._selected_cert_path)
            if (not certificate_value) and hasattr(self.certificate_row, 'get_subtitle'):
                subtitle_value = self.certificate_row.get_subtitle() or ''
                # Filter out placeholder text
                if subtitle_value and not subtitle_value.lower().startswith('select certificate'):
                    certificate_value = subtitle_value
            if (not certificate_value) and self.is_editing and hasattr(self, 'connection') and self.connection:
                conn_cert_value = str(getattr(self.connection, 'certificate', '') or '')
                # Filter out placeholder text
                if conn_cert_value and not conn_cert_value.lower().startswith('select certificate'):
                    certificate_value = conn_cert_value
        except Exception:
            certificate_value = ''


        # Get extra SSH config from advanced tab
        extra_ssh_config = ''
        if hasattr(self, 'advanced_tab'):
            try:
                extra_ssh_config = self.advanced_tab.get_extra_ssh_config()
                logger.debug(f"Retrieved extra SSH config from advanced tab: {extra_ssh_config}")
            except Exception as e:
                logger.error(f"Error getting extra SSH config from advanced tab: {e}")
                extra_ssh_config = ''

        # Gather connection data
        connection_data = {
            'nickname': self.nickname_row.get_text().strip(),
            'host': self.host_row.get_text().strip(),
            'username': self.username_row.get_text().strip(),
            'port': int(self.port_row.get_text().strip() or '22'),
            'auth_method': self.auth_method_row.get_selected(),
            'keyfile': keyfile_value,
            'certificate': certificate_value,
            'key_select_mode': (self.key_select_row.get_selected() if hasattr(self, 'key_select_row') else 0),
            'key_passphrase': self.key_passphrase_row.get_text(),
            'password': self.password_row.get_text(),
            'x11_forwarding': self.x11_row.get_active(),
            'pubkey_auth_no': self.pubkey_auth_row.get_active(),

            'forwarding_rules': forwarding_rules,
            'local_command': (self.local_command_row.get_text() if hasattr(self, 'local_command_row') else ''),
            'remote_command': (self.remote_command_row.get_text() if hasattr(self, 'remote_command_row') else ''),
            'extra_ssh_config': extra_ssh_config,
            'password_changed': bool(password_changed),
        }
        
        # Update the connection object locally when editing (do not persist here; window handles persistence)
        if self.is_editing and self.connection:
            try:
                self.connection.data.update(connection_data)
            except Exception:
                pass
            # Explicitly update forwarding rules to ensure they're fresh
            self.connection.forwarding_rules = forwarding_rules
            
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


    
    def on_forwarding_toggled(self, switch, param, settings_box):
        """Handle toggling of port forwarding settings visibility and state"""
        is_active = switch.get_active()
        settings_box.set_visible(is_active)
        # Run inline validation on fields within this section when enabled
        try:
            if is_active:
                if switch == self.local_forwarding_enabled:
                    if hasattr(self, 'local_port_row'):
                        self._validate_port_row(self.local_port_row, _("Local Port"))
                    if hasattr(self, 'remote_host_row'):
                        self._validate_host_row(self.remote_host_row, allow_empty=False)
                    if hasattr(self, 'remote_port_row'):
                        self._validate_port_row(self.remote_port_row, _("Target Port"))
                elif switch == self.remote_forwarding_enabled:
                    if hasattr(self, 'remote_bind_host_row'):
                        self._validate_host_row(self.remote_bind_host_row, allow_empty=True)
                    if hasattr(self, 'remote_bind_port_row'):
                        self._validate_port_row(self.remote_bind_port_row, _("Remote port"))
                    if hasattr(self, 'dest_host_row'):
                        self._validate_host_row(self.dest_host_row, allow_empty=False)
                    if hasattr(self, 'dest_port_row'):
                        self._validate_port_row(self.dest_port_row, _("Destination port"))
                elif switch == self.dynamic_forwarding_enabled:
                    if hasattr(self, 'dynamic_bind_row'):
                        self._validate_host_row(self.dynamic_bind_row, allow_empty=True)
                    if hasattr(self, 'dynamic_port_row'):
                        self._validate_port_row(self.dynamic_port_row, _("Local Port"))
        except Exception:
            pass
        
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
        try:
            if hasattr(self, 'present'):
                self.present()
        except Exception:
            pass
        
        # Use the parent window as the transient parent for the error dialog
        parent_window = self.parent_window if hasattr(self, 'parent_window') else None
        dialog = Adw.MessageDialog.new(
            parent_window,
            _("Error"),
            message
        )
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present()

    def _update_passphrase_for_key(self, key_path):
        """Update the passphrase field when a different key is selected"""
        try:
            if not key_path or not hasattr(self, 'key_passphrase_row'):
                return
            
            # Clear the passphrase field first
            self.key_passphrase_row.set_text("")
            
            # Try to load passphrase from secure storage for the selected key
            if hasattr(self, 'connection_manager') and self.connection_manager:
                stored_passphrase = self.connection_manager.get_key_passphrase(key_path)
                if stored_passphrase:
                    self.key_passphrase_row.set_text(stored_passphrase)
                    logger.debug(f"Loaded passphrase for key: {key_path}")
                else:
                    logger.debug(f"No stored passphrase for key: {key_path}")
            else:
                logger.debug(f"No connection manager available for key: {key_path}")
        except Exception as e:
            logger.debug(f"Failed to update passphrase for key {key_path}: {e}")
    
    def _refresh_connection_data_from_ssh_config(self):
        """Refresh connection data from the updated SSH config file"""
        try:
            if not self.is_editing or not self.connection:
                return
            
            # Reload the connection manager to get fresh data from SSH config
            if hasattr(self, 'connection_manager') and self.connection_manager:
                self.connection_manager.load_ssh_config()
                
                # Find the updated connection by nickname
                updated_connection = self.connection_manager.get_connection_by_nickname(self.connection.nickname)
                if updated_connection:
                    self.connection = updated_connection
                    self.load_connection_data(self.connection) # Reload UI with new data
                    logger.debug(f"Refreshed connection dialog data for '{self.connection.nickname}'")
                else:
                    logger.warning(f"Could not find updated connection '{self.connection.nickname}' after SSH config reload")
        except Exception as e:
            logger.error(f"Error refreshing connection data from SSH config: {e}", exc_info=True)
    
