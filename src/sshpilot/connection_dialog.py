"""
Connection Dialog for sshPilot
Dialog for adding/editing SSH connections
"""

import os
import logging
import re
import subprocess
import threading
import types
from typing import Optional, Dict, Any

try:
    from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gdk, Pango, PangoFT2
except (ImportError, AttributeError):  # pragma: no cover - used in tests without GTK
    class _DummyGIMeta(type):
        def __getattr__(cls, name):
            value = _DummyGIMeta(name, (object,), {})
            setattr(cls, name, value)
            return value

        def __call__(cls, *args, **kwargs):
            return object()

    class _DummyGIModule(metaclass=_DummyGIMeta):
        pass

    Gtk = _DummyGIModule
    Adw = _DummyGIModule
    Gio = _DummyGIModule
    GObject = _DummyGIModule
    Gdk = _DummyGIModule
    Pango = _DummyGIModule
    PangoFT2 = _DummyGIModule

    class _DummyGLib(_DummyGIModule):
        @staticmethod
        def idle_add(*args, **kwargs):
            return None

    GLib = _DummyGLib
    GObject.SignalFlags = types.SimpleNamespace(RUN_FIRST=None)
from .platform_utils import is_macos, get_ssh_dir, get_config_dir
from .shortcut_utils import install_esc_to_close
from .ssh_key_fingerprint import (
    _fingerprint_for_path,
    _fingerprint_for_pub_line,
)
from .ssh_connection_validator import (  # SSHConnectionValidator also re-exported for tests/back-compat
    SSHConnectionValidator,
    ValidationResult,
)
from .path_list import PathList
from .connection_dialog_validation import ConnectionDialogValidationMixin
from .connection_dialog_field_helpers import ConnectionDialogFieldHelpersMixin
from .connection_dialog_port_forwarding import ConnectionDialogPortForwardingMixin
from .plugins.registry import protocol_registry

from gettext import gettext as _

logger = logging.getLogger(__name__)


class _AuthMethodToggleFallback(Gtk.Box):
    """Segmented-control fallback for Adw.ToggleGroup (libadwaita < 1.7).

    Adw.ToggleGroup/Adw.Toggle only exist in libadwaita 1.7+, so on older
    runtimes (e.g. Ubuntu 24.04 ships 1.5) the connection dialog would crash
    while building the auth-method selector. This implements the subset of the
    AdwToggleGroup API the dialog relies on: an integer ``active`` property
    (emits ``notify::active``), plus ``get_active``/``set_active``, built from
    linked Gtk.ToggleButtons.
    """

    __gtype_name__ = "SshPilotAuthMethodToggleFallback"
    active = GObject.Property(type=int, default=0)

    def __init__(self, labels):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.add_css_class("linked")
        self.set_hexpand(True)
        self._buttons = []
        self._syncing = False
        for index, label in enumerate(labels):
            button = Gtk.ToggleButton(label=label)
            button.set_hexpand(True)
            if self._buttons:
                button.set_group(self._buttons[0])
            button.connect("toggled", self._on_toggled, index)
            self.append(button)
            self._buttons.append(button)
        if self._buttons:
            self._syncing = True
            self._buttons[0].set_active(True)
            self._syncing = False

    def _on_toggled(self, button, index):
        if self._syncing or not button.get_active():
            return
        self.set_property("active", index)  # emits notify::active when changed

    def get_active(self):
        return int(self.get_property("active"))

    def set_active(self, index):
        if 0 <= index < len(self._buttons):
            self._syncing = True
            try:
                self._buttons[index].set_active(True)
            finally:
                self._syncing = False
            self.set_property("active", index)


_TOGGLE_SUGGESTED_CSS_REGISTERED = False


def _ensure_toggle_suggested_css():
    """Register accent styling for the active item in connection-dialog toggle groups."""
    global _TOGGLE_SUGGESTED_CSS_REGISTERED
    if _TOGGLE_SUGGESTED_CSS_REGISTERED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(b"""
    toggle-group.toggle-suggested,
    inline-view-switcher.toggle-suggested toggle-group {
        --active-toggle-bg-color: @accent_bg_color;
        --active-toggle-fg-color: @accent_fg_color;
    }
    .linked-toggle-suggested button:checked {
        background-color: @accent_bg_color;
        color: @accent_fg_color;
    }
    """)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _TOGGLE_SUGGESTED_CSS_REGISTERED = True


def _build_expanding_toggle_group(labels):
    """Build a full-width Adw.ToggleGroup, or Gtk toggle fallback on older libadwaita."""
    _ensure_toggle_suggested_css()
    if hasattr(Adw, "ToggleGroup"):
        toggle = Adw.ToggleGroup()
        toggle.add_css_class("toggle-suggested")
        toggle.set_valign(Gtk.Align.CENTER)
        toggle.set_hexpand(True)
        try:
            toggle.set_homogeneous(True)
        except Exception:
            pass
        try:
            for label in labels:
                toggle.add(Adw.Toggle(label=label))
        except Exception:
            logger.debug("Failed to build ToggleGroup", exc_info=True)
    else:
        toggle = _AuthMethodToggleFallback(labels)
        toggle.add_css_class("linked-toggle-suggested")
        toggle.set_valign(Gtk.Align.CENTER)
        toggle.set_hexpand(True)
    try:
        toggle.set_active(0)
    except Exception:
        pass
    return toggle


def _set_action_row_child(row, widget):
    """Place *widget* as the sole content of an ActionRow when supported."""
    try:
        row.set_child(widget)
    except Exception:
        widget.set_hexpand(True)
        row.add_prefix(widget)


class SSHConfigEntry(GObject.Object):
    """Data model for SSH config entries"""
    
    def __init__(self, key="", value=""):
        super().__init__()
        self.key = key
        self.value = value

class SSHConfigAdvancedTab(Gtk.Box):
    """Advanced SSH Configuration Tab for GTK 4"""

    def __init__(self, connection_manager, parent_dialog=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self.connection_manager = connection_manager
        self.parent_dialog = parent_dialog
        
        # SSH config options list
        self.ssh_options = [
            'AddKeysToAgent', 'AddressFamily', 'BatchMode', 'BindAddress', 'BindInterface',
            'CanonicalDomains', 'CanonicalizeFallbackLocal', 'CanonicalizeHostname',
            'CanonicalizeMaxDots', 'CanonicalizePermittedCNAMEs', 'CASignatureAlgorithms',
            'CertificateFile', 'CheckHostIP', 'Ciphers', 'ClearAllForwardings', 'Compression',
            'ConnectionAttempts', 'ConnectTimeout', 'ControlMaster', 'ControlPath',
            'ControlPersist', 'DynamicForward', 'EnableSSHKeysign', 'EscapeChar',
            'ExitOnForwardFailure', 'FingerprintHash', 'ForwardX11',
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
            'PreferredAuthentications', 'ProxyCommand', 'ProxyUseFdpass',
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
        title = Gtk.Label(label=_("Advanced SSH Configuration"))
        title.set_markup(_("<b>Advanced SSH Configuration</b>"))
        title.set_halign(Gtk.Align.START)
        
        subtitle = Gtk.Label(label=_("Add custom SSH configuration options"))
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        
        header.append(title)
        header.append(subtitle)
        self.append(header)
        
        # Add button (positioned at the top)
        from sshpilot import icon_utils
        self.add_button = Gtk.Button(label=_("Add SSH Option"))
        icon_utils.set_button_icon(self.add_button, "list-add-symbolic")
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
        
        key_header = Gtk.Label(label=_("SSH Option"))
        key_header.set_markup(_("<b>Keyword</b>"))
        key_header.set_halign(Gtk.Align.START)
        key_header.set_hexpand(True)
        
        value_header = Gtk.Label(label=_("Value"))
        value_header.set_markup(_("<b>Value</b>"))
        value_header.set_halign(Gtk.Align.START)
        value_header.set_hexpand(True)
        
        header_grid.attach(key_header, 0, 0, 1, 1)
        header_grid.attach(value_header, 1, 0, 1, 1)
        
        self.content_box.append(header_grid)
        
        # Empty state label
        self.empty_label = Gtk.Label(label=_("No custom SSH options configured.\nClick 'Add' button to get started."))
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_justify(Gtk.Justification.CENTER)
        self.empty_label.set_margin_top(24)
        self.empty_label.set_margin_bottom(24)
        self.content_box.append(self.empty_label)
        
        self.append(scrolled)

    def _build_preview_config_data(self, connection):
        """Assemble the config dict for this host's own block (used by the viewer)."""
        return {
            'nickname': getattr(connection, 'nickname', 'your-host-name'),
            'hostname': getattr(connection, 'hostname', getattr(connection, 'host', '')),
            'username': getattr(connection, 'username', ''),
            'port': getattr(connection, 'port', 22),
            'auth_method': getattr(connection, 'auth_method', 0),
            'key_select_mode': getattr(connection, 'key_select_mode', 0),
            'keyfile': getattr(connection, 'keyfile', ''),
            'identity_files': getattr(connection, 'identity_files', None) or [],
            'certificate': getattr(connection, 'certificate', ''),
            'x11_forwarding': getattr(connection, 'x11_forwarding', False),
            'local_command': getattr(connection, 'local_command', ''),
            'remote_command': getattr(connection, 'remote_command', ''),
            'forwarding_rules': getattr(connection, 'forwarding_rules', []),
            'extra_ssh_config': self.get_extra_ssh_config(),
        }

    def _preview_data_from_form(self, dialog):
        """Core resolution-relevant fields read straight from the form.

        Used when there is no saved connection object yet (creating a new one),
        so the viewer can still show how global rules affect the host in progress.
        """
        def _txt(name):
            row = getattr(dialog, name, None)
            try:
                return row.get_text().strip() if row is not None else ''
            except Exception:
                return ''
        port = _txt('port_row')
        return {
            'nickname': _txt('nickname_row') or 'new-host',
            'hostname': _txt('hostname_row'),
            'username': _txt('username_row'),
            'port': int(port) if port.isdigit() else 22,
            'auth_method': 0,
            'key_select_mode': 0,
            'forwarding_rules': [],
            'extra_ssh_config': self.get_extra_ssh_config(),
        }

    def _on_view_effective_clicked(self, _button):
        """Open the two-pane effective-config viewer for the current host."""
        try:
            parent_dialog = self.get_ancestor(Adw.Window)
            if not (self.connection_manager and parent_dialog):
                return
            connection = getattr(parent_dialog, 'connection', None)
            if connection is not None:
                config_data = self._build_preview_config_data(connection)
                try:
                    root_config = connection._resolve_config_override_path()
                except Exception:
                    root_config = None
                is_new = not bool(getattr(parent_dialog, 'is_editing', False))
            else:
                # Brand-new connection: no object yet — read the form.
                config_data = self._preview_data_from_form(parent_dialog)
                root_config = getattr(self.connection_manager, 'ssh_config_path', None) or None
                is_new = True

            own_block = self.connection_manager.format_ssh_config_entry(config_data)
            host = config_data.get('nickname') or ''

            from .effective_config_dialog import EffectiveConfigDialog
            dialog = EffectiveConfigDialog(
                parent_dialog, host=host, own_block=own_block,
                root_config=root_config, is_new=is_new)
            dialog.present()
        except Exception:
            logger.debug("Failed to open effective config viewer", exc_info=True)

        

        
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
        
        # Enable search functionality, matching anywhere in the keyword
        # (the default search mode only matches the prefix).
        key_dropdown.set_enable_search(True)
        try:
            key_dropdown.set_search_match_mode(Gtk.StringFilterMatchMode.SUBSTRING)
        except AttributeError:  # GTK < 4.12
            pass
        
        # Value entry
        value_entry = Gtk.Entry()
        value_entry.set_placeholder_text(_("Enter value..."))
        value_entry.set_hexpand(True)
        value_entry.connect("activate", self.on_value_entry_activate, row_grid)
        
        # Remove button
        from sshpilot import icon_utils
        remove_button = Gtk.Button()
        icon_utils.set_button_icon(remove_button, "user-trash-symbolic")
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
        """No-op retained for existing callers.

        The live "Generated SSH Config" dump was replaced by the on-demand
        effective-config viewer opened from the dialog's "Verify Configuration"
        button; nothing needs to be refreshed as fields change.
        """
        return

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
        config_lines: list[str] = []
        host_rows = []

        for row_grid in self.config_entries.copy():
            try:
                key = self._get_dropdown_selected_text(row_grid.key_dropdown)
                value = row_grid.value_entry.get_text().strip()
            except Exception:
                continue

            if not key or not value or key == "Select SSH option...":
                continue

            if key.lower() == "host":
                host_rows.append(row_grid)
                continue

            config_lines.append(f"{key} {value}")

        if host_rows:
            def _remove_rows():
                for row in host_rows:
                    try:
                        self.on_remove_option(None, row)
                    except Exception:
                        pass
                self.update_config_preview()
                return False

            GLib.idle_add(_remove_rows)

        return "\n".join(config_lines)

    def _update_parent_connection(self):
        """Update the parent connection object with current advanced tab data"""
        try:
            parent_dialog = self.get_ancestor(Adw.Window)
            if parent_dialog and hasattr(parent_dialog, 'connection') and parent_dialog.connection:
                extra_config = self.get_extra_ssh_config()
                parent_dialog.connection.extra_ssh_config = extra_config
                if hasattr(parent_dialog.connection, 'data'):
                    parent_dialog.connection.data['extra_ssh_config'] = extra_config
                logger.debug(
                    f"Updated parent connection with extra SSH config: {extra_config}"
                )
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
                key = parts[0].strip()
                value = parts[1].strip() if len(parts) == 2 else "yes"
                if key.lower() == 'host':
                    continue
                entries.append((key, value))

        logger.debug(f"Parsed entries: {entries}")
        self.set_config_entries(entries)
        # Update preview after loading data
        self.update_config_preview()
        # Update the parent connection object if we're editing
        self._update_parent_connection()


_KEY_BADGE_CSS_REGISTERED = False


def _ensure_key_badge_css():
    """The .key-order-badge / .key-type-pill styles now live in the bundled
    style.css (loaded once at startup); nothing to register here."""
    return


def _accent_hex():
    """Current libadwaita accent colour as #RRGGBB (theme-aware)."""
    try:
        sm = Adw.StyleManager.get_default()
        rgba = sm.get_accent_color().to_standalone_rgba(sm.get_dark())
        return "#%02x%02x%02x" % (
            int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255))
    except Exception:
        return "#3584e4"  # GNOME blue fallback


def _type_tag_markup(ktype):
    """Pango markup for a key-type tag (e.g. ED25519) tinted with the accent
    colour — used inside an Adw.ActionRow subtitle, which is text-only."""
    if not ktype:
        return ""
    accent = _accent_hex()
    return (f"<span background='{accent}' bgalpha='12%' foreground='{accent}' "
            f"weight='bold'> {GLib.markup_escape_text(ktype)} </span>")


@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/key_chooser_dialog.ui")
class KeyChooserDialog(Adw.Window):
    """Modal chooser listing keys on disk and in the agent across two tabs.

    Each tab is a boxed list of selectable rows showing type · fingerprint ·
    comment/path. Checked rows are committed via ``on_add(path)`` when the user
    presses Add. Agent keys are materialised to a real path at add time.
    """

    __gtype_name__ = 'SshPilotKeyChooserDialog'

    cancel_btn = Gtk.Template.Child()
    add_btn = Gtk.Template.Child()
    stack = Gtk.Template.Child()

    def __init__(self, parent, *, disk_keys, agent_keys, existing_paths,
                 on_add, on_browse=None):
        super().__init__()
        if parent is not None:
            self.set_transient_for(parent)
        install_esc_to_close(self)

        self._on_add = on_add
        self._on_browse = on_browse
        self._checks = []  # (payload, check_button); payload is path or callable
        self._existing = {
            os.path.realpath(os.path.expanduser(p))
            for p in (existing_paths or []) if p
        }

        # Static shell (toolbar, header Cancel/Add, view switcher + stack) is in
        # the template; the disk/agent pages are built and added here.
        self.cancel_btn.connect("clicked", lambda *_a: self.close())
        self.add_btn.connect("clicked", self._on_add_clicked)

        self.stack.add_titled_with_icon(
            self._build_disk_page(disk_keys), "disk", _("On disk"),
            "computer-symbolic")
        self.stack.add_titled_with_icon(
            self._build_agent_page(agent_keys), "agent", _("In agent"),
            "dialog-password-symbolic")

    # ---- page builders --------------------------------------------------
    def _wrap_group(self, group):
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)
        box.append(group)
        scrolled.set_child(box)
        return scrolled

    def _build_disk_page(self, disk_keys):
        group = Adw.PreferencesGroup()
        for item in disk_keys:
            group.add(self._make_choice_row(
                title=item.get("name") or item.get("path"),
                ktype=item.get("ktype"),
                meta=item.get("meta"),
                path=item.get("path"),
                payload=item.get("path"),
                dedup_path=item.get("path"),
            ))
        if not disk_keys:
            group.add(self._placeholder_row(_("No private keys found in ~/.ssh")))
        if callable(self._on_browse):
            try:
                browse = Adw.ButtonRow(title=_("Browse…"), start_icon_name="folder-symbolic")
            except AttributeError:
                # Adw.ButtonRow requires libadwaita >= 1.6
                browse = Adw.ActionRow(title=_("Browse…"))
                try:
                    browse.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
                except Exception:
                    pass
            browse.connect("activated", self._on_browse_clicked)
            group.add(browse)
        return self._wrap_group(group)

    def _build_agent_page(self, agent_keys):
        group = Adw.PreferencesGroup()
        for item in agent_keys:
            group.add(self._make_choice_row(
                title=item.get("title"),
                ktype=item.get("ktype"),
                meta=item.get("meta"),
                path=None,
                payload=item.get("materializer"),
                dedup_path=None,
            ))
        if not agent_keys:
            group.add(self._placeholder_row(_("No keys loaded in ssh-agent")))
        return self._wrap_group(group)

    def _placeholder_row(self, text):
        row = Adw.ActionRow(title=text)
        row.set_sensitive(False)
        return row

    def _make_choice_row(self, *, title, ktype=None, meta=None, path=None,
                         payload, dedup_path):
        row = Adw.ActionRow(title=title or _("key"))
        check = Gtk.CheckButton()
        check.set_valign(Gtk.Align.CENTER)
        already = bool(dedup_path) and (
            os.path.realpath(os.path.expanduser(dedup_path)) in self._existing)
        meta_text = meta or ""
        if already:
            meta_text = (meta_text + " · " if meta_text else "") + _("Added")

        # Subtitle: type tag + fingerprint on top, the path dimmed below.
        tag = _type_tag_markup(ktype)
        first = "  ".join(p for p in (tag, GLib.markup_escape_text(meta_text) if meta_text else "") if p)
        lines = []
        if first:
            lines.append(first)
        if path:
            lines.append(f"<span alpha='55%'>{GLib.markup_escape_text(path)}</span>")
        if lines:
            row.set_subtitle("\n".join(lines))
            try:
                row.set_subtitle_lines(len(lines))
            except Exception:
                pass

        row.add_prefix(check)
        if already:
            check.set_active(True)
            check.set_sensitive(False)
            row.set_sensitive(False)
        else:
            check.connect("toggled", lambda *_a: self._update_add_sensitivity())
            row.set_activatable_widget(check)
            self._checks.append((payload, check))
        return row

    # ---- actions --------------------------------------------------------
    def _update_add_sensitivity(self):
        self.add_btn.set_sensitive(any(c.get_active() for _p, c in self._checks))

    def _on_add_clicked(self, *_a):
        for payload, check in self._checks:
            if not check.get_active():
                continue
            try:
                path = payload() if callable(payload) else payload
            except Exception:
                logger.debug("Key chooser: failed to resolve a selection", exc_info=True)
                path = None
            if path:
                self._on_add(path)
        self.close()

    def _on_browse_clicked(self, *_a):
        def _chosen(path):
            if path:
                self._on_add(path)
            self.close()
        try:
            self._on_browse(_chosen)
        except Exception:
            logger.debug("Key chooser browse failed", exc_info=True)


