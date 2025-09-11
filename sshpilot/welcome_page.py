"""Welcome page widget for sshPilot."""

import gi
import shlex
import logging

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, Gdk

from gettext import gettext as _


from .connection_manager import Connection


class WelcomePage(Gtk.Overlay):
    """Welcome page shown when no tabs are open."""

    BG_OPTIONS = [
        (_("Default"), None),
        (
            _("Sunset"),
            "linear-gradient(135deg, #ff9a9e 0%, #fad0c4 100%)",
        ),
        (
            _("Ocean"),
            "linear-gradient(135deg, #a1c4fd 0%, #c2e9fb 100%)",
        ),
        (
            _("Mint"),
            "linear-gradient(135deg, #d4fc79 0%, #96e6a1 100%)",
        ),
        (
            _("Lavender"),
            "linear-gradient(135deg, #a18cd1 0%, #fbc2eb 100%)",
        ),
        (
            _("Peach"),
            "linear-gradient(135deg, #fccb90 0%, #d57eeb 100%)",
        ),
        (
            _("Sky"),
            "linear-gradient(135deg, #89f7fe 0%, #66a6ff 100%)",
        ),
        (
            _("Midnight"),
            "linear-gradient(135deg, #2c3e50 0%, #34495e 100%)",
        ),
        (
            _("Deep Forest"),
            "linear-gradient(135deg, #1a252f 0%, #2c5530 100%)",
        ),
        (
            _("Dark Ocean"),
            "linear-gradient(135deg, #0f3460 0%, #1e3c72 100%)",
        ),
        (
            _("Purple Night"),
            "linear-gradient(135deg, #2d1b69 0%, #4a148c 100%)",
        ),
    ]

    TILE_COLOR_OPTIONS = [
        (_("Default"), None),
        (_("Black"), "#000000"),
        (_("Dark Blue"), "#1a237e"),
        (_("Dark Green"), "#1b5e20"),
        (_("Dark Red"), "#b71c1c"),
        (_("Dark Purple"), "#4a148c"),
        (_("Dark Orange"), "#e65100"),
        (_("Dark Teal"), "#004d40"),
        (_("Dark Gray"), "#212121"),
        (_("Navy"), "#0d47a1"),
        (_("Forest"), "#2e7d32"),
        (_("Maroon"), "#c62828"),
        (_("Indigo"), "#3f51b5"),
    ]


    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.connection_manager = window.connection_manager
        self.config = window.config
        self.set_hexpand(True)
        self.set_vexpand(True)
        clamp = Adw.Clamp()
        clamp.set_halign(Gtk.Align.CENTER)
        clamp.set_valign(Gtk.Align.CENTER)
        grid = Gtk.Grid(column_spacing=24, row_spacing=24)
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)

        self._card_provider = Gtk.CssProvider()
        self._card_provider.load_from_data(
            b"""
            .welcome-card {
                min-width: 100px;
                min-height: 100px;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self._card_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._bg_provider = Gtk.CssProvider()
        self.get_style_context().add_provider(
            self._bg_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.add_css_class("welcome-bg")


        clamp = Adw.Clamp()
        clamp.set_halign(Gtk.Align.CENTER)
        clamp.set_valign(Gtk.Align.CENTER)
        grid = Gtk.Grid(column_spacing=24, row_spacing=24)
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)
        grid.set_halign(Gtk.Align.CENTER)
        grid.set_valign(Gtk.Align.CENTER)
        clamp.set_child(grid)
        self.set_child(clamp)

        # Combined color button
        self.color_button = Gtk.Button.new_from_icon_name("preferences-color-symbolic")
        self.color_button.add_css_class("flat")
        self.color_button.set_tooltip_text(_("Change colors"))
        self.color_button.set_halign(Gtk.Align.END)
        self.color_button.set_valign(Gtk.Align.START)
        self.color_button.set_margin_top(6)
        self.color_button.set_margin_end(6)
        self.color_button.set_focusable(False)  # Prevent stealing keyboard focus
        self.color_button.set_can_focus(False)  # Additional focus prevention
        self.color_button.connect("clicked", self._show_color_menu)
        self.add_overlay(self.color_button)

        # Combined color popover
        self.color_popover = Gtk.Popover.new()
        self.color_popover.set_has_arrow(True)
        self.color_popover.set_parent(self.color_button)
        
        # Create main container with sections
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)
        
        # Background colors section
        bg_label = Gtk.Label(label=_("Background Colors"))
        bg_label.set_halign(Gtk.Align.START)
        bg_label.set_css_classes(['heading'])
        main_box.append(bg_label)
        
        bg_grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        for idx, (name, css) in enumerate(self.BG_OPTIONS):
            btn = Gtk.Button()
            btn.set_size_request(32, 32)
            btn.add_css_class("flat")
            btn.set_tooltip_text(name)
            if css:
                provider = Gtk.CssProvider()
                provider.load_from_data(
                    f"* {{ background: {css}; border-radius:4px; }}".encode()
                )
                btn.get_style_context().add_provider(
                    provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            else:
                img = Gtk.Image.new_from_icon_name("window-close-symbolic")
                btn.set_child(img)
            btn.connect("clicked", self._on_bg_selected, css)
            bg_grid.attach(btn, idx % 3, idx // 3, 1, 1)
        
        # Custom background color button
        custom_bg_btn = Gtk.Button()
        custom_bg_btn.set_size_request(32, 32)
        custom_bg_btn.add_css_class("flat")
        custom_bg_btn.set_tooltip_text(_("Custom background color"))
        custom_bg_icon = Gtk.Image.new_from_icon_name("color-select-symbolic")
        custom_bg_btn.set_child(custom_bg_icon)
        custom_bg_btn.connect("clicked", self._on_custom_bg_color)
        bg_grid.attach(custom_bg_btn, len(self.BG_OPTIONS) % 3, len(self.BG_OPTIONS) // 3, 1, 1)
        
        main_box.append(bg_grid)
        
        # Separator
        separator = Gtk.Separator()
        main_box.append(separator)
        
        # Tile colors section
        tile_label = Gtk.Label(label=_("Tile Colors"))
        tile_label.set_halign(Gtk.Align.START)
        tile_label.set_css_classes(['heading'])
        main_box.append(tile_label)
        
        tile_grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        for idx, (name, color) in enumerate(self.TILE_COLOR_OPTIONS):
            btn = Gtk.Button()
            btn.set_size_request(32, 32)
            btn.add_css_class("flat")
            btn.set_tooltip_text(name)
            if color:
                provider = Gtk.CssProvider()
                provider.load_from_data(
                    f"* {{ background: {color}; border-radius:4px; }}".encode()
                )
                btn.get_style_context().add_provider(
                    provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            else:
                img = Gtk.Image.new_from_icon_name("window-close-symbolic")
                btn.set_child(img)
            btn.connect("clicked", self._on_tile_color_selected, color)
            tile_grid.attach(btn, idx % 3, idx // 3, 1, 1)
        
        # Custom tile color button
        custom_tile_btn = Gtk.Button()
        custom_tile_btn.set_size_request(32, 32)
        custom_tile_btn.add_css_class("flat")
        custom_tile_btn.set_tooltip_text(_("Custom tile color"))
        custom_tile_icon = Gtk.Image.new_from_icon_name("color-select-symbolic")
        custom_tile_btn.set_child(custom_tile_icon)
        custom_tile_btn.connect("clicked", self._on_custom_tile_color)
        tile_grid.attach(custom_tile_btn, len(self.TILE_COLOR_OPTIONS) % 3, len(self.TILE_COLOR_OPTIONS) // 3, 1, 1)
        
        main_box.append(tile_grid)
        
        self.color_popover.set_child(main_box)
        
        # Load saved color settings
        self._load_color_settings()
        
        # Create welcome page cards
        quick_connect_card = self.create_card(
            _('Quick Connect'),
            _('Connect to a server using SSH command'),
            'network-server-symbolic',
            self.on_quick_connect_clicked
        )

        local_terminal_card = self.create_card(
            _('Local Terminal'),
            _('Open a local terminal session'),
            'utilities-terminal-symbolic',
            lambda *_: window.terminal_manager.show_local_terminal()
        )

        shortcuts_card = self.create_card(
            _('Shortcuts'),
            _('View and learn keyboard shortcuts'),
            'preferences-desktop-keyboard-symbolic',
            lambda *_: window.show_shortcuts_window()
        )

        help_card = self.create_card(
            _('Online Help'),
            _('View online documentation and help'),
            'help-contents-symbolic',
            lambda *_: self.open_online_help()
        )

        # Add cards to grid (2 columns, 2 rows)
        grid.attach(quick_connect_card, 0, 0, 1, 1)
        grid.attach(local_terminal_card, 1, 0, 1, 1)
        grid.attach(shortcuts_card, 0, 1, 1, 1)
        grid.attach(help_card, 1, 1, 1, 1)

    def _load_color_settings(self):
        """Load saved color settings from configuration"""
        try:
            # Load background color
            bg_color = self.config.get_setting('welcome.background_color', None)
            if bg_color:
                self._bg_provider.load_from_data(
                    f".welcome-bg {{ background: {bg_color}; }}".encode()
                )
            
            # Load tile color
            tile_color = self.config.get_setting('welcome.tile_color', None)
            self._apply_tile_color(tile_color)
        except Exception as e:
            logging.error(f"Failed to load color settings: {e}")

    def _save_background_color(self, css):
        """Save background color to configuration"""
        try:
            self.config.set_setting('welcome.background_color', css)
        except Exception as e:
            logging.error(f"Failed to save background color: {e}")

    def _save_tile_color(self, color):
        """Save tile color to configuration"""
        try:
            self.config.set_setting('welcome.tile_color', color)
        except Exception as e:
            logging.error(f"Failed to save tile color: {e}")

    def _apply_tile_color(self, color):
        """Apply tile color without saving to config"""
        if color is None:
            # Reset to theme default styling
            css = """
            .welcome-card {
                min-width: 100px;
                min-height: 100px;
            }
            """
        else:
            hover_color = self._lighten_color(color, 0.1)
            active_color = self._lighten_color(color, 0.2)
            border_color = self._lighten_color(color, 0.3)
            
            css = f"""
            .welcome-card {{
                min-width: 100px;
                min-height: 100px;
                background: {color};
                color: #ffffff;
                border-radius: 12px;
                border: 1px solid {border_color};
            }}
            .welcome-card:hover {{
                background: {hover_color};
                border-color: {self._lighten_color(border_color, 0.2)};
            }}
            .welcome-card:active {{
                background: {active_color};
            }}
            """
        self._card_provider.load_from_data(css.encode('utf-8'))

    def create_card(self, title, tooltip_text, icon_name, callback):
        """Create an activatable card with icon and title"""
        # Create a vertical box for icon and text
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)
        
        # Create larger icon
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_icon_size(Gtk.IconSize.LARGE)  # Use large icon size
        icon.set_pixel_size(64)  # Set specific pixel size for even larger icons
        content.append(icon)
        
        # Create title label
        title_label = Gtk.Label(label=title)
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_css_classes(['title-5'])  # Use larger text style
        content.append(title_label)

        card = Adw.Bin()
        card.add_css_class("card")
        card.add_css_class("activatable")
        card.add_css_class("welcome-card")
        card.set_child(content)
        card.set_tooltip_text(tooltip_text)
        card.set_focusable(True)
        card.set_accessible_role(Gtk.AccessibleRole.BUTTON)
        card.set_hexpand(True)
        card.set_vexpand(True)
        card.set_valign(Gtk.Align.FILL)

        click = Gtk.GestureClick()
        click.connect("released", lambda *_: callback(card))
        card.add_controller(click)

        key = Gtk.EventControllerKey()
        key.connect(
            "key-released",
            lambda _c, keyval, *_: (
                callback(card)
                if keyval in (Gdk.KEY_Return, Gdk.KEY_space)
                else False
            ),
        )
        card.add_controller(key)

        return card


    # Quick connect handlers
    def _show_color_menu(self, button):
        self.color_popover.popup()

    def _on_bg_selected(self, button, css):
        if css:
            self._bg_provider.load_from_data(
                f".welcome-bg {{ background: {css}; }}".encode()

            )
        else:
            self._bg_provider.load_from_data(b"")
        
        # Save to configuration
        self._save_background_color(css)
        self.color_popover.popdown()

    def _on_tile_color_selected(self, button, color):
        # Apply the tile color
        self._apply_tile_color(color)
        
        # Save to configuration
        self._save_tile_color(color)
        self.color_popover.popdown()

    def _lighten_color(self, hex_color, factor):
        """Lighten a hex color by a factor (0.0 to 1.0)"""
        # Remove # if present
        hex_color = hex_color.lstrip('#')
        
        # Convert to RGB
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        
        # Lighten by factor
        r = min(255, int(r + (255 - r) * factor))
        g = min(255, int(g + (255 - g) * factor))
        b = min(255, int(b + (255 - b) * factor))
        
        # Convert back to hex
        return f"#{r:02x}{g:02x}{b:02x}"

    def _on_custom_bg_color(self, button):
        """Open color picker for custom background color"""
        self._open_color_picker(self._on_custom_bg_selected, _("Select Background Color"))

    def _on_custom_tile_color(self, button):
        """Open color picker for custom tile color"""
        self._open_color_picker(self._on_custom_tile_selected, _("Select Tile Color"))

    def _open_color_picker(self, callback, title):
        """Open a color picker dialog"""
        dialog = Gtk.ColorChooserDialog(title=title, transient_for=self.window)
        dialog.set_modal(True)
        
        # Set initial color (white as default)
        initial_color = Gdk.RGBA()
        initial_color.parse("#ffffff")
        dialog.set_rgba(initial_color)
        
        def on_response(dialog, response):
            if response == Gtk.ResponseType.OK:
                color = dialog.get_rgba()
                # Convert to hex
                hex_color = f"#{int(color.red * 255):02x}{int(color.green * 255):02x}{int(color.blue * 255):02x}"
                callback(hex_color)
            dialog.destroy()
        
        dialog.connect("response", on_response)
        dialog.present()

    def _on_custom_bg_selected(self, hex_color):
        """Handle custom background color selection"""
        # Create a solid color CSS
        css = f"#{hex_color[1:]}"  # Remove # prefix for CSS
        self._on_bg_selected(None, css)

    def _on_custom_tile_selected(self, hex_color):
        """Handle custom tile color selection"""
        self._on_tile_color_selected(None, hex_color)


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
                "Visit the sshPilot documentation at:\nhttps://github.com/mfat/sshpilot/wiki"
            )
            dialog.add_response("ok", "OK")
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_modal(True)
            dialog.set_transient_for(self.window)
            dialog.present()
    
    def _parse_ssh_command(self, command_text):
        """Parse SSH command text and extract connection parameters"""
        try:
            # Handle simple user@host format (backward compatibility)
            if not command_text.startswith('ssh') and '@' in command_text and ' ' not in command_text:
                username, host = command_text.split('@', 1)
                return {
                    "nickname": host,
                    "host": host,
                    "username": username,
                    "port": 22,
                    "auth_method": 0,  # Default to key-based auth
                    "key_select_mode": 0  # Try all keys
                }
            
            # Parse full SSH command
            # Remove 'ssh' prefix if present
            if command_text.startswith('ssh '):
                command_text = command_text[4:]
            elif command_text.startswith('ssh'):
                command_text = command_text[3:]
            
            # Use shlex to properly parse the command with quoted arguments
            try:
                args = shlex.split(command_text)
            except ValueError:
                # If shlex fails, fall back to simple split
                args = command_text.split()
            
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
                "dynamic_forwards": []
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
                    connection_data["key_select_mode"] = 1  # Use specific key
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    # Handle SSH options like -o "UserKnownHostsFile=/dev/null"
                    option = args[i + 1]
                    if '=' in option:
                        key, value = option.split('=', 1)
                        if key == 'User':
                            connection_data["username"] = value
                        elif key == 'Port':
                            try:
                                connection_data["port"] = int(value)
                            except ValueError:
                                pass
                        elif key == 'IdentityFile':
                            connection_data["keyfile"] = value
                            connection_data["key_select_mode"] = 1
                    i += 2
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
                    connection_data["key_select_mode"] = 1
                    i += 1
                    continue
                elif not arg.startswith('-'):
                    # This should be the host specification (user@host)
                    if '@' in arg:
                        username, host = arg.split('@', 1)
                        connection_data["username"] = username
                        connection_data["host"] = host
                        connection_data["nickname"] = host
                    else:
                        # Just hostname, no username
                        connection_data["host"] = arg
                        connection_data["nickname"] = arg
                    i += 1
                else:
                    # Unknown option, skip it
                    i += 1
            
            # Validate that we have at least a host
            if not connection_data["host"]:
                return None
            
            return connection_data
            
        except Exception as e:
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
                        "key_select_mode": 0
                    }
                except:
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
            # Handle simple user@host format (backward compatibility)
            if not command_text.startswith('ssh') and '@' in command_text and ' ' not in command_text:
                username, host = command_text.split('@', 1)
                return {
                    "nickname": host,
                    "host": host,
                    "username": username,
                    "port": 22,
                    "auth_method": 0,  # Default to key-based auth
                    "key_select_mode": 0  # Try all keys
                }
            
            # Parse full SSH command
            # Remove 'ssh' prefix if present
            if command_text.startswith('ssh '):
                command_text = command_text[4:]
            elif command_text.startswith('ssh'):
                command_text = command_text[3:]
            
            # Use shlex to properly parse the command with quoted arguments
            try:
                args = shlex.split(command_text)
            except ValueError:
                # If shlex fails, fall back to simple split
                args = command_text.split()
            
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
                "dynamic_forwards": []
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
                    connection_data["key_select_mode"] = 1  # Use specific key
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    # Handle SSH options like -o "UserKnownHostsFile=/dev/null"
                    option = args[i + 1]
                    if '=' in option:
                        key, value = option.split('=', 1)
                        if key == 'User':
                            connection_data["username"] = value
                        elif key == 'Port':
                            try:
                                connection_data["port"] = int(value)
                            except ValueError:
                                pass
                        elif key == 'IdentityFile':
                            connection_data["keyfile"] = value
                            connection_data["key_select_mode"] = 1
                    i += 2
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
                    connection_data["key_select_mode"] = 1
                    i += 1
                    continue
                elif not arg.startswith('-'):
                    # This should be the host specification (user@host)
                    if '@' in arg:
                        username, host = arg.split('@', 1)
                        connection_data["username"] = username
                        connection_data["host"] = host
                        connection_data["nickname"] = host
                    else:
                        # Just hostname, no username
                        connection_data["host"] = arg
                        connection_data["nickname"] = arg
                    i += 1
                else:
                    # Unknown option, skip it
                    i += 1
            
            # Validate that we have at least a host
            if not connection_data["host"]:
                return None
            
            return connection_data
            
        except Exception as e:
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
                        "key_select_mode": 0
                    }
                except:
                    pass
            return None