class FileListEditor(Adw.PreferencesGroup):
    """An editable list of file paths shown as an Adwaita preferences group.

    The group *title* is the section label (e.g. "Identity files (private
    keys)"); each path is an AdwActionRow with an optional per-key passphrase
    button and a remove button. The bottom of the group holds one Adw.ButtonRow
    per *add action* \u2014 e.g. "Add key from disk" and "Add key from agent" \u2014 so
    different key sources are never mixed in a single menu.

    add_actions: list of dicts, each:
        {'label': str, 'icon': str (optional),
         'discover': callable() -> [(display, value)],
         'browse':  callable(on_chosen) (optional)}
    A *value* may be a path string or a zero-arg callable returning a path
    (used to materialise an agent key's public key on demand).
    """

    __gtype_name__ = 'SshPilotFileListEditor'

    def __init__(self, *, title, add_actions=None,
                 with_passphrase=False, connection_manager=None,
                 on_changed=None, verify=None, rows_group=None,
                 add_at_bottom=False, empty_placeholder=None,
                 reorderable=False):
        super().__init__()
        self.set_title(title)
        self._model = PathList()
        self._rows = []
        self._with_passphrase = with_passphrase
        self._reorderable = reorderable
        self._connection_manager = connection_manager
        self._on_changed = on_changed
        self._rows_group = rows_group or self
        self._rows_visible = True
        self._add_at_bottom = add_at_bottom
        self._empty_row = None
        if empty_placeholder:
            self._empty_row = Adw.ActionRow(title=empty_placeholder)
            self._empty_row.set_sensitive(False)
            self._empty_row.set_activatable(False)
        # verify(path, passphrase) -> bool before storing (None disables it).
        self._verify = verify

        # Add actions sit in the group header by default, or in list rows below
        # the path rows when add_at_bottom is True.
        self._add_rows = []
        self._add_buttons = []
        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_box.set_valign(Gtk.Align.CENTER)
        for action in (add_actions or []):
            icon = action.get('icon', 'list-add-symbolic')
            label = action.get('label', '')
            btn = Gtk.Button()
            if label:
                btn.set_child(Adw.ButtonContent(
                    icon_name=icon,
                    label=label,
                ))
            else:
                btn.set_icon_name(icon)
                btn.add_css_class('flat')
                btn.set_tooltip_text(action.get('tooltip') or _("Add"))
            btn.connect('clicked', self._on_add_clicked, action)
            if add_at_bottom:
                row = Adw.ActionRow()
                row.set_activatable(False)
                btn.set_valign(Gtk.Align.CENTER)
                btn.set_halign(Gtk.Align.START)
                row.add_prefix(btn)
                row._add_button = btn
                self._add_rows.append(row)
            else:
                add_box.append(btn)
                self._add_buttons.append(btn)
        if self._add_buttons:
            self.set_header_suffix(add_box)
        if self._add_at_bottom:
            for row in self._add_rows:
                self._add_row_widget(row)
        self._sync_empty_placeholder()

    # ---- public API -----------------------------------------------------
    def get_paths(self):
        return self._model.get()

    def set_paths(self, paths):
        for row in list(self._rows):
            if row.get_parent() is not None:
                self._remove_row_widget(row)
        self._rows = []
        self._model.set(paths)
        for p in self._model.get():
            self._append_key_row(p)
        self._ensure_add_rows_last()
        self._sync_empty_placeholder()

    def set_visible(self, visible):
        super().set_visible(visible)
        self._rows_visible = visible
        for row in self._rows:
            row.set_visible(visible)
        for row in self._add_rows:
            row.set_visible(visible)
        if self._empty_row is not None:
            self._empty_row.set_visible(visible)

    def set_sensitive(self, sensitive):
        super().set_sensitive(sensitive)
        for row in self._rows:
            row.set_sensitive(sensitive)
        for row in self._add_rows:
            row.set_sensitive(sensitive)

    def add_path(self, path):
        if self._model.add(path):
            self._append_key_row(self._model.get()[-1])
            self._emit_changed()

    # ---- internals ------------------------------------------------------
    def _emit_changed(self):
        if callable(self._on_changed):
            try:
                self._on_changed(self)
            except Exception:
                logger.debug("FileListEditor on_changed callback failed", exc_info=True)

    def _ensure_add_rows_last(self):
        """Re-append the add buttons so they stay below the key rows, in order."""
        for btn in self._add_rows:
            if btn.get_parent() is not None:
                self._remove_row_widget(btn)
        for btn in self._add_rows:
            self._add_row_widget(btn)

    def _sync_empty_placeholder(self):
        """Show or hide the empty-state row above the add button."""
        if self._empty_row is None:
            return
        if self._model.get():
            if self._empty_row.get_parent() is not None:
                self._remove_row_widget(self._empty_row)
            return
        if self._empty_row.get_parent() is not None:
            return
        for btn in self._add_rows:
            if btn.get_parent() is not None:
                self._remove_row_widget(btn)
        self._add_row_widget(self._empty_row)
        for btn in self._add_rows:
            self._add_row_widget(btn)

    def _add_row_widget(self, row):
        row.set_visible(self._rows_visible)
        self._rows_group.add(row)

    def _remove_row_widget(self, row):
        parent = row.get_parent()
        if parent is not None and hasattr(parent, 'remove'):
            parent.remove(row)

    def _append_key_row(self, path):
        for btn in self._add_rows:
            if btn.get_parent() is not None:
                self._remove_row_widget(btn)
        if self._with_passphrase:
            row = self._make_key_expander_row(path)
        else:
            row = Adw.ActionRow(title=os.path.basename(path) or path, subtitle=path)
            try:
                row.set_subtitle_lines(1)
            except Exception:
                pass
            remove_btn = Gtk.Button(icon_name='user-trash-symbolic')
            remove_btn.add_css_class('flat')
            remove_btn.set_valign(Gtk.Align.CENTER)
            remove_btn.set_tooltip_text(_("Remove"))
            remove_btn.connect('clicked', lambda _b, p=path, r=row: self._remove_row(p, r))
            row.add_suffix(remove_btn)
        row._path = path
        if self._reorderable:
            self._setup_row_dnd(row)
        self._add_row_widget(row)
        self._rows.append(row)
        for btn in self._add_rows:
            self._add_row_widget(btn)
        self._renumber_rows()
        self._sync_empty_placeholder()

    def _renumber_rows(self):
        """Keep the circular order badges in sync with each key's position."""
        n = 0
        for row in self._rows:
            badge = getattr(row, '_order_badge', None)
            if badge is None:
                continue
            n += 1
            badge.set_label(str(n))

    # ---- drag-and-drop reordering ----------------------------------------
    def _setup_row_dnd(self, row):
        """Let *row* be dragged onto another row to change the offer order."""
        handle = Gtk.Image.new_from_icon_name('list-drag-handle-symbolic')
        handle.set_valign(Gtk.Align.CENTER)
        handle.add_css_class('dim-label')
        row.add_prefix(handle)

        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.MOVE)
        source.connect('prepare', self._on_drag_prepare, row)
        source.connect('drag-begin', self._on_drag_begin, row)
        row.add_controller(source)

        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        target.connect('drop', self._on_drop, row)
        row.add_controller(target)

    def _on_drag_prepare(self, _source, _x, _y, row):
        path = getattr(row, '_path', None)
        if not path or not row.get_sensitive():
            return None
        return Gdk.ContentProvider.new_for_value(path)

    def _on_drag_begin(self, source, _drag, row):
        try:
            source.set_icon(Gtk.WidgetPaintable.new(row), 0, 0)
        except Exception:
            pass

    def _on_drop(self, _target, value, _x, y, row):
        src_path = str(value)
        dest_path = getattr(row, '_path', None)
        paths = self._model.get()
        # Only accept drags that originate from this editor's own rows.
        if src_path not in paths or dest_path not in paths or src_path == dest_path:
            return False
        src_index = paths.index(src_path)
        dest_index = paths.index(dest_path)
        # Dropping on the lower half of a row inserts after it, upper half before.
        after = y > row.get_height() / 2
        new_index = dest_index - (1 if src_index < dest_index else 0) + (1 if after else 0)
        if not self._model.move(src_path, new_index):
            return False
        self._sync_row_order()
        self._emit_changed()
        return True

    def _sync_row_order(self):
        """Re-attach the existing row widgets in model order (preserves entry state)."""
        order = {p: i for i, p in enumerate(self._model.get())}
        self._rows.sort(key=lambda r: order.get(getattr(r, '_path', None), len(order)))
        for row in self._rows:
            if row.get_parent() is not None:
                self._remove_row_widget(row)
        for row in self._rows:
            self._add_row_widget(row)
        self._ensure_add_rows_last()
        self._renumber_rows()

    def _make_key_expander_row(self, path):
        """A key row with its passphrase entry shown beside it (two columns)."""
        _ensure_key_badge_css()
        norm = os.path.realpath(os.path.expanduser(path))
        ktype, _fp, _comment = _fingerprint_for_path(path)
        # Key list shows name as title; the type tag + path live in the subtitle.
        # The fingerprint is only shown in the key selector dialog.
        row = Adw.ActionRow(title=os.path.basename(path) or path)
        tag = _type_tag_markup(ktype)
        row.set_subtitle("  ".join(p for p in (tag, GLib.markup_escape_text(path)) if p))
        try:
            row.set_subtitle_lines(1)
        except Exception:
            pass

        # Prefixes render as number, then icon (add_prefix prepends, so the
        # icon is added first and the badge last to get this order).
        key_icon = Gtk.Image.new_from_icon_name('dialog-password-symbolic')
        key_icon.set_valign(Gtk.Align.CENTER)
        row.add_prefix(key_icon)

        # Order badge (number in a circle) reflecting the offer order.
        badge = Gtk.Label(label="")
        badge.add_css_class('key-order-badge')
        badge.set_valign(Gtk.Align.CENTER)
        badge.set_halign(Gtk.Align.CENTER)
        row.add_prefix(badge)
        row._order_badge = badge

        # Second column: per-key passphrase entry, always visible next to the key.
        pass_entry = Gtk.PasswordEntry()
        pass_entry.set_show_peek_icon(True)
        pass_entry.set_valign(Gtk.Align.CENTER)
        pass_entry.set_width_chars(18)
        try:
            pass_entry.set_property('placeholder-text', _("Key passphrase"))
        except Exception:
            pass
        row._pass_initial = ''
        # A stored passphrase can come from a slow secret backend (rbw on a large vault
        # is ≈1s per lookup), so load it off the main thread: the dialog opens instantly
        # and the entry fills in when ready, unless the user is already typing.
        if self._connection_manager is not None:
            self._load_passphrase_async(pass_entry, row, norm)
        # Clear the error state as soon as the user edits the value again.
        pass_entry.connect('changed', lambda e: e.remove_css_class('error'))
        # Commit on Enter and when focus leaves the entry.
        pass_entry.connect('activate', self._commit_passphrase, path, norm)
        focus = Gtk.EventControllerFocus()
        focus.connect('leave', lambda _c, e=pass_entry, p=path, n=norm: self._commit_passphrase(e, p, n))
        pass_entry.add_controller(focus)
        row.add_suffix(pass_entry)
        # Track the entry on the row so the save flow can flush all passphrases at once.
        row._pass_entry = pass_entry
        row._pass_path = path
        row._pass_norm = norm

        remove_btn = Gtk.Button(icon_name='user-trash-symbolic')
        remove_btn.add_css_class('flat')
        remove_btn.set_valign(Gtk.Align.CENTER)
        remove_btn.set_tooltip_text(_("Remove"))
        remove_btn.connect('clicked', lambda _b, p=path, r=row: self._remove_row(p, r))
        row.add_suffix(remove_btn)
        return row

    def _load_passphrase_async(self, pass_entry, row, norm):
        """Fetch a stored passphrase off the main thread and fill the entry when ready.

        Skips the fill if the user has already typed into the entry, and syncs
        ``_pass_initial`` to the loaded value so the save flow sees no spurious change.
        Widget writes are guarded — the dialog may have closed while we waited."""
        def _apply(value):
            try:
                if not pass_entry.get_text():   # don't clobber what the user typed
                    pass_entry.set_text(value)
                    row._pass_initial = value
            except Exception:
                pass
            return False  # one-shot idle

        def worker():
            try:
                existing = self._connection_manager.get_key_passphrase(norm) or ''
            except Exception:
                existing = ''
            if existing:
                GLib.idle_add(_apply, existing)

        threading.Thread(target=worker, daemon=True).start()

    def _commit_passphrase(self, pass_entry, path, norm, force=False):
        """Validate an edited passphrase; persistence is deferred to dialog save."""
        text = pass_entry.get_text()
        if text and callable(self._verify):
            try:
                ok = bool(self._verify(path, text))
            except Exception:
                ok = False
            if not ok:
                pass_entry.add_css_class('error')
                return False
        pass_entry.remove_css_class('error')
        return True

    @staticmethod
    def _secret_backend_needs_unlock() -> bool:
        try:
            from .secret_storage import get_secret_manager
            return bool(get_secret_manager().selected_needs_unlock())
        except Exception:
            return False

    def _passphrase_rows(self):
        """(entry, path, norm) for each key row that carries a passphrase entry."""
        out = []
        for row in self._rows:
            entry = getattr(row, '_pass_entry', None)
            if entry is not None:
                out.append((entry, getattr(row, '_pass_path', None),
                            getattr(row, '_pass_norm', None)))
        return out

    def has_pending_passphrases(self) -> bool:
        """True if any key row's passphrase changed — a store or a clear — pending save."""
        if not self._with_passphrase:
            return False
        for row in self._rows:
            entry = getattr(row, '_pass_entry', None)
            if entry is None:
                continue
            try:
                if entry.get_text() != (getattr(row, '_pass_initial', '') or ''):
                    return True
            except Exception:
                pass
        return False

    def pending_passphrase_operations(self):
        """Return changed passphrases as GTK-free worker operations.

        Each tuple is ``("store"|"delete", normalized_path, value)``.  Reading and
        validating entries stays on the GTK thread; only backend I/O uses these snapshots.
        """
        operations = []
        for entry, path, norm in self._passphrase_rows():
            try:
                if not self._commit_passphrase(entry, path, norm):
                    return None
                text = entry.get_text()
                row = next(
                    (candidate for candidate in self._rows
                     if getattr(candidate, '_pass_entry', None) is entry),
                    None,
                )
                initial = getattr(row, '_pass_initial', '') if row is not None else ''
                if text == initial:
                    continue
                operations.append(('store' if text else 'delete', norm, text))
            except Exception:
                logger.debug("Failed to collect passphrase for %s", norm, exc_info=True)
        return operations

    def _remove_row(self, path, row):
        self._model.remove(path)
        try:
            self._remove_row_widget(row)
        except Exception:
            pass
        if row in self._rows:
            self._rows.remove(row)
        self._renumber_rows()
        self._sync_empty_placeholder()
        self._emit_changed()

    def _on_add_clicked(self, row, action):
        # A 'chooser' action opens a custom dialog instead of the inline popover.
        chooser = action.get('chooser')
        if callable(chooser):
            chooser(self)
            return

        popover = Gtk.Popover()
        popover.set_parent(row)
        popover.set_position(Gtk.PositionType.TOP)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for m in ('top', 'bottom', 'start', 'end'):
            getattr(box, f'set_margin_{m}')(6)

        present = set(self._model.get())
        discover = action.get('discover')
        items = []
        if callable(discover):
            try:
                items = discover() or []
            except Exception:
                logger.debug("FileListEditor discover() failed", exc_info=True)
                items = []
        fresh = [(d, v) for d, v in items if callable(v) or (v and v not in present)]
        rendered_any = False
        for display, value in fresh:
            item = Gtk.Button(label=display)
            item.add_css_class('flat')
            try:
                item.get_child().set_halign(Gtk.Align.START)
            except Exception:
                pass
            # Close the popover first, then add on the next idle tick: adding a
            # row reparents the add-button rows to keep them last, which would
            # crash if done while a popover is still anchored to one of them.
            item.connect('clicked', lambda _x, v=value, pop=popover: (
                pop.popdown(), GLib.idle_add(self._add_value, v)))
            box.append(item)
            rendered_any = True

        if not rendered_any and not action.get('browse'):
            none_lbl = Gtk.Label(label=_("Nothing detected"))
            none_lbl.add_css_class('dim-label')
            none_lbl.set_margin_start(6)
            none_lbl.set_margin_end(6)
            box.append(none_lbl)

        browse = action.get('browse')
        if callable(browse):
            if rendered_any:
                box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            browse_item = Gtk.Button(label=_("Browse\u2026"))
            browse_item.add_css_class('flat')
            browse_item.connect('clicked', lambda _x, pop=popover, b=browse: (pop.popdown(), self._do_browse(b)))
            box.append(browse_item)

        popover.set_child(box)
        popover.popup()

    def _add_value(self, value):
        """Add a path, resolving a callable value (materialise-on-add) first."""
        try:
            path = value() if callable(value) else value
        except Exception:
            logger.debug("FileListEditor value producer failed", exc_info=True)
            path = None
        if path:
            self.add_path(path)

    def _do_browse(self, browse):
        if callable(browse):
            try:
                browse(self.add_path)
            except Exception:
                logger.debug("FileListEditor browse() failed", exc_info=True)

@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/connection_dialog.ui")
class ConnectionDialog(
    Adw.Window,
    ConnectionDialogValidationMixin,
    ConnectionDialogFieldHelpersMixin,
    ConnectionDialogPortForwardingMixin,
):
    """Dialog for adding/editing SSH connections using custom layout with pinned buttons"""

    __gtype_name__ = 'ConnectionDialog'

    __gsignals__ = {
        'connection-saved': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    content_mount = Gtk.Template.Child()
    cancel_button = Gtk.Template.Child()
    save_button = Gtk.Template.Child()
    
    def __init__(self, parent, connection=None, connection_manager=None, force_split_from_group=False, split_group_source=None, split_original_nickname=None, as_new=False):
        super().__init__()
        
        self.parent_window = parent
        self.connection = connection
        self.connection_manager = connection_manager
        # as_new: pre-fill from *connection* but save as a brand-new entry
        # (CLI "Save as new connection" after an ad-hoc session).
        self.is_editing = connection is not None and not as_new

        self.force_split_from_group = bool(force_split_from_group)
        self.split_group_source = split_group_source or (getattr(connection, 'source', None) if connection else None)
        if split_original_nickname:
            self.split_original_nickname = split_original_nickname
        elif connection is not None:
            self.split_original_nickname = getattr(connection, 'nickname', '')
        else:
            self.split_original_nickname = ''

        self._loading_connection_data = False
        self._active_key_path: Optional[str] = None

        self.set_title(_('Edit Connection') if self.is_editing else _('New Connection'))
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
        """Wire the dynamic content + buttons into the template skeleton."""
        # The container skeleton (header bar, separator, Cancel/Save button box)
        # lives in connection_dialog.blp; here we mount the tabbed preferences
        # content and wire the pinned buttons.
        self.preferences_content = self.create_preferences_content()
        self.preferences_content.set_vexpand(True)
        self.content_mount.append(self.preferences_content)

        self.cancel_button.connect("clicked", self.on_cancel_clicked)
        self.save_button.connect("clicked", self.on_save_clicked)
        self._save_buttons = [self.save_button]

        # "Verify Configuration" — pinned to the LEFT of the bottom button bar
        # (Cancel/Save stay right). Opens the effective-config viewer.
        try:
            button_box = self.save_button.get_parent()
            if button_box is not None:
                button_box.set_halign(Gtk.Align.FILL)
                verify_button = Gtk.Button(label=_("Verify Configuration"))
                verify_button.add_css_class("success")
                verify_button.set_tooltip_text(
                    _("Check this connection against effective SSH configuration"))
                verify_button.set_halign(Gtk.Align.START)
                verify_button.set_hexpand(True)
                verify_button.connect(
                    "clicked",
                    lambda *_a: self.advanced_tab._on_view_effective_clicked(None))
                button_box.prepend(verify_button)
                self.verify_button = verify_button
        except Exception:
            logger.debug("Could not add Verify Configuration button", exc_info=True)

        self.setup_keyboard_shortcuts()

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
    
    def _build_connection_tab_pages(self):
        """Build the connection editor tab pages as (name, title, widget) tuples."""

        def _page_box():
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.set_margin_top(12)
            box.set_margin_bottom(24)
            box.set_margin_start(24)
            box.set_margin_end(24)
            return box

        general_page = _page_box()
        for group in self.build_connection_groups():
            general_page.append(group)
        # Container for declarative FieldSpec rows rendered when a plugin
        # protocol is selected; empty and hidden for SSH.
        self._plugin_fields_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._plugin_fields_box.set_visible(False)
        general_page.append(self._plugin_fields_box)

        authentication_page = _page_box()
        for group in self.build_authentication_groups():
            authentication_page.append(group)

        forwarding_page = _page_box()
        for group in self.build_port_forwarding_groups():
            forwarding_page.append(group)

        commands_page = _page_box()
        commands_page.append(self.build_commands_group())

        advanced_page = _page_box()
        self.advanced_tab = SSHConfigAdvancedTab(self.connection_manager, parent_dialog=self)
        advanced_group = Adw.PreferencesGroup()
        advanced_group.add(self.advanced_tab)
        advanced_page.append(advanced_group)

        # Wake on LAN on its own page (built by build_connection_groups above).
        wol_page = _page_box()
        wol_page.append(self._wol_group)

        return [
            ("connection", _("Connection"), general_page),
            ("authentication", _("Authentication"), authentication_page),
            ("forwarding", _("Port Forwarding"), forwarding_page),
            ("commands", _("Commands"), commands_page),
            ("advanced", _("Advanced"), advanced_page),
            ("wol", _("Wake on LAN"), wol_page),
        ]

    # Wider than Adw.PreferencesPage's 600 so the dialog doesn't feel empty;
    # only reins in content on very wide windows.
    _CLAMP_ARGS = {"maximum_size": 900, "tightening_threshold": 700}

    @classmethod
    def _wrap_page_scrolled(cls, widget):
        """Give a tab page its own scroller so scroll position doesn't carry over between tabs."""
        clamp = Adw.Clamp(**cls._CLAMP_ARGS)
        clamp.set_child(widget)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(clamp)
        return scrolled

    def _create_preferences_viewstack_content(self, pages):
        """Card-style tabs via Adw.ViewStack + Adw.InlineViewSwitcher (libadwaita 1.7+)."""
        stack = Adw.ViewStack()
        stack.set_vexpand(True)
        self._stack_pages = {}
        for name, title, widget in pages:
            scrolled = self._wrap_page_scrolled(widget)
            stack.add_titled(scrolled, name, title)
            try:
                self._stack_pages[name] = stack.get_page(scrolled)
            except Exception:
                pass

        switcher = Adw.InlineViewSwitcher()
        switcher.set_stack(stack)
        switcher.set_hexpand(True)
        switcher.set_halign(Gtk.Align.FILL)
        _ensure_toggle_suggested_css()
        switcher.add_css_class("toggle-suggested")
        try:
            switcher.set_display_mode(Adw.InlineViewSwitcherDisplayMode.LABELS)
        except Exception:
            pass
        try:
            switcher.set_can_shrink(False)
        except Exception:
            pass
        try:
            switcher.set_homogeneous(False)
        except Exception:
            pass

        switcher_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        switcher_card.add_css_class("card")
        switcher_card.set_hexpand(True)
        switcher_card.set_margin_start(24)
        switcher_card.set_margin_end(24)
        switcher_card.set_margin_top(12)
        switcher_card.append(switcher)
        self._switcher_card = switcher_card

        # Same clamp as the pages so the switcher stays aligned with the
        # content edges when the dialog is resized.
        switcher_clamp = Adw.Clamp(**self._CLAMP_ARGS)
        switcher_clamp.set_child(switcher_card)

        def _relayout_switcher(*_args):
            try:
                switcher.queue_resize()
            except Exception:
                pass
            return False

        switcher.connect("map", lambda *_a: GLib.idle_add(_relayout_switcher))

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        container.set_vexpand(True)
        container.append(switcher_clamp)
        container.append(stack)
        return container

    def _create_preferences_notebook_content(self, pages):
        """Fallback tabbed layout for libadwaita < 1.7 (no InlineViewSwitcher)."""
        notebook = Gtk.Notebook()
        notebook.set_show_tabs(True)
        notebook.set_show_border(False)
        notebook.set_vexpand(True)
        self._notebook_pages = {}
        self._notebook_order = []
        for name, title, widget in pages:
            scrolled = self._wrap_page_scrolled(widget)
            notebook.append_page(scrolled, Gtk.Label(label=title))
            self._notebook_pages[name] = scrolled
            self._notebook_order.append((name, scrolled, title))
        self._notebook = notebook
        return notebook

    def create_preferences_content(self):
        """Create tabbed preferences content."""
        pages = self._build_connection_tab_pages()
        if hasattr(Adw, "InlineViewSwitcher"):
            return self._create_preferences_viewstack_content(pages)
        return self._create_preferences_notebook_content(pages)
    
    def setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts for common actions"""
        
        shortcut_controller = Gtk.ShortcutController()
        
        # Ctrl/Command+S to save
        save_trigger = "<Meta>s" if is_macos() else "<Primary>s"
        
        save_shortcut = Gtk.Shortcut()
        save_shortcut.set_trigger(Gtk.ShortcutTrigger.parse_string(save_trigger))
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
    
    def _auth_is_key_based(self) -> bool:
        try:
            return self.auth_toggle.get_active() == 0
        except Exception:
            return True

    def _selected_auth_method(self) -> int:
        """0 = key-based, 1 = password."""
        try:
            return int(self.auth_toggle.get_active())
        except Exception:
            return 0

    def _selected_key_mode(self) -> int:
        """0 = automatic, 1 = specific + IdentitiesOnly, 2 = specific."""
        try:
            specific = bool(getattr(self, 'key_select_row', None)
                            and self.key_select_row.get_selected() == 1)
        except Exception:
            specific = False
        if not specific:
            return 0
        try:
            only = self.key_only_row.get_active()
        except Exception:
            only = True
        return 1 if only else 2

    def _collect_identity_files(self):
        try:
            return self.key_editor.get_paths()
        except Exception:
            return []

    def _collect_certificate_files(self):
        try:
            return self.cert_editor.get_paths()
        except Exception:
            return []

    def _selected_add_keys_to_agent(self) -> str:
        """Return the AddKeysToAgent value for the selected combo row ('' = default)."""
        try:
            idx = self.add_keys_to_agent_row.get_selected()
            return self._add_keys_values[idx] if 0 <= idx < len(self._add_keys_values) else ''
        except Exception:
            return ''

    def on_auth_method_changed(self, *args):
        """Reveal key-based vs password controls based on the auth ToggleGroup."""
        is_key_based = self._auth_is_key_based()
        for name in ('key_selection_group', 'key_select_row',
                     'idonly_group', 'add_keys_to_agent_row'):
            row = getattr(self, name, None)
            if row is not None:
                row.set_visible(is_key_based)
        if hasattr(self, 'password_row'):
            try:
                title = _("Password (optional)") if is_key_based else _("Password")
                self.password_row.set_title(title)
            except Exception:
                pass
            self.password_row.set_visible(True)  # optional for keys, primary for password
        if hasattr(self, 'pubkey_auth_row'):
            self.pubkey_auth_row.set_visible(not is_key_based)
        # Agent/hardware key sources apply to BOTH Automatic and specific-key
        # modes (that's what Automatic relies on); hide only for password auth.
        if hasattr(self, 'hw_group'):
            self.hw_group.set_visible(is_key_based)
        # Update key/cert editor visibility for the current selection mode.
        self.on_key_select_changed()

    def on_key_select_changed(self, *args):
        """Show key rows for key auth, but only enable editing in specific-key mode."""
        is_key_based = self._auth_is_key_based()
        try:
            use_specific = bool(is_key_based and getattr(self, 'key_select_row', None)
                                and self.key_select_row.get_selected() == 1)
        except Exception:
            use_specific = False

        # In Automatic mode keep the specific-key rows visible but inactive, so
        # users can see what would apply without being able to edit it.
        key_editor = getattr(self, 'key_editor', None)
        if key_editor is not None:
            key_editor.set_visible(is_key_based)
            key_editor.set_sensitive(use_specific)

        key_only_row = getattr(self, 'key_only_row', None)
        if key_only_row is not None:
            key_only_row.set_visible(is_key_based)
            key_only_row.set_sensitive(use_specific)

        cert_editor = getattr(self, 'cert_editor', None)
        if cert_editor is not None:
            cert_editor.set_visible(is_key_based)
            cert_editor.set_sensitive(use_specific)

    # ---- discovery / browse for the key & certificate FileListEditors -------
    def _agent_keys(self):
        """List of (raw_line, blob, key_type, comment) for keys in ssh-agent."""
        keys = []
        try:
            result = subprocess.run(
                ['ssh-add', '-L'], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        comment = parts[2] if len(parts) >= 3 else ''
                        keys.append((line.strip(), parts[1], parts[0], comment))
        except Exception:
            logger.debug("ssh-add -L unavailable", exc_info=True)
        return keys

    def _make_agent_materializer(self, raw_line):
        return lambda: self._materialize_agent_pubkey(raw_line)

    def _materialize_agent_pubkey(self, raw_line):
        """Write an agent key's public key to the app config dir and return its path.

        Referencing that .pub as IdentityFile makes ssh use the agent's matching
        private key — the standard way to pin an agent-held key with no local
        private key file. Stored under the app's own config directory (keeping
        ~/.ssh untouched). Idempotent (named by comment + key-blob hash).
        """
        import hashlib
        parts = raw_line.split()
        if len(parts) < 2:
            return ''
        blob = parts[1]
        comment = parts[2] if len(parts) >= 3 else ''
        base = re.sub(r'[^A-Za-z0-9._-]+', '_', comment).strip('_') or 'agent'
        digest = hashlib.sha256(blob.encode()).hexdigest()[:8]
        try:
            agent_dir = os.path.join(get_config_dir(), 'agent_keys')
            os.makedirs(agent_dir, exist_ok=True)
        except Exception:
            return ''
        path = os.path.join(agent_dir, f"{base}-{digest}.pub")
        try:
            if not os.path.exists(path):
                with open(path, 'w') as f:
                    f.write(raw_line.strip() + "\n")
                try:
                    os.chmod(path, 0o644)
                except Exception:
                    pass
        except Exception:
            logger.debug("Failed to materialise agent public key", exc_info=True)
            return ''
        return path

    @staticmethod
    def _key_type_label(pub_first_field):
        t = (pub_first_field or '').lower()
        if t.startswith('sk-'):
            return _("FIDO security key")
        if 'ed25519' in t:
            return _("Ed25519")
        if 'ecdsa' in t:
            return _("ECDSA")
        if 'rsa' in t:
            return _("RSA")
        if 'dss' in t or 'dsa' in t:
            return _("DSA")
        return _("key")

    @staticmethod
    def _read_pub(pub_path):
        try:
            with open(pub_path) as f:
                parts = f.read().split()
            return parts if len(parts) >= 2 else None
        except Exception:
            return None

    def _discover_disk_keys(self):
        """[(key_name, path)] of private key files on disk (names only)."""
        out = []
        seen = set()
        cm = getattr(self, 'connection_manager', None)
        try:
            if cm is not None and hasattr(cm, 'load_ssh_keys'):
                for path in (cm.load_ssh_keys() or []):
                    if path and path not in seen:
                        seen.add(path)
                        out.append((os.path.basename(path), path))
        except Exception:
            logger.debug("disk key discovery failed", exc_info=True)
        return out

    def _discover_agent_keys(self):
        """[(display, materialiser)] of every key currently loaded in ssh-agent.

        Selecting one writes its public key to the app config dir so ssh can use
        the agent's matching private key."""
        import hashlib
        out = []
        for raw_line, blob, ktype, comment in self._agent_keys():
            label = self._key_type_label(ktype)
            name = comment or _("agent key")
            short = hashlib.sha256(blob.encode()).hexdigest()[:8]
            out.append((f"{name}  —  {label} ({short})",
                        self._make_agent_materializer(raw_line)))
        return out

    def _discover_certs(self):
        """Return [(display, path)] of detected *-cert.pub certificate files."""
        out = []
        try:
            ssh_dir = get_ssh_dir()
            if os.path.isdir(ssh_dir):
                for filename in sorted(os.listdir(ssh_dir)):
                    if filename.endswith('-cert.pub'):
                        out.append((filename, os.path.join(ssh_dir, filename)))
        except Exception:
            logger.debug("Certificate discovery failed", exc_info=True)
        return out

    def _browse_file(self, title, on_chosen, filters=None):
        try:
            dialog = Gtk.FileDialog(title=title)
            try:
                ssh_dir = get_ssh_dir()
                if os.path.isdir(ssh_dir):
                    dialog.set_initial_folder(Gio.File.new_for_path(ssh_dir))
            except Exception:
                pass
            if filters is not None:
                dialog.set_filters(filters)
            parent = self.get_transient_for()
            if not isinstance(parent, Gtk.Window):
                parent = None

            def _done(dlg, result):
                try:
                    gfile = dlg.open_finish(result)
                    if gfile and gfile.get_path():
                        on_chosen(gfile.get_path())
                except Exception:
                    logger.debug("File chooser cancelled or failed", exc_info=True)

            dialog.open(parent, None, _done)
        except Exception:
            logger.debug("Failed to open file chooser", exc_info=True)

    def _open_key_chooser(self, editor):
        """Open the disk/agent key chooser and add selected keys to *editor*."""
        disk_keys = []
        for name, path in self._discover_disk_keys():
            ktype, fp, _comment = _fingerprint_for_path(path)
            disk_keys.append({'name': name, 'path': path, 'ktype': ktype, 'meta': fp})

        agent_keys = []
        for raw_line, blob, ktype_raw, comment in self._agent_keys():
            ktype, fp, _c = _fingerprint_for_pub_line(raw_line)
            title = comment or _("agent key")
            agent_keys.append({
                'title': title,
                'ktype': ktype,
                'meta': fp,
                'materializer': self._make_agent_materializer(raw_line),
            })

        parent = self.get_root() if hasattr(self, 'get_root') else None
        if not isinstance(parent, Gtk.Window):
            parent = None
        dialog = KeyChooserDialog(
            parent,
            disk_keys=disk_keys,
            agent_keys=agent_keys,
            existing_paths=editor.get_paths(),
            on_add=editor.add_path,
            on_browse=self._browse_key,
        )
        dialog.present()

    def _browse_key(self, on_chosen):
        self._browse_file(_("Select SSH Key File"), on_chosen)

    def _browse_cert(self, on_chosen):
        filters = None
        try:
            cert_filter = Gtk.FileFilter()
            cert_filter.set_name(_("SSH Certificate Files"))
            cert_filter.add_pattern("*-cert.pub")
            cert_filter.add_pattern("*.pub")
            all_filter = Gtk.FileFilter()
            all_filter.set_name(_("All Files"))
            all_filter.add_pattern("*")
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(cert_filter)
            filters.append(all_filter)
        except Exception:
            filters = None
        self._browse_file(_("Select SSH Certificate File"), on_chosen, filters=filters)

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
            host = getattr(self, 'hostname_row', None)
            username = getattr(self, 'username_row', None)
            port = getattr(self, 'port_row', None)

            # Get values from UI or use defaults
            nickname_val = nickname.get_text().strip() if nickname else "my-server"
            host_val = host.get_text().strip() if host else "example.com"
            username_val = username.get_text().strip() if username else "user"
            port_val = port.get_text().strip() if port else "22"

            # Get authentication settings from the new auth widgets
            auth_method_val = self._selected_auth_method()
            key_select_mode_val = self._selected_key_mode()

            # Full lists of identity files / certificates from the editors
            identity_files = self._collect_identity_files()
            certificate_files = self._collect_certificate_files()

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

            # Add proxy settings
            proxy_hosts = []
            if hasattr(self, 'proxy_jump_row'):
                proxy_hosts = [h.strip() for h in re.split(r'[\s,]+', self.proxy_jump_row.get_text()) if h.strip()]
            if proxy_hosts:
                config_lines.append(f"    ProxyJump {','.join(proxy_hosts)}")
            if hasattr(self, 'forward_agent_row') and self.forward_agent_row.get_active():
                config_lines.append("    ForwardAgent yes")

            # Add authentication settings
            password_val = self.password_row.get_text().strip() if hasattr(self, 'password_row') else ''

            if auth_method_val == 0:  # Key-based auth (password optional)
                if key_select_mode_val in (1, 2) and identity_files:  # Specific key(s)
                    for kf in identity_files:
                        config_lines.append(f"    IdentityFile {kf}")
                    if key_select_mode_val == 1:
                        config_lines.append("    IdentitiesOnly yes")

                    # Add certificate(s) if specified
                    for cert in certificate_files:
                        config_lines.append(f"    CertificateFile {cert}")
                # Add combined authentication if a password is provided
                if password_val:
                    config_lines.append(
                        "    PreferredAuthentications gssapi-with-mic,hostbased,publickey,keyboard-interactive,password"
                    )
                # For automatic key selection, don't add IdentityFile
            else:  # Password auth only
                config_lines.append(
                    "    PreferredAuthentications keyboard-interactive,password"
                )
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
    HostName {getattr(self, 'hostname_row', None).get_text().strip() if hasattr(self, 'hostname_row') else 'example.com'}
    User {getattr(self, 'username_row', None).get_text().strip() if hasattr(self, 'username_row') else 'user'}
    Port {getattr(self, 'port_row', None).get_text().strip() if hasattr(self, 'port_row') else '22'}"""
    
    def _load_password_async(self):
        """Fetch the saved SSH password off the main thread and fill the row when ready.

        rbw on a large vault is ≈1s per lookup; doing it inline froze dialog open. Skips
        the fill if the user is already typing and syncs ``_orig_password`` so the loaded
        value isn't mistaken for a user edit. Widget writes are guarded (dialog may close)."""
        mgr = getattr(self.parent_window, 'connection_manager', None)
        if not mgr or not hasattr(self.connection, 'username'):
            return

        def _apply(pw):
            try:
                if pw and not self.password_row.get_text():
                    self.password_row.set_text(pw)
                    self._orig_password = pw
            except Exception:
                pass
            return False  # one-shot idle

        def worker():
            try:
                pw = mgr.get_connection_password(self.connection)
            except Exception:
                pw = None
            if pw:
                GLib.idle_add(_apply, pw)

        threading.Thread(target=worker, daemon=True).start()

    def load_connection_data(self):
        """Load connection data into the dialog fields"""
        # Load whenever we have a connection object — including as_new prefills
        # from CLI/ad-hoc connect (is_editing is False in that case).
        if not self.connection:
            return

        required_attrs = [
            'nickname_row', 'hostname_row', 'username_row', 'port_row',
            'proxy_jump_row', 'forward_agent_row',
            'auth_toggle', 'key_editor', 'cert_editor', 'password_row',
            'key_select_row', 'key_only_row', 'pubkey_auth_row'
        ]
        for attr in required_attrs:
            if not hasattr(self, attr):
                return

        if getattr(self, '_loading_connection_data', False):
            return

        self._loading_connection_data = True

        # Plugin-protocol connections: select the protocol (which renders the
        # declarative rows), load the shared rows + plugin fields, and skip
        # all SSH-specific loads below.
        protocol = getattr(self.connection, 'protocol', 'ssh') or 'ssh'
        if protocol != 'ssh':
            try:
                backends = getattr(self, '_protocol_backends', None) or []
                for index, backend in enumerate(backends):
                    if backend.protocol_id == protocol:
                        self.protocol_row.set_selected(index)
                        break
                self._apply_protocol_to_ui()
                if hasattr(self.connection, 'nickname'):
                    self.nickname_row.set_text(self.connection.nickname or "")
                self._load_shared_meta_rows()
                self._load_plugin_field_values()
            finally:
                self._loading_connection_data = False
            return

        try:
            # Load basic connection data
            if hasattr(self.connection, 'nickname'):
                self.nickname_row.set_text(self.connection.nickname or "")
            if hasattr(self.connection, 'hostname'):
                self.hostname_row.set_text(self.connection.hostname or "")
            if hasattr(self.connection, 'username'):
                self.username_row.set_text(self.connection.username or "")
            if hasattr(self.connection, 'port'):
                try:
                    self.port_row.set_text(str(int(self.connection.port) if self.connection.port else 22))
                except Exception:
                    self.port_row.set_text("22")

            # Load proxy settings (without triggering inline completion)
            if hasattr(self.connection, 'proxy_jump'):
                try:
                    self._set_text_without_completion(
                        self.proxy_jump_row,
                        ",".join(self.connection.proxy_jump or []),
                    )
                except Exception:
                    self._set_text_without_completion(self.proxy_jump_row, "")
            if hasattr(self.connection, 'forward_agent'):
                try:
                    self.forward_agent_row.set_active(bool(self.connection.forward_agent))
                except Exception:
                    self.forward_agent_row.set_active(False)

            # Load Wake-on-LAN and tags metadata from connections_meta
            self._load_shared_meta_rows()

            # Set authentication method (ToggleGroup: 0 key-based, 1 password)
            auth_method = getattr(self.connection, 'auth_method', 0)
            try:
                self.auth_toggle.set_active(int(auth_method or 0))
            except Exception:
                pass
            try:
                self.pubkey_auth_row.set_active(bool(getattr(self.connection, 'pubkey_auth_no', False)))
            except Exception:
                self.pubkey_auth_row.set_active(False)

            # Populate the identity-file editor with the FULL list the parser
            # resolved (fall back to the single keyfile for older/partial data).
            def _clean(value, placeholder):
                v = str(value or '').strip()
                return '' if v.lower() in (placeholder, '') else v

            identity_files = [p for p in (getattr(self.connection, 'identity_files', None) or []) if str(p).strip()]
            if not identity_files:
                single = _clean(
                    getattr(self.connection, 'keyfile', None) or getattr(self.connection, 'private_key', None),
                    'select key file or leave empty for auto-detection',
                )
                if single:
                    identity_files = [single]
            has_specific_key = bool(identity_files)
            self.key_editor.set_paths(identity_files)

            # Certificates: full list, with single-value fallback.
            certificate_files = [p for p in (getattr(self.connection, 'certificate_files', None) or []) if str(p).strip()]
            if not certificate_files:
                single_cert = _clean(getattr(self.connection, 'certificate', None), 'select certificate file (optional)')
                if single_cert:
                    certificate_files = [single_cert]
            self.cert_editor.set_paths(certificate_files)

            # Agent / hardware key sources (text fields)
            for attr, row in (
                ('identity_agent', getattr(self, 'identity_agent_row', None)),
                ('pkcs11_provider', getattr(self, 'pkcs11_provider_row', None)),
                ('security_key_provider', getattr(self, 'security_key_provider_row', None)),
            ):
                if row is not None:
                    try:
                        row.set_text(str(getattr(self.connection, attr, '') or ''))
                    except Exception:
                        pass
            # AddKeysToAgent → ComboRow selection
            if hasattr(self, 'add_keys_to_agent_row'):
                try:
                    val = str(getattr(self.connection, 'add_keys_to_agent', '') or '').strip().lower()
                    idx = self._add_keys_values.index(val) if val in self._add_keys_values else 0
                    self.add_keys_to_agent_row.set_selected(idx)
                except Exception:
                    self.add_keys_to_agent_row.set_selected(0)

            if hasattr(self.connection, 'password') and self.connection.password:
                self.password_row.set_text(self.connection.password)
            else:
                # Fallback: fetch from the selected secret backend off the main thread —
                # rbw on a large vault is ≈1s per lookup and would otherwise freeze the
                # dialog open. The masked row fills in when ready (see _load_password_async).
                self._load_password_async()
            # Capture original password value to detect user changes later. The async
            # fetch updates this when it fills the row, so a loaded value isn't seen as
            # a user edit on save.
            try:
                self._orig_password = self.password_row.get_text()
            except Exception:
                self._orig_password = ""

            # Key selection mode → Automatic/Specific radios + IdentitiesOnly switch.
            # (Per-key passphrases are loaded on demand by the editor's key button.)
            try:
                mode = None
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
                if has_specific_key and mode not in (1, 2):
                    mode = 2
                specific = mode in (1, 2)
                self.key_select_row.set_selected(1 if specific else 0)
                try:
                    self.key_only_row.set_active(mode == 1)
                except Exception:
                    pass
            except Exception:
                pass

            # Reveal the correct sections for the loaded method/mode.
            try:
                self.on_auth_method_changed()
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

                if hasattr(self, 'pre_command_row'):
                    pre_cmd_val = ''
                    try:
                        pre_cmd_val = getattr(self.connection, 'pre_command', '') or (
                            self.connection.data.get('pre_command') if hasattr(self.connection, 'data') else ''
                        ) or ''
                    except Exception:
                        pre_cmd_val = ''
                    self.pre_command_row.set_text(_display_safe(pre_cmd_val))
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
                
            # Load port forwarding rules
            if hasattr(self.connection, 'forwarding_rules') and self.connection.forwarding_rules:
                self.forwarding_rules = self.connection.forwarding_rules
                logger.debug(f"Loaded forwarding rules: {self.forwarding_rules}")
                
                self.load_port_forwarding_rules()

        except Exception as e:
            logger.error(f"Error loading connection data: {e}")
            self.show_error(_("Failed to load connection data"))
        finally:
            self._loading_connection_data = False


    def build_authentication_groups(self):
        """Build PreferencesGroups for authentication settings."""
        cm = getattr(self, 'connection_manager', None)

        # --- Method: key-based vs password (AdwToggleGroup) ---
        auth_group = Adw.PreferencesGroup(title=_("Authentication method"))
        method_row = Adw.ActionRow()
        method_row.set_activatable(False)
        self.auth_toggle = _build_expanding_toggle_group([_("Key-based"), _("Password")])
        self.auth_toggle.connect("notify::active", self.on_auth_method_changed)
        _set_action_row_child(method_row, self.auth_toggle)
        auth_group.add(method_row)

        # --- Key selection mode: Automatic vs Use specific keys ---
        self.key_selection_group = Adw.PreferencesGroup()
        key_select_model = Gtk.StringList()
        key_select_model.append(_("Automatic"))
        key_select_model.append(_("Use Specific Key(s)"))
        self.key_select_row = Adw.ComboRow(title=_("Key selection"))
        self.key_select_row.set_subtitle(_("Use SSH defaults or pick specific keys below."))
        self.key_select_row.set_model(key_select_model)
        self.key_select_row.set_selected(0)
        self.key_select_row.connect("notify::selected", self.on_key_select_changed)
        self.key_selection_group.add(self.key_select_row)

        # --- Identity files (private keys) ---
        self.key_editor = FileListEditor(
            title=_("Private Keys"),
            with_passphrase=True,
            connection_manager=cm,
            add_actions=[{
                'icon': 'plus-large-symbolic',
                'label': _("Add"),
                'chooser': lambda editor: self._open_key_chooser(editor),
            }],
            add_at_bottom=True,
            reorderable=True,
            verify=lambda path, passphrase: self.validator.verify_key_passphrase(
                os.path.expanduser(path), passphrase
            ),
        )

        # --- Certificates ---
        self.cert_editor = FileListEditor(
            title=_("Certificates"),
            add_actions=[
                {'icon': 'plus-large-symbolic', 'label': _("Add"),
                 'discover': self._discover_certs, 'browse': self._browse_cert},
            ],
            add_at_bottom=True,
            with_passphrase=False,
            connection_manager=cm,
        )

        # --- Key handling ---
        self.idonly_group = Adw.PreferencesGroup(title=_("Key handling"))
        self.key_only_row = Adw.SwitchRow()
        self.key_only_row.set_title(_("Only use the selected key(s)"))
        self.key_only_row.set_subtitle(_("Write IdentitiesOnly yes for this connection."))
        self.key_only_row.set_active(True)
        self.idonly_group.add(self.key_only_row)

        self._add_keys_values = ['', 'yes', 'no', 'ask', 'confirm']
        akta_model = Gtk.StringList()
        for lbl in (_("Default"), _("Yes"), _("No"), _("Ask"), _("Confirm")):
            akta_model.append(lbl)
        self.add_keys_to_agent_row = Adw.ComboRow(title=_("Add keys to agent"))
        self.add_keys_to_agent_row.set_subtitle(
            _("Load keys into ssh-agent on first use (AddKeysToAgent)")
        )
        self.add_keys_to_agent_row.set_model(akta_model)
        self.add_keys_to_agent_row.set_selected(0)
        self.idonly_group.add(self.add_keys_to_agent_row)

        # --- Password / password-only options ---
        password_group = Adw.PreferencesGroup(title=_("Password"))
        self.password_row = Adw.PasswordEntryRow(title=_("Password (optional)"))
        self.password_row.set_show_apply_button(False)
        password_group.add(self.password_row)

        self.pubkey_auth_row = Adw.SwitchRow()
        self.pubkey_auth_row.set_title(_("Disable public key authentication"))
        self.pubkey_auth_row.set_subtitle(_("Force password authentication only (PubkeyAuthentication no)."))
        self.pubkey_auth_row.set_active(False)
        password_group.add(self.pubkey_auth_row)

        # --- Agent & hardware key sources -------------------------------------
        # A key (and any cert that pairs with it) may come from an ssh-agent, a
        # PKCS#11 smartcard, or a FIDO security key rather than an on-disk file.
        self.hw_group = hw_group = Adw.PreferencesGroup(
            title=_("Agent and hardware keys"),
            description=_("Optional sources for IdentityAgent, PKCS#11, and FIDO security keys."),
        )
        self.identity_agent_row = Adw.EntryRow(title=_("IdentityAgent"))
        try:
            self.identity_agent_row.set_subtitle(_("Socket path, $VARIABLE, or none"))
        except Exception:
            pass
        hw_group.add(self.identity_agent_row)

        self.pkcs11_provider_row = Adw.EntryRow(title=_("PKCS#11 provider"))
        try:
            self.pkcs11_provider_row.set_subtitle(_("Provider library path"))
        except Exception:
            pass
        pkcs_btn = Gtk.Button(icon_name='document-open-symbolic')
        pkcs_btn.add_css_class('flat')
        pkcs_btn.set_valign(Gtk.Align.CENTER)
        pkcs_btn.set_tooltip_text(_("Browse for provider library"))
        pkcs_btn.connect('clicked', lambda *_a: self._browse_file(
            _("Select PKCS#11 provider library"),
            lambda p: self.pkcs11_provider_row.set_text(p)))
        self.pkcs11_provider_row.add_suffix(pkcs_btn)
        hw_group.add(self.pkcs11_provider_row)

        self.security_key_provider_row = Adw.EntryRow(title=_("FIDO security key provider"))
        try:
            self.security_key_provider_row.set_subtitle(_("Provider library path"))
        except Exception:
            pass
        sk_btn = Gtk.Button(icon_name='document-open-symbolic')
        sk_btn.add_css_class('flat')
        sk_btn.set_valign(Gtk.Align.CENTER)
        sk_btn.set_tooltip_text(_("Browse for provider library"))
        sk_btn.connect('clicked', lambda *_a: self._browse_file(
            _("Select FIDO security key provider library"),
            lambda p: self.security_key_provider_row.set_text(p)))
        self.security_key_provider_row.add_suffix(sk_btn)
        hw_group.add(self.security_key_provider_row)

        # Initialize visibility for new connections.
        try:
            self.on_auth_method_changed(self.auth_toggle, None)
        except Exception:
            pass

        return [
            auth_group,
            self.key_selection_group,
            self.key_editor,
            self.cert_editor,
            self.idonly_group,
            password_group,
            hw_group,
        ]

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
        
        # Host Group
        basic_group = Adw.PreferencesGroup(
            title=_("Host"),
        )

        # Protocol selector. Invisible while SSH is the only registered
        # backend; insensitive when editing (no cross-store migration).
        try:
            self._protocol_backends = list(protocol_registry().all())
        except Exception:
            self._protocol_backends = []
        self.protocol_row = Adw.ComboRow(title=_("Protocol"))
        try:
            names = Gtk.StringList()
            for backend in self._protocol_backends:
                names.append(backend.display_name or backend.protocol_id)
            self.protocol_row.set_model(names)
        except Exception:
            pass
        self.protocol_row.set_visible(len(self._protocol_backends) > 1)
        if self.is_editing:
            self.protocol_row.set_sensitive(False)
        # Reflect the right protocol in the dropdown: the connection's own when
        # editing, otherwise SSH. registry.all() is alphabetical, so index 0 is
        # not SSH — without this an edited SSH connection would show the first
        # listed protocol (e.g. Docker/Podman).
        target_protocol = 'ssh'
        if self.is_editing:
            target_protocol = getattr(self.connection, 'protocol', 'ssh') or 'ssh'
        try:
            for i, backend in enumerate(self._protocol_backends):
                if getattr(backend, 'protocol_id', '') == target_protocol:
                    self.protocol_row.set_selected(i)
                    break
        except Exception:
            pass
        self.protocol_row.connect('notify::selected', self._on_protocol_changed)
        basic_group.add(self.protocol_row)

        # Nickname
        self.nickname_row = Adw.EntryRow(
            title=_("Nickname (no whitespace allowed)")
        )
        basic_group.add(self.nickname_row)
        
        # Hostname
        self.hostname_row = Adw.EntryRow(title=_("Hostname / IP address"))
        basic_group.add(self.hostname_row)

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

        # Tags (app metadata, stored in connections_meta — not in ssh_config).
        # Adw.EntryRow has no subtitle/placeholder, so the hint lives in the title.
        self.tags_row = Adw.EntryRow(title=_("Tags (comma-separated)"))
        self.tags_row.set_tooltip_text(_("Used by the sidebar search, e.g. production, web"))
        tag_pick_btn = Gtk.Button()
        tag_pick_btn.set_icon_name('view-list-symbolic')
        tag_pick_btn.set_tooltip_text(_("Pick from existing tags"))
        tag_pick_btn.add_css_class('flat')
        tag_pick_btn.set_valign(Gtk.Align.CENTER)
        tag_pick_btn.connect('clicked', self._show_tag_picker_popover)
        self.tags_row.add_suffix(tag_pick_btn)
        # Inline autocompletion from existing tags (GtkEntryCompletion is
        # deprecated and never supported Adw.EntryRow, so done by hand).
        self._setup_comma_autocomplete(self.tags_row, self._tag_candidates)
        basic_group.add(self.tags_row)

        # Wake-on-LAN Group
        wol_group = Adw.PreferencesGroup(
            title=_("Wake on LAN"),
            description=_("Optional. Set MAC address to wake this host from the context menu. Host must be on the same subnet for detection.")
        )
        self.wol_mac_row = Adw.EntryRow(title=_("MAC address"))
        entry = self.wol_mac_row.get_child()
        if entry and hasattr(entry, 'set_placeholder_text'):
            entry.set_placeholder_text("aa:bb:cc:dd:ee:ff")
        wol_group.add(self.wol_mac_row)
        self.wol_broadcast_row = Adw.EntryRow(title=_("Broadcast IP (optional)"))
        if self.wol_broadcast_row.get_child() and hasattr(self.wol_broadcast_row.get_child(), 'set_placeholder_text'):
            self.wol_broadcast_row.get_child().set_placeholder_text(_("e.g. 192.168.1.255"))
        wol_group.add(self.wol_broadcast_row)
        self.wol_port_row = Adw.EntryRow(title=_("WoL port (optional)"))
        try:
            wpe = self.wol_port_row.get_child()
            if wpe and hasattr(wpe, 'set_input_purpose'):
                wpe.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if wpe and hasattr(wpe, 'set_max_length'):
                wpe.set_max_length(5)
        except Exception:
            pass
        self.wol_port_row.set_text("9")
        wol_group.add(self.wol_port_row)
        # Detect MAC button (run in thread, update row from main thread)
        wol_detect_row = Adw.ActionRow(title=_("Detect MAC from network"))
        wol_detect_row.set_subtitle(_("Host must be on and reachable on the same subnet"))
        wol_detect_btn = Gtk.Button(label=_("Detect MAC"))
        wol_detect_btn.connect("clicked", self._on_wol_detect_mac_clicked)
        wol_detect_row.add_suffix(wol_detect_btn)
        wol_detect_row.set_activatable(False)
        wol_group.add(wol_detect_row)

        # Routing / jump hosts.
        proxy_group = Adw.PreferencesGroup(
            title=_("Routing"),
            description=_("Optional SSH path settings used before authentication."),
        )
        # Adw.EntryRow has no subtitle/placeholder, so the hint lives in the title.
        self.proxy_jump_row = Adw.EntryRow(title=_("Jump hosts (comma-separated)"))
        self.proxy_jump_row.set_tooltip_text(_("Comma-separated ProxyJump chain, e.g. bastion1, bastion2"))
        # Inline autocompletion from saved connection nicknames. Bare-comma
        # separator to match the loaded format (",".join of proxy_jump).
        self._setup_comma_autocomplete(
            self.proxy_jump_row, self._jump_host_candidates, separator=","
        )

        # Inventory picker button — lets the user pick a host from the saved inventory.
        if self.connection_manager and self.connection_manager.connections:
            pick_btn = Gtk.Button()
            pick_btn.set_icon_name('view-list-symbolic')
            pick_btn.set_tooltip_text(_("Pick from inventory"))
            pick_btn.add_css_class('flat')
            pick_btn.set_valign(Gtk.Align.CENTER)
            pick_btn.connect('clicked', self._show_host_picker_popover)
            self.proxy_jump_row.add_suffix(pick_btn)

        proxy_group.add(self.proxy_jump_row)

        self.forward_agent_row = Adw.SwitchRow()
        self.forward_agent_row.set_title(_("Forward agent"))
        self.forward_agent_row.set_subtitle(_("Allow the remote host to use your local ssh-agent"))
        self.forward_agent_row.set_active(False)
        proxy_group.add(self.forward_agent_row)

        # Kept as attributes so _on_protocol_changed can hide the SSH-specific
        # parts when a plugin protocol is selected.
        self._host_group = basic_group
        self._routing_group = proxy_group
        self._wol_group = wol_group

        # Wake on LAN lives on its own (SSH-only) tab — see
        # _build_connection_tab_pages — so it isn't shown for non-SSH protocols.
        return [basic_group, proxy_group]
    
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
        from sshpilot import icon_utils
        self.add_rule_button = Gtk.Button(label=_("Add Rule"))
        icon_utils.set_button_icon(self.add_rule_button, "list-add-symbolic")
        self.add_rule_button.set_tooltip_text(_("Add a new port forwarding rule"))
        self.add_rule_button.connect("clicked", self.on_add_forwarding_rule_clicked)
        button_box.append(self.add_rule_button)
        
        # Port info button
        self.port_info_button = Gtk.Button(label=_("View Port Info"))
        icon_utils.set_button_icon(self.port_info_button, "network-transmit-receive-symbolic")
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

        # Initialize empty rules list if it doesn't exist
        if not hasattr(self, 'forwarding_rules'):
            self.forwarding_rules = []

        # Load any existing rules if editing
        if self.is_editing and self.connection and hasattr(self.connection, 'forwarding_rules'):
            self.load_port_forwarding_rules()

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

        # Return groups for PreferencesPage: Port forwarding first, about, X11 last
        return [rules_group, about_group, x11_group]

    def build_commands_group(self):
        """Build PreferencesGroup for configuring connection commands"""

        commands_group = Adw.PreferencesGroup(
            title=_("Connection Commands"),
            description=_(
                "Run a command automatically on connect.\n\n"
                "• Pre-Connection Command: Runs locally before connecting.\n"
                "• Local Command: Runs on your machine after connection (requires PermitLocalCommand).\n"
                "• Remote Command: Runs on the remote host (uses RequestTTY for interactive shell)."
            )
        )
        self.pre_command_row = Adw.EntryRow(title=_("Pre-Connection Command"))
        try:
            self.pre_command_row.set_subtitle(_("Executed locally before connecting"))
        except Exception:
            pass
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
        commands_group.add(self.pre_command_row)
        commands_group.add(self.local_command_row)
        commands_group.add(self.remote_command_row)

        return commands_group
    
    def on_cancel_clicked(self, button):
        """Handle cancel button click"""
        self.close()
    
    # --- Protocol selector / plugin protocol support ----------------------

    _SSH_ONLY_PAGES = ("authentication", "forwarding", "commands", "advanced", "wol")

    def _selected_protocol_backend(self):
        """The ProtocolBackend chosen in the selector (None -> SSH default)."""
        backends = getattr(self, '_protocol_backends', None) or []
        if not backends:
            return None
        try:
            index = int(self.protocol_row.get_selected())
        except Exception:
            index = -1
        if 0 <= index < len(backends):
            return backends[index]
        # Unselected / out of range -> prefer SSH (registry order is not SSH-first).
        for backend in backends:
            if getattr(backend, 'protocol_id', '') == 'ssh':
                return backend
        return backends[0]

    def _selected_protocol_id(self) -> str:
        backend = self._selected_protocol_backend()
        return getattr(backend, 'protocol_id', 'ssh') or 'ssh'

    def _set_page_visible(self, name: str, visible: bool):
        page = (getattr(self, '_stack_pages', None) or {}).get(name)
        if page is not None:
            try:
                page.set_visible(visible)
            except Exception:
                pass
            return
        # Gtk.Notebook fallback (libadwaita < 1.7): remove/re-insert the page,
        # keeping the original tab order.
        notebook = getattr(self, '_notebook', None)
        order = getattr(self, '_notebook_order', None)
        if notebook is None or not order:
            return
        try:
            for index, (page_name, scrolled, title) in enumerate(order):
                if page_name != name:
                    continue
                current = notebook.page_num(scrolled)
                if visible and current == -1:
                    insert_at = sum(
                        1 for n, s, _t in order[:index]
                        if notebook.page_num(s) != -1
                    )
                    notebook.insert_page(scrolled, Gtk.Label(label=title), insert_at)
                elif not visible and current != -1:
                    notebook.remove_page(current)
                return
        except Exception as e:
            logger.debug("Notebook page visibility failed: %s", e)

    def _update_switcher_visibility(self):
        """Hide the tab switcher when only one page is visible (e.g. a non-SSH
        protocol leaves just 'Connection') so there's no orphan full-width tab."""
        pages = getattr(self, '_stack_pages', None)
        if pages:
            try:
                visible = sum(1 for p in pages.values() if p.get_visible())
            except Exception:
                visible = 2
            card = getattr(self, '_switcher_card', None)
            if card is not None:
                card.set_visible(visible > 1)
            return
        notebook = getattr(self, '_notebook', None)
        if notebook is not None:
            try:
                notebook.set_show_tabs(notebook.get_n_pages() > 1)
            except Exception:
                pass

    def _on_protocol_changed(self, *_args):
        self._apply_protocol_to_ui()

    def _apply_protocol_to_ui(self):
        """Show/hide SSH-specific UI according to the selected protocol."""
        backend = self._selected_protocol_backend()
        is_ssh = self._selected_protocol_id() == 'ssh'

        for row_name in ('hostname_row', 'username_row', 'port_row'):
            row = getattr(self, row_name, None)
            if row is not None:
                try:
                    row.set_visible(is_ssh)
                except Exception:
                    pass
        routing = getattr(self, '_routing_group', None)
        if routing is not None:
            try:
                routing.set_visible(is_ssh)
            except Exception:
                pass
        for page_name in self._SSH_ONLY_PAGES:
            self._set_page_visible(page_name, is_ssh)
        # A single remaining page (non-SSH protocols) shouldn't show a lone tab.
        self._update_switcher_visibility()

        box = getattr(self, '_plugin_fields_box', None)
        if box is None:
            return
        try:
            while box.get_first_child():
                box.remove(box.get_first_child())
        except Exception:
            pass
        self._plugin_field_widgets = {}
        if not is_ssh and backend is not None:
            for group in self._build_plugin_field_rows(backend):
                box.append(group)
        box.set_visible(not is_ssh)

    def _build_plugin_field_rows(self, backend):
        """Render the backend's FieldSpec list as Adw rows, grouped by
        FieldSpec.group. Returns a list of Adw.PreferencesGroup."""
        try:
            specs = list(backend.connection_fields() or [])
        except Exception:
            logger.exception("connection_fields() failed for %r",
                             getattr(backend, 'protocol_id', '?'))
            specs = []

        groups: Dict[str, Any] = {}
        ordered_groups = []
        self._plugin_field_widgets = {}

        for spec in specs:
            group_key = getattr(spec, 'group', 'general') or 'general'
            if group_key not in groups:
                title = (backend.display_name or backend.protocol_id) \
                    if group_key == 'general' else group_key.replace('_', ' ').title()
                group = Adw.PreferencesGroup(title=title)
                groups[group_key] = group
                ordered_groups.append(group)
            group = groups[group_key]

            kind = getattr(spec, 'kind', 'text') or 'text'
            default = spec.default
            if kind == 'int':
                row = Adw.SpinRow.new_with_range(0, 2 ** 31 - 1, 1)
                row.set_title(spec.label)
                initial = default
                if initial is None and spec.key == 'port':
                    initial = getattr(backend, 'default_port', None)
                try:
                    row.set_value(int(initial))
                except (TypeError, ValueError):
                    pass
                getter = lambda r=row: int(r.get_value())
                setter = lambda v, r=row: r.set_value(int(v))
            elif kind == 'password':
                row = Adw.PasswordEntryRow(title=spec.label)
                if default:
                    row.set_text(str(default))
                getter = lambda r=row: r.get_text()
                setter = lambda v, r=row: r.set_text(str(v or ''))
            elif kind == 'choice':
                row = Adw.ComboRow(title=spec.label)
                choices = list(spec.choices or [])
                values = [value for value, _label in choices]
                names = Gtk.StringList()
                for _value, label in choices:
                    names.append(label)
                row.set_model(names)
                if default in values:
                    row.set_selected(values.index(default))

                def _choice_getter(r=row, vals=values):
                    index = int(r.get_selected())
                    return vals[index] if 0 <= index < len(vals) else None

                def _choice_setter(v, r=row, vals=values):
                    if v in vals:
                        r.set_selected(vals.index(v))

                getter, setter = _choice_getter, _choice_setter
            elif kind == 'switch':
                row = Adw.SwitchRow()
                row.set_title(spec.label)
                row.set_active(bool(default))
                getter = lambda r=row: bool(r.get_active())
                setter = lambda v, r=row: r.set_active(bool(v))
            elif kind == 'file':
                row = Adw.ActionRow(title=spec.label)
                row.set_activatable(False)
                holder = [str(default) if default else '']
                if holder[0]:
                    row.set_subtitle(holder[0])
                browse_btn = Gtk.Button()
                browse_btn.set_icon_name('document-open-symbolic')
                browse_btn.add_css_class('flat')
                browse_btn.set_valign(Gtk.Align.CENTER)

                def _on_browse(_btn, r=row, h=holder, title=spec.label):
                    dialog = Gtk.FileDialog()
                    dialog.set_title(title)

                    def _done(dlg, result):
                        try:
                            gfile = dlg.open_finish(result)
                            if gfile and gfile.get_path():
                                h[0] = gfile.get_path()
                                r.set_subtitle(h[0])
                        except Exception:
                            pass

                    dialog.open(self, None, _done)

                browse_btn.connect('clicked', _on_browse)
                row.add_suffix(browse_btn)
                getter = lambda h=holder: h[0]

                def _file_setter(v, r=row, h=holder):
                    h[0] = str(v or '')
                    r.set_subtitle(h[0])

                setter = _file_setter
            else:  # text
                row = Adw.EntryRow(title=spec.label)
                if default:
                    row.set_text(str(default))
                if getattr(spec, 'placeholder', ''):
                    entry = row.get_child()
                    if entry and hasattr(entry, 'set_placeholder_text'):
                        entry.set_placeholder_text(spec.placeholder)
                getter = lambda r=row: r.get_text().strip()
                setter = lambda v, r=row: r.set_text(str(v or ''))

            group.add(row)
            self._plugin_field_widgets[spec.key] = (spec, row, getter, setter)

        return ordered_groups

    def _load_shared_meta_rows(self):
        """Load protocol-agnostic app metadata (Wake-on-LAN, tags) into rows."""
        if hasattr(self, 'wol_mac_row'):
            try:
                cfg = getattr(self.parent_window, 'config', None)
                nickname = getattr(self.connection, 'nickname', '').strip()
                if cfg and nickname:
                    meta = cfg.get_connection_meta(nickname)
                    if meta:
                        self.wol_mac_row.set_text((meta.get('wol_mac') or '').strip())
                        self.wol_broadcast_row.set_text((meta.get('wol_broadcast_ip') or '').strip())
                        port_val = meta.get('wol_port')
                        if port_val is not None:
                            try:
                                self.wol_port_row.set_text(str(int(port_val)))
                            except Exception:
                                self.wol_port_row.set_text("9")
            except Exception as e:
                logger.debug("Load WoL meta: %s", e)

        if hasattr(self, 'tags_row'):
            try:
                cfg = getattr(self.parent_window, 'config', None)
                nickname = getattr(self.connection, 'nickname', '').strip()
                if cfg and nickname:
                    # Suppress inline autocompletion while loading.
                    self._set_text_without_completion(
                        self.tags_row,
                        ', '.join(cfg.get_connection_tags(nickname)),
                    )
            except Exception as e:
                logger.debug("Load tags meta: %s", e)

    def _load_plugin_field_values(self):
        """Populate rendered plugin rows from the connection's data dict."""
        data = getattr(self.connection, 'data', None) or {}
        for key, (spec, _row, _getter, setter) in (
                getattr(self, '_plugin_field_widgets', None) or {}).items():
            value = data.get(key, spec.default)
            if value is None:
                continue
            try:
                setter(value)
            except Exception as e:
                logger.debug("Failed to load plugin field %r: %s", key, e)

    def _save_plugin_connection(self, backend):
        """Collect, validate, and emit connection data for a plugin protocol."""
        nickname = self.nickname_row.get_text().strip()
        if not nickname:
            self.show_error(_("Please enter a nickname for this connection"))
            return

        data = {
            'nickname': nickname,
            'protocol': backend.protocol_id,
        }
        for key, (spec, row, getter, _setter) in (
                getattr(self, '_plugin_field_widgets', None) or {}).items():
            try:
                data[key] = getter()
            except Exception as e:
                logger.debug("Failed to read plugin field %r: %s", key, e)
                data[key] = spec.default
            if getattr(spec, 'required', False) and not data[key]:
                self.show_error(_("{field} is required").format(field=spec.label))
                try:
                    self._focus_row(row)
                except Exception:
                    pass
                return

        try:
            errors = list(backend.validate(data) or [])
        except Exception as e:
            errors = [str(e)]
        if errors:
            self.show_error("\n".join(errors))
            return

        self._persist_connection_meta(nickname)

        if self.is_editing and self.connection:
            try:
                self.connection.data.update(data)
            except Exception:
                pass

        self.emit('connection-saved', data)
        self.close()

    def _persist_connection_meta(self, nickname_for_meta):
        """Persist Wake-on-LAN and tags metadata (protocol-agnostic app meta)."""
        if nickname_for_meta and hasattr(self, 'wol_mac_row'):
            try:
                cfg = getattr(self.parent_window, 'config', None)
                if cfg:
                    meta = cfg.get_connection_meta(nickname_for_meta)
                    wol_mac = (self.wol_mac_row.get_text() or '').strip()
                    wol_broadcast = (self.wol_broadcast_row.get_text() or '').strip()
                    try:
                        wol_port = int((self.wol_port_row.get_text() or '9').strip() or '9')
                    except ValueError:
                        wol_port = 9
                    meta['wol_mac'] = wol_mac
                    meta['wol_broadcast_ip'] = wol_broadcast
                    meta['wol_port'] = wol_port
                    cfg.set_connection_meta(nickname_for_meta, meta)
            except Exception as e:
                logger.debug("Save WoL meta: %s", e)

        # Tags are keyed by the new nickname, so renames carry them forward.
        if nickname_for_meta and hasattr(self, 'tags_row'):
            try:
                cfg = getattr(self.parent_window, 'config', None)
                if cfg:
                    tags_raw = (self.tags_row.get_text() or '').strip()
                    tags = [t.strip() for t in tags_raw.split(',') if t.strip()] if tags_raw else []
                    cfg.set_connection_tags(nickname_for_meta, tags)
            except Exception as e:
                logger.debug("Save tags meta: %s", e)

    def on_save_clicked(self, *_args):
        """Handle save button click or dialog save response"""
        if getattr(self, '_secret_save_in_progress', False):
            return
        # Plugin protocols collect their own (declarative) fields; the rest of
        # this method is the SSH path, which serializes to ~/.ssh/config.
        backend = self._selected_protocol_backend()
        if backend is not None and backend.protocol_id != 'ssh':
            self._save_plugin_connection(backend)
            return
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
            
        # Initialize forwarding_rules list if needed
        if not hasattr(self, 'forwarding_rules') or self.forwarding_rules is None:
            self.forwarding_rules = []
        
        # Persist exactly what is in the editor list (enabled rules only) - no sanitization
        forwarding_rules = [dict(r) for r in self.forwarding_rules if r.get('enabled', True)]
        # Identity files / certificates come straight from the editors (full
        # lists). Per-key passphrases are stored as the user edits them via each
        # key row's passphrase button, so there is nothing to persist here.
        identity_files = self._collect_identity_files()
        certificate_files = self._collect_certificate_files()
        keyfile_value = identity_files[0] if identity_files else ''
        certificate_value = certificate_files[0] if certificate_files else ''

        try:
            logger.info(
                "ConnectionDialog save: %d forwarding rules, %d identity files",
                len(forwarding_rules or []), len(identity_files),
            )
            logger.debug("Forwarding rules: %s", forwarding_rules)
        except Exception:
            pass

        # Detect if password text was changed by user during this edit session
        try:
            # _orig_password is unset for new connections — treat it as empty so an
            # untouched empty field doesn't count as a change.
            password_changed = (self.password_row.get_text() != (getattr(self, '_orig_password', None) or ''))
        except Exception:
            password_changed = False


        # Get extra SSH config from advanced tab
        extra_ssh_config = ''
        if hasattr(self, 'advanced_tab'):
            try:
                extra_ssh_config = self.advanced_tab.get_extra_ssh_config()
                logger.debug(f"Retrieved extra SSH config from advanced tab: {extra_ssh_config}")
            except Exception as e:
                logger.error(f"Error getting extra SSH config from advanced tab: {e}")
                extra_ssh_config = ''

        key_select_mode_val = self._selected_key_mode()

        # Gather connection data
        connection_data = {
            'nickname': self.nickname_row.get_text().strip(),
            'hostname': self.hostname_row.get_text().strip(),
            'username': self.username_row.get_text().strip(),
            'port': int(self.port_row.get_text().strip() or '22'),
            'auth_method': self._selected_auth_method(),
            'keyfile': keyfile_value,
            'identity_files': identity_files,
            'certificate': certificate_value,
            'certificate_files': certificate_files,
            'key_select_mode': key_select_mode_val,
            'identity_agent': (self.identity_agent_row.get_text().strip()
                               if hasattr(self, 'identity_agent_row') else ''),
            'add_keys_to_agent': self._selected_add_keys_to_agent(),
            'pkcs11_provider': (self.pkcs11_provider_row.get_text().strip()
                                if hasattr(self, 'pkcs11_provider_row') else ''),
            'security_key_provider': (self.security_key_provider_row.get_text().strip()
                                      if hasattr(self, 'security_key_provider_row') else ''),
            'password': self.password_row.get_text(),
            'x11_forwarding': self.x11_row.get_active(),
            'pubkey_auth_no': self.pubkey_auth_row.get_active(),
            'proxy_jump': [h.strip() for h in re.split(r'[\s,]+', self.proxy_jump_row.get_text()) if h.strip()],
            'forward_agent': self.forward_agent_row.get_active(),

            'forwarding_rules': forwarding_rules,
            'pre_command': (self.pre_command_row.get_text() if hasattr(self, 'pre_command_row') else ''),
            'local_command': (self.local_command_row.get_text() if hasattr(self, 'local_command_row') else ''),
            'remote_command': (self.remote_command_row.get_text() if hasattr(self, 'remote_command_row') else ''),
            'extra_ssh_config': extra_ssh_config,
            'password_changed': bool(password_changed),
        }
        
        if getattr(self, 'force_split_from_group', False):
            connection_data['__split_from_group'] = True
            if getattr(self, 'split_group_source', None):
                connection_data['__split_source'] = self.split_group_source
            if getattr(self, 'split_original_nickname', None):
                connection_data['__split_original_nickname'] = self.split_original_nickname

        # Persist Wake-on-LAN and tags metadata to connections_meta
        nickname_for_meta = connection_data.get('nickname', '').strip()
        self._persist_connection_meta(nickname_for_meta)

        if self.is_editing and self.connection:
            connection_data['__previous_secret_identity'] = {
                'nickname': getattr(self.connection, 'nickname', ''),
                'hostname': getattr(self.connection, 'hostname', ''),
                'host': getattr(self.connection, 'host', ''),
                'username': getattr(self.connection, 'username', ''),
            }

        # Update the connection object locally when editing (do not persist here; window handles persistence)
        if self.is_editing and self.connection:
            try:
                self.connection.data.update(connection_data)
                self.connection.data.pop('aliases', None)
            except Exception:
                pass
            if hasattr(self.connection, 'aliases'):
                self.connection.aliases = []
            self.connection.proxy_jump = connection_data.get('proxy_jump', [])
            self.connection.forward_agent = connection_data.get('forward_agent', False)
            # Explicitly update forwarding rules to ensure they're fresh
            self.connection.forwarding_rules = forwarding_rules
            
        # Unlock first when needed, then persist all changed secrets in one worker. Secret
        # backends may invoke external tools (notably ``bw``), so none of this I/O may run
        # in the GTK signal handler.
        if self._needs_secret_unlock_before_save(connection_data):
            try:
                from .secret_unlock_dialog import prompt_unlock

                def _after_unlock(ok):
                    if ok:
                        self._store_secrets_then_save(connection_data)
                    else:
                        self._set_secret_save_busy(False)

                self._set_secret_save_busy(True)
                prompt_unlock(self, on_done=_after_unlock)
                return
            except Exception:
                logger.debug("Secret unlock-on-save gate failed", exc_info=True)
                self._set_secret_save_busy(False)
                self.show_error(_("The secure storage backend could not be unlocked."))
                return
        self._store_secrets_then_save(connection_data)

    def _set_secret_save_busy(self, busy):
        self._secret_save_in_progress = bool(busy)
        for button in getattr(self, '_save_buttons', []) or []:
            try:
                button.set_sensitive(not busy)
            except Exception:
                pass

    def _store_secrets_then_save(self, connection_data):
        """Persist changed secrets off-thread, then emit the normal save signal."""
        operations = []
        previous_identity = connection_data.pop('__previous_secret_identity', None)
        manager = getattr(self, 'connection_manager', None)
        if manager is None:
            manager = getattr(getattr(self, 'parent_window', None),
                              'connection_manager', None)

        password = connection_data.get('password') or ''
        if password or connection_data.get('password_changed'):
            operations.append(('password', 'store' if password else 'delete', '', password))

        editor = getattr(self, 'key_editor', None)
        if editor is not None:
            try:
                passphrase_operations = editor.pending_passphrase_operations()
                if passphrase_operations is None:
                    self._set_secret_save_busy(False)
                    self.show_error(_("Please correct the invalid key passphrase."))
                    return
                operations.extend(
                    ('passphrase', action, path, value)
                    for action, path, value in passphrase_operations)
            except Exception:
                logger.debug("Failed to collect key passphrase changes", exc_info=True)

        # update_connection normally persists the password synchronously. Mark this save
        # as pre-handled even when nothing changed so it does not repeat backend I/O.
        connection_data['__secret_storage_done'] = True
        if not operations:
            self._set_secret_save_busy(False)
            self._emit_connection_saved(connection_data)
            return
        if manager is None:
            self._set_secret_save_busy(False)
            self.show_error(_("Secure storage is unavailable."))
            return

        from .secret_storage import get_secret_manager
        from .secret_unlock_dialog import _friendly_backend_name, _spinner_dialog

        secret_manager = get_secret_manager()
        backend = secret_manager.selected_backend()
        backend_name = _friendly_backend_name(backend)
        _set_status, close_spinner, spinner = _spinner_dialog(
            self,
            _("Saving to {backend}").format(backend=backend_name),
            _("Saving passwords and passphrases to secure storage…"),
        )
        self._set_secret_save_busy(True)

        def _worker():
            ok = True
            try:
                for secret_type, action, key, value in operations:
                    if secret_type == 'password':
                        username = connection_data.get('username') or ''
                        if action == 'store':
                            ok = bool(manager.store_connection_password(
                                connection_data, value, username=username,
                                previous_connection=previous_identity))
                        else:
                            # Delete is idempotent: "nothing was stored" already is the
                            # desired end state, not a storage failure. Real backend
                            # errors raise and are caught below.
                            manager.delete_connection_passwords(
                                connection_data, username=username)
                            if previous_identity:
                                previous_user = previous_identity.get('username') or username
                                manager.delete_connection_passwords(
                                    previous_identity, username=previous_user)
                            ok = True
                    elif action == 'store':
                        ok = bool(manager.store_key_passphrase(key, value))
                    else:
                        manager.delete_key_passphrase(key)
                        ok = True
                    if not ok:
                        break
            except Exception:
                logger.exception("Failed to save connection secrets")
                ok = False
            GLib.idle_add(self._finish_secret_save, connection_data, ok,
                          close_spinner, spinner, True)

        completion_called = [False]

        def _after_config_saved(ok):
            completion_called[0] = True
            if ok:
                threading.Thread(target=_worker, daemon=True).start()
            else:
                self._finish_secret_save(
                    connection_data, False, close_spinner, spinner, False)

        # Preserve the established save order: persist connection/config data first, then
        # its credentials. The private marker keeps update_connection from performing the
        # same secret I/O synchronously inside this signal emission.
        connection_data['__save_completion'] = _after_config_saved
        self.emit('connection-saved', connection_data)
        if not completion_called[0]:
            _after_config_saved(False)

    def _finish_secret_save(self, connection_data, ok, close_spinner, spinner,
                            settings_saved):
        """Finish an asynchronous secret save on the GTK main thread."""
        def _after_closed(*_args):
            self._set_secret_save_busy(False)
            if ok:
                self.close()
            elif not settings_saved:
                self.show_error(_("The connection settings could not be saved."))
            else:
                self.show_error(_(
                    "The connection settings were saved, but secure storage rejected "
                    "a password or passphrase. Please try saving again."
                ))

        connected = False
        try:
            spinner.connect('closed', _after_closed)
            connected = True
        except Exception:
            pass
        close_spinner()
        if not connected:
            _after_closed()
        return False

    def _emit_connection_saved(self, connection_data):
        """Emit the saved signal and close the dialog."""
        self.emit('connection-saved', connection_data)
        self.close()

    def _needs_secret_unlock_before_save(self, connection_data) -> bool:
        """True when saving would store or delete a secret — a host password or a key
        passphrase — and the selected session backend (Bitwarden/Vaultwarden) is locked
        or not signed in, so it should be unlocked before the secret I/O runs."""
        try:
            from .secret_storage import get_secret_manager
            if not get_secret_manager().selected_needs_unlock():
                return False
            pw = connection_data.get('password')
            if pw and str(pw).strip():
                return True
            # Clearing a stored password is a vault delete, which a locked
            # session backend silently skips — unlock for it too.
            if connection_data.get('password_changed'):
                return True
            editor = getattr(self, 'key_editor', None)
            return bool(editor is not None and editor.has_pending_passphrases())
        except Exception:
            return False

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

