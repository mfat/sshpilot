"""Preferences dialog and font selection utilities."""

import os
import logging
import shutil
import json
import hashlib
import zipfile
import tempfile
import threading
import importlib
import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Optional
from gettext import gettext as _

from .platform_utils import get_config_dir, is_flatpak, is_macos
from .file_manager_integration import (
    has_internal_file_manager,
    has_native_gvfs_support,
)
from .shortcut_editor import ShortcutsPreferencesPage
from .monospace_font_dialog import MonospaceFontDialog


import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Gdk, Adw, Pango, Vte, GLib, Gio

logger = logging.getLogger(__name__)


_GROUP_PREVIEW_CSS_INSTALLED = False


class _GroupDisplayToggleFallback:
    """Fallback controller for group display toggle when Adw.ToggleGroup is missing."""

    def __init__(self, buttons: Dict[str, Gtk.ToggleButton], default: str = "fullwidth"):
        self._buttons = buttons
        self._default = default
        self._active_name: Optional[str] = None
        self._callbacks: List[Any] = []
        self._syncing = False

        for name, button in self._buttons.items():
            button.connect("toggled", self._on_button_toggled, name)

    def get_active_name(self) -> str:
        if self._active_name:
            return self._active_name
        if self._default in self._buttons:
            return self._default
        if self._buttons:
            return next(iter(self._buttons.keys()))
        return self._default

    def set_active_name(self, name: str):
        if name not in self._buttons:
            return
        if self._active_name == name:
            return

        self._syncing = True
        try:
            self._active_name = name
            for option, button in self._buttons.items():
                button.set_active(option == name)
        finally:
            self._syncing = False

    def connect(self, callback):
        self._callbacks.append(callback)

    def _emit_changed(self):
        for callback in self._callbacks:
            callback(self, None)

    def _on_button_toggled(self, button: Gtk.ToggleButton, name: str):
        if self._syncing:
            return

        if not button.get_active():
            # Keep one option active at all times.
            if self._active_name == name:
                self._syncing = True
                try:
                    button.set_active(True)
                finally:
                    self._syncing = False
            return

        if self._active_name == name:
            return

        self._active_name = name
        self._syncing = True
        try:
            for option, other_button in self._buttons.items():
                if option != name and other_button.get_active():
                    other_button.set_active(False)
        finally:
            self._syncing = False

        self._emit_changed()


def _install_group_display_preview_css():
    global _GROUP_PREVIEW_CSS_INSTALLED
    if _GROUP_PREVIEW_CSS_INSTALLED:
        return

    display = Gdk.Display.get_default()
    if not display:
        return

    provider = Gtk.CssProvider()
    css = """
    .group-display-preview-container {
        padding: 4px 0;
    }

    .group-display-preview-title {
        font-weight: 600;
    }

    .group-display-preview-parent {
        background-color: alpha(@accent_bg_color, 0.12);
        border-radius: 8px;
        padding: 8px 10px;
    }

    .group-display-preview-parent-title {
        font-weight: 600;
    }

    .group-display-preview-parent-subtitle {
        opacity: 0.7;
        font-size: 0.9em;
    }

    .group-display-preview-row {
        background-color: alpha(@accent_bg_color, 0.18);
        border-radius: 10px;
        padding: 8px 12px;
        transition: background-color 0.15s ease-in-out;
    }

    .group-display-preview-row.active {
        background-color: alpha(@accent_bg_color, 0.36);
        box-shadow: inset 0 0 0 1px alpha(@accent_bg_color, 0.65);
    }

    .group-display-preview-row-title {
        font-weight: 600;
    }

    .group-display-preview-row-subtitle {
        opacity: 0.7;
        font-size: 0.9em;
    }
    """
    try:
        provider.load_from_data(css.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _GROUP_PREVIEW_CSS_INSTALLED = True
    except Exception:
        logger.debug("Failed to install group display preview CSS", exc_info=True)

def macos_third_party_terminal_available() -> bool:
    """Check if a third-party terminal is available on macOS."""
    if not is_macos():
        return False

    terminals = [
        "iterm2",
        "ghostty",
        "alacritty",
        "iterm",
        "terminator",
        "kitty",
        "tmux",
        "warp",
    ]

    applications_dir = "/Applications"
    try:
        for entry in os.listdir(applications_dir):
            lower = entry.lower()
            if any(lower.startswith(t) and entry.endswith(".app") for t in terminals):
                return True
    except Exception:
        pass

    for terminal in terminals:
        if shutil.which(terminal):
            return True

    return False


def should_hide_external_terminal_options() -> bool:
    """Check if external terminal options should be hidden.

    Returns True when running in Flatpak or when on macOS without a supported
    third-party terminal.
    """
    return is_flatpak() or (
        is_macos() and not macos_third_party_terminal_available()
    )


def should_show_force_internal_file_manager_toggle() -> bool:
    """Return True when the built-in toggle for forcing the internal manager should be shown."""

    try:
        if is_macos():
            return False
        return bool(has_native_gvfs_support())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to determine GVFS availability: %s", exc)
        return False


def should_hide_file_manager_options() -> bool:
    """Check if file manager options should be hidden.

    File manager UI should only be hidden when neither the native GVFS
    integration nor the built-in manager are available. This allows the
    Manage Files button to remain visible on platforms like macOS or Flatpak
    where the new in-app manager is preferred.
    """

    try:
        if has_native_gvfs_support():
            return False
        if has_internal_file_manager():
            return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("File manager capability detection failed: %s", exc)
    return True


class PreferencesWindow(Adw.Window):
    """Preferences dialog window"""
    
    def __init__(self, parent_window, config):
        super().__init__(transient_for=parent_window)
        self.parent_window = parent_window
        self.config = config
        self._shortcuts_row = None
        self._shortcuts_button = None
        self._group_display_sync = False
        self._tab_color_sync = False
        self._terminal_color_sync = False
        self._encoding_selection_sync = False
        self._encoding_options = []
        self._encoding_codes = []
        self._suppress_encoding_config_handler = False
        self._user_initiated_encoding_change = False
        self.force_internal_file_manager_row = None
        self.open_file_manager_externally_row = None

        self._config_signal_id = None

        if hasattr(self.config, 'connect'):
            try:
                self._config_signal_id = self.config.connect(
                    'setting-changed', self._on_config_setting_changed
                )
            except Exception:
                self._config_signal_id = None

        self.connect('destroy', self._on_destroy)

        # Use a consistent title for the window and header regardless of parent
        self._base_header_title = "Settings"

        # Set window properties with modern Adwaita structure
        self.set_title(self._base_header_title)
        self.set_default_size(820, 600)  # Ensure wide enough to see the sidebar
        
        # Ensure window decorations and controls are visible
        self.set_decorated(True)
        self.set_resizable(True)
        
        # Create custom layout with sidebar
        self.setup_navigation_layout()
        
        # Initialize the preferences UI
        self.setup_preferences()
        
        # Apply any existing color overrides
        self.apply_color_overrides()

        # Save on close to persist advanced SSH settings
        self.connect('close-request', self.on_close_request)
    
    def setup_navigation_layout(self):
        """Configure split view layout using OverlaySplitView."""
        # Adw.Window doesn't have a default titlebar, so no need to hide it
        # The headerbar with controls is in the ToolbarView below

        # Main split view container using OverlaySplitView
        self.overlay_split_view = Adw.OverlaySplitView()
        
        # Configure sidebar width properties according to OverlaySplitView API
        # Defaults: 25% fraction, 180sp min, 280sp max (using SP units by default)
        try:
            self.overlay_split_view.set_sidebar_width_fraction(0.25)
            self.overlay_split_view.set_min_sidebar_width(180)
            self.overlay_split_view.set_max_sidebar_width(280)
            # Set length unit to SP (scale-independent pixels) for responsive sizing
            if hasattr(Adw, 'LengthUnit'):
                self.overlay_split_view.set_sidebar_width_unit(Adw.LengthUnit.SP)
        except Exception as e:
            logger.debug(f"Failed to set OverlaySplitView sidebar width properties: {e}")
        
        # Show the sidebar by default
        try:
            self.overlay_split_view.set_show_sidebar(True)
        except Exception as e:
            logger.debug(f"Failed to set OverlaySplitView show_sidebar: {e}")
        
        self.set_content(self.overlay_split_view)
        
        # Add breakpoint for responsive design: collapse on small widths
        # This follows the OverlaySplitView API recommendation
        # See: https://gnome.pages.gitlab.gnome.org/libadwaita/doc/1.4/class.OverlaySplitView.html
        try:
            if hasattr(Adw, 'Breakpoint') and hasattr(Adw, 'breakpoint_condition_parse'):
                condition = Adw.breakpoint_condition_parse("max-width: 400sp")
                breakpoint = Adw.Breakpoint.new(condition)
                breakpoint.add_setter(self.overlay_split_view, "collapsed", True)
                self.add_breakpoint(breakpoint)
        except Exception as e:
            logger.debug(f"Failed to add OverlaySplitView breakpoint: {e}")

        # Sidebar list with navigation styling
        self.sidebar = Gtk.ListBox()
        self.sidebar.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.sidebar.set_activate_on_single_click(True)
        self.sidebar.add_css_class("navigation-sidebar")

        sidebar_scroller = Gtk.ScrolledWindow()
        sidebar_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sidebar_scroller.set_child(self.sidebar)
        
        # Header bar for sidebar with window controls
        # Note: OverlaySplitView automatically hides window buttons in the middle of HeaderBars
        # See: https://gnome.pages.gitlab.gnome.org/libadwaita/doc/1.4/class.OverlaySplitView.html
        self.sidebar_header_bar = Adw.HeaderBar()
        self.sidebar_header_bar.add_css_class("flat")
        
        # Sidebar title always shows "Preferences" (like GNOME Settings)
        sidebar_title_label = Gtk.Label.new(self._base_header_title)
        sidebar_title_label.add_css_class("title")
        sidebar_title_label.set_single_line_mode(True)
        sidebar_title_label.set_xalign(0.0)
        self.sidebar_header_bar.set_title_widget(sidebar_title_label)
        
        # Enable window controls on both sides - OverlaySplitView will automatically
        # hide middle buttons. This ensures close button is visible for users with
        # window controls on either left or right side.
        self.sidebar_header_bar.set_show_start_title_buttons(True)
        self.sidebar_header_bar.set_show_end_title_buttons(True)
        
        # ToolbarView with HeaderBar for sidebar
        sidebar_toolbar_view = Adw.ToolbarView()
        sidebar_toolbar_view.add_top_bar(self.sidebar_header_bar)
        sidebar_toolbar_view.set_content(sidebar_scroller)
        
        # OverlaySplitView doesn't use NavigationPage - set widget directly
        self.overlay_split_view.set_sidebar(sidebar_toolbar_view)

        # Stack for page content
        self.content_stack = Gtk.Stack()
        self.content_stack.set_hexpand(True)
        self.content_stack.set_vexpand(True)
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        content_scroller = Gtk.ScrolledWindow()
        content_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content_scroller.set_child(self.content_stack)
        # Header bar for content area with window controls
        # Note: OverlaySplitView automatically hides window buttons in the middle of HeaderBars
        # See: https://gnome.pages.gitlab.gnome.org/libadwaita/doc/1.4/class.OverlaySplitView.html
        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class("flat")
        
        self.header_title_label = Gtk.Label.new("")
        self.header_title_label.add_css_class("title")
        self.header_title_label.set_single_line_mode(True)
        self.header_title_label.set_xalign(0.0)
        self.header_bar.set_title_widget(self.header_title_label)
        
        # Enable window controls on both sides - OverlaySplitView will automatically
        # hide middle buttons. This ensures close button is visible for users with
        # window controls on either left or right side.
        self.header_bar.set_show_start_title_buttons(True)
        self.header_bar.set_show_end_title_buttons(True)

        # ToolbarView with HeaderBar (matching pattern used in file_manager_window.py)
        self.content_toolbar_view = Adw.ToolbarView()
        self.content_toolbar_view.add_top_bar(self.header_bar)
        self.content_toolbar_view.set_content(content_scroller)

        # OverlaySplitView doesn't use NavigationPage - set widget directly
        self.overlay_split_view.set_content(self.content_toolbar_view)

        # Connect sidebar selection to content stack
        self.sidebar.connect('row-selected', self.on_sidebar_row_selected)

        # Store pages for later reference
        self.pages = {}

        # Ensure the header defaults to the base preferences title
        self._update_header_title()

    def on_sidebar_row_selected(self, listbox, row):
        """Handle sidebar row selection"""
        if row is not None:
            page_name = row.get_name()
            self.content_stack.set_visible_child_name(page_name)
            if isinstance(row, Adw.ActionRow):
                title = row.get_title() or ""
                self._update_header_title(title)

    def _update_header_title(self, page_title: Optional[str] = None):
        """Update the content area header label dynamically based on selected page.
        
        Sidebar always shows "Preferences", content area shows the page title.
        """
        header_label = getattr(self, "header_title_label", None)
        if header_label:
            # Content area shows the page title, or "Preferences" if no page selected
            if page_title:
                header_label.set_label(page_title)
            else:
                header_label.set_label(self._base_header_title)

        # Keep window title static as "Preferences" only
        self.set_title(self._base_header_title)

    def add_page_to_layout(self, title, icon_name, page):
        """Add a page to the custom layout"""
        # Create sidebar row
        row = Adw.ActionRow()
        row.set_title(title)
        page_id = title.lower().replace(' ', '-')
        row.set_name(page_id)
        
        # Add icon using bundled icon helper
        from sshpilot import icon_utils
        icon = icon_utils.new_image_from_icon_name(icon_name)
        row.add_prefix(icon)
        
        # Add to sidebar
        self.sidebar.append(row)
        
        # Add page to stack
        self.content_stack.add_named(page, page_id)
        
        # Store reference
        self.pages[page_id] = page
        
        # Select first page
        if len(self.pages) == 1:
            self.sidebar.select_row(row)
            if isinstance(row, Adw.ActionRow):
                title = row.get_title() or ""
                self._update_header_title(title)
    
    def setup_preferences(self):
        """Set up preferences UI with current values"""
        try:
            # Create Terminal preferences page
            terminal_page = Adw.PreferencesPage()
            terminal_page.set_title("Terminal")
            terminal_page.set_icon_name("utilities-terminal-symbolic")
            
            # Terminal appearance group
            appearance_group = Adw.PreferencesGroup(title="Appearance")
            
            # Font selection row
            self.font_row = Adw.ActionRow()
            self.font_row.set_title("Font")
            current_font = self.config.get_setting('terminal.font', 'Monospace 12')
            self.font_row.set_subtitle(current_font)
            
            font_button = Gtk.Button()
            font_button.set_label("Choose")
            font_button.set_valign(Gtk.Align.CENTER)
            font_button.connect('clicked', self.on_font_button_clicked)
            self.font_row.add_suffix(font_button)
            
            appearance_group.add(self.font_row)
            
            # Terminal color scheme
            self.color_scheme_row = Adw.ComboRow()
            self.color_scheme_row.set_title("Color Scheme")
            self.color_scheme_row.set_subtitle("Terminal color theme")
            
            color_schemes = Gtk.StringList()
            color_schemes.append("Default")
            color_schemes.append("Black on White")
            color_schemes.append("Solarized Dark")
            color_schemes.append("Solarized Light")
            color_schemes.append("Monokai")
            color_schemes.append("Dracula")
            color_schemes.append("Nord")
            color_schemes.append("Gruvbox Dark")
            color_schemes.append("One Dark")
            color_schemes.append("Tomorrow Night")
            color_schemes.append("Material Dark")
            color_schemes.append("Rosé Pine")
            color_schemes.append("Rosé Pine Moon")
            color_schemes.append("Rosé Pine Dawn")
            color_schemes.append("Catppuccin Latte")
            color_schemes.append("Catppuccin Frappé")
            color_schemes.append("Catppuccin Macchiato")
            color_schemes.append("Catppuccin Mocha")
            self.color_scheme_row.set_model(color_schemes)
            
            # Set current color scheme from config
            current_scheme_key = self.config.get_setting('terminal.theme', 'default')
            
            # Get the display name for the current scheme key
            theme_mapping = self.get_theme_name_mapping()
            reverse_mapping = {v: k for k, v in theme_mapping.items()}
            current_scheme_display = reverse_mapping.get(current_scheme_key, 'Default')
            
            # Find the index of the current scheme in the dropdown
            scheme_names = [
                "Default", "Black on White", "Solarized Dark", "Solarized Light",
                "Monokai", "Dracula", "Nord",
                "Gruvbox Dark", "One Dark", "Tomorrow Night", "Material Dark",
                "Rosé Pine", "Rosé Pine Moon", "Rosé Pine Dawn",
                "Catppuccin Latte", "Catppuccin Frappé", "Catppuccin Macchiato", "Catppuccin Mocha"
            ]
            try:
                current_index = scheme_names.index(current_scheme_display)
                self.color_scheme_row.set_selected(current_index)
            except ValueError:
                # If the saved scheme isn't found, default to the first option
                self.color_scheme_row.set_selected(0)
                # Also update the config to use the default value
                self.config.set_setting('terminal.theme', 'default')
            
            self.color_scheme_row.connect('notify::selected', self.on_color_scheme_changed)

            appearance_group.add(self.color_scheme_row)

            self._initialize_encoding_selector(appearance_group)

            # Color scheme preview
            preview_group = Adw.PreferencesGroup(title="Preview")
            preview_group.set_margin_top(18)  # Add more spacing above "Preview" label
            
            # Create preview terminal widget
            self.color_preview_terminal = Gtk.DrawingArea()
            self.color_preview_terminal.set_draw_func(self.draw_color_preview)
            self.color_preview_terminal.set_size_request(400, 120)
            self.color_preview_terminal.add_css_class("terminal-preview")
            
            # Create a standard Adwaita container with rounded corners
            preview_container = Adw.Bin()
            preview_container.add_css_class("card")
            preview_container.set_margin_top(6)  # Reduce spacing between label and preview
            
            # Add some margin around the preview
            preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            preview_box.set_margin_top(12)
            preview_box.set_margin_bottom(12)
            preview_box.set_margin_start(12)
            preview_box.set_margin_end(12)
            preview_box.append(self.color_preview_terminal)
            
            preview_container.set_child(preview_box)
            preview_group.add(preview_container)
            appearance_group.add(preview_group)
            terminal_page.add(appearance_group)

            # Terminal backend selection group
            backend_group = Adw.PreferencesGroup(title="Backend")
            
            # Build backend choices
            self._backend_choice_data = self._build_backend_choices()
            
            # Create combo row for backend selection
            self.backend_row = Adw.ComboRow()
            self.backend_row.set_title("Terminal Backend")
            self.backend_row.set_subtitle("Choose the terminal rendering backend")
            
            # Create model from choices
            backend_model = Gtk.StringList()
            for choice in self._backend_choice_data:
                backend_model.append(choice['label'])
            self.backend_row.set_model(backend_model)
            
            # Set current backend
            current_backend = self.config.get_setting('terminal.backend', 'vte')
            # Reset to VTE if pyxterm is configured on macOS (not supported)
            if is_macos() and current_backend and current_backend.lower() == 'pyxterm':
                current_backend = 'vte'
                self.config.set_setting('terminal.backend', 'vte')
            current_index = 0
            for i, choice in enumerate(self._backend_choice_data):
                if choice['id'] == current_backend:
                    current_index = i
                    break
            self.backend_row.set_selected(current_index)
            self._backend_last_valid_index = current_index
            self._update_backend_row_subtitle(current_index)
            
            # Connect change handler
            self.backend_row.connect('notify::selected', self._on_backend_row_changed)
            
            backend_group.add(self.backend_row)
            terminal_page.add(backend_group)

            keyboard_group = Adw.PreferencesGroup(title="Keyboard")

            self.pass_through_switch = Adw.SwitchRow()
            self.pass_through_switch.set_title("Terminal Shortcut Pass-through")
            self.pass_through_switch.set_subtitle(
                "Disable all keyboard shortcuts, pass all key events directly to terminal"
            )
            try:
                pass_through_active = bool(
                    self.config.get_setting('terminal.pass_through_mode', False)
                )
            except Exception:
                pass_through_active = False
            self._pass_through_enabled = pass_through_active
            self.pass_through_switch.set_active(pass_through_active)
            self.pass_through_switch.connect('notify::active', self.on_pass_through_mode_toggled)
            keyboard_group.add(self.pass_through_switch)

            terminal_page.add(keyboard_group)

            # Mouse behavior group
            mouse_group = Adw.PreferencesGroup(title="Mouse")

            self.copy_on_select_switch = Adw.SwitchRow()
            self.copy_on_select_switch.set_title("Copy on selection")
            self.copy_on_select_switch.set_subtitle(
                "Automatically copy selected text to the clipboard"
            )
            try:
                copy_on_select_active = bool(
                    self.config.get_setting('terminal.copy_on_select', False)
                )
            except Exception:
                copy_on_select_active = False
            self.copy_on_select_switch.set_active(copy_on_select_active)
            self.copy_on_select_switch.connect('notify::active', self.on_copy_on_select_toggled)
            mouse_group.add(self.copy_on_select_switch)

            self.paste_on_right_click_switch = Adw.SwitchRow()
            self.paste_on_right_click_switch.set_title("Paste on right-click")
            self.paste_on_right_click_switch.set_subtitle(
                "Right-click pastes; Shift+right-click opens the menu"
            )
            try:
                paste_on_right_click_active = bool(
                    self.config.get_setting('terminal.paste_on_right_click', False)
                )
            except Exception:
                paste_on_right_click_active = False
            self.paste_on_right_click_switch.set_active(paste_on_right_click_active)
            self.paste_on_right_click_switch.connect(
                'notify::active', self.on_paste_on_right_click_toggled
            )
            mouse_group.add(self.paste_on_right_click_switch)

            terminal_page.add(mouse_group)

            # Preferred Terminal group (shown when external terminals are available)
            if not should_hide_external_terminal_options():
                terminal_choice_group = Adw.PreferencesGroup(title="Preferred Terminal")
                
                # Radio buttons for terminal choice
                self.builtin_terminal_radio = Gtk.CheckButton(label="Use built-in terminal")
                self.builtin_terminal_radio.set_can_focus(True)
                self.external_terminal_radio = Gtk.CheckButton(label="Use other terminal")
                self.external_terminal_radio.set_can_focus(True)
                
                # Make them behave like radio buttons
                self.external_terminal_radio.set_group(self.builtin_terminal_radio)
                
                # Set current preference
                use_external = self.config.get_setting('use-external-terminal', False)
                if use_external:
                    self.external_terminal_radio.set_active(True)
                else:
                    self.builtin_terminal_radio.set_active(True)
                
                # Connect radio button changes
                self.builtin_terminal_radio.connect('toggled', self.on_terminal_choice_changed)
                self.external_terminal_radio.connect('toggled', self.on_terminal_choice_changed)
                
                # Add radio buttons to group
                terminal_choice_group.add(self.builtin_terminal_radio)
                terminal_choice_group.add(self.external_terminal_radio)
                
                # External terminal dropdown and custom path
                self.external_terminal_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                self.external_terminal_box.set_margin_start(24)
                self.external_terminal_box.set_margin_top(6)
                self.external_terminal_box.set_margin_bottom(6)
                
                # Terminal dropdown
                self.terminal_dropdown = Gtk.DropDown()
                self.terminal_dropdown.set_can_focus(True)
                
                # Populate dropdown with available terminals
                self._populate_terminal_dropdown()
                
                # Set current selection
                current_terminal = self.config.get_setting('external-terminal', 'gnome-terminal')
                
                # Connect dropdown changes
                self.terminal_dropdown.connect('notify::selected', self.on_terminal_dropdown_changed)
                
                # Custom path entry (initially hidden)
                self.custom_terminal_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                self.custom_terminal_entry = Gtk.Entry()
                self.custom_terminal_entry.set_placeholder_text("/usr/bin/gnome-terminal")
                self.custom_terminal_entry.set_can_focus(True)
                
                # Set current custom path if any
                custom_path = self.config.get_setting('custom-terminal-path', '')
                if custom_path:
                    self.custom_terminal_entry.set_text(custom_path)
                
                # Connect custom path changes
                self.custom_terminal_entry.connect('changed', self.on_custom_terminal_path_changed)
                
                self.custom_terminal_box.append(self.custom_terminal_entry)
                
                # Add dropdown and custom path to box
                self.external_terminal_box.append(self.terminal_dropdown)
                self.external_terminal_box.append(self.custom_terminal_box)
                
                # Now set the dropdown selection and show/hide custom path entry
                self._set_terminal_dropdown_selection(current_terminal)
                
                # Initial sensitivity will be set by radio button state
                
                # Add to group
                terminal_choice_group.add(self.external_terminal_box)
                
                # Set initial sensitivity based on radio button state
                self.external_terminal_box.set_sensitive(self.external_terminal_radio.get_active())
                
                terminal_page.add(terminal_choice_group)
            
            # Create Groups preferences page
            groups_page = Adw.PreferencesPage()
            groups_page.set_title("Groups")
            groups_page.set_icon_name("folder-open-symbolic")

            group_appearance_group = Adw.PreferencesGroup(title="Group Appearance")

            # Sidebar group color display mode
            self._group_color_display_values = ['fill', 'badge']
            self.group_color_display_row = Adw.ComboRow()
            self.group_color_display_row.set_title("Sidebar Group Colors")
            self.group_color_display_row.set_subtitle(
                "Choose how group colors are shown in the sidebar"
            )

            color_display_options = Gtk.StringList()
            color_display_options.append("Colored Rows")
            color_display_options.append("Color Badges")
            self.group_color_display_row.set_model(color_display_options)

            current_mode = 'fill'
            try:
                current_mode = str(
                    self.config.get_setting('ui.group_color_display', 'fill')
                ).lower()
            except Exception:
                current_mode = 'fill'
            if current_mode not in self._group_color_display_values:
                current_mode = 'fill'

            self._group_display_sync = True
            try:
                self.group_color_display_row.set_selected(
                    self._group_color_display_values.index(current_mode)
                )
            finally:
                self._group_display_sync = False

            self.group_color_display_row.connect(
                'notify::selected', self.on_group_color_display_changed
            )

            group_appearance_group.add(self.group_color_display_row)

            # Toggle for coloring tabs using group colors
            self.tab_group_color_row = Adw.SwitchRow()
            self.tab_group_color_row.set_title("Show Group Color in Tabs")
            self.tab_group_color_row.set_subtitle(
                "Show the selected group's color badge in the terminal tabs"
            )
            try:
                tab_pref = bool(
                    self.config.get_setting('ui.use_group_color_in_tab', False)
                )
            except Exception:
                tab_pref = False
            self.tab_group_color_row.set_active(tab_pref)
            self.tab_group_color_row.connect(
                'notify::active', self.on_use_group_color_in_tab_toggled
            )
            group_appearance_group.add(self.tab_group_color_row)

            # Toggle for applying group colors inside terminals
            self.terminal_group_color_row = Adw.SwitchRow()
            self.terminal_group_color_row.set_title("Use Group Color in Terminals")
            self.terminal_group_color_row.set_subtitle(
                "Use parent group's color as terminal background"
            )
            try:
                terminal_pref = bool(
                    self.config.get_setting('ui.use_group_color_in_terminal', False)
                )
            except Exception:
                terminal_pref = False
            self.terminal_group_color_row.set_active(terminal_pref)
            self.terminal_group_color_row.connect(
                'notify::active', self.on_use_group_color_in_terminal_toggled
            )
            group_appearance_group.add(self.terminal_group_color_row)


            groups_page.add(group_appearance_group)

            # Group display layout section (shown after appearance options)
            group_layout_group = Adw.PreferencesGroup(title="Group Layout")

            self._group_display_preview_rows = {}
            _install_group_display_preview_css()

            self._group_display_modes = ['fullwidth', 'nested']
            self.group_display_toggle_row = Adw.ActionRow()
            self.group_display_toggle_row.set_title("Group Display")
            self.group_display_toggle_row.set_subtitle(
                "Choose how grouped connections appear in the sidebar"
            )
            self.group_display_toggle_row.set_activatable(False)
            try:
                self.group_display_toggle_row.set_focusable(False)
            except Exception:
                pass

            self.group_display_toggle_group = None
            self._group_display_toggle_controller = None

            if hasattr(Adw, 'ToggleGroup') and hasattr(Adw.ToggleGroup, 'new'):
                toggle_group = Adw.ToggleGroup.new()
                toggle_group.set_orientation(Gtk.Orientation.HORIZONTAL)
                toggle_group.add_css_class('linked')
                toggle_group.set_hexpand(True)
                try:
                    toggle_group.set_homogeneous(True)
                except Exception:
                    pass

                self.group_display_toggle_fullwidth = Adw.Toggle.new()
                self.group_display_toggle_fullwidth.props.name = 'fullwidth'
                self.group_display_toggle_fullwidth.set_label('Fullwidth')

                self.group_display_toggle_nested = Adw.Toggle.new()
                self.group_display_toggle_nested.props.name = 'nested'
                self.group_display_toggle_nested.set_label('Nested')

                toggle_group.add(self.group_display_toggle_fullwidth)
                toggle_group.add(self.group_display_toggle_nested)
                self.group_display_toggle_row.set_child(toggle_group)

                self.group_display_toggle_group = toggle_group
                self._group_display_toggle_controller = toggle_group
            else:
                fallback_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                fallback_box.add_css_class('linked')
                fallback_box.set_hexpand(True)

                self.group_display_toggle_fullwidth = Gtk.ToggleButton(label='Fullwidth')
                self.group_display_toggle_fullwidth.props.name = 'fullwidth'
                self.group_display_toggle_fullwidth.set_hexpand(True)

                self.group_display_toggle_nested = Gtk.ToggleButton(label='Nested')
                self.group_display_toggle_nested.props.name = 'nested'
                self.group_display_toggle_nested.set_hexpand(True)

                fallback_box.append(self.group_display_toggle_fullwidth)
                fallback_box.append(self.group_display_toggle_nested)
                self.group_display_toggle_row.set_child(fallback_box)

                buttons = {
                    'fullwidth': self.group_display_toggle_fullwidth,
                    'nested': self.group_display_toggle_nested,
                }
                self._group_display_toggle_controller = _GroupDisplayToggleFallback(buttons)

            current_display_mode = 'nested'
            try:
                current_display_mode = str(
                    self.config.get_setting('ui.group_row_display', 'nested')
                ).lower()
            except Exception:
                current_display_mode = 'nested'
            if current_display_mode not in self._group_display_modes:
                current_display_mode = 'nested'

            self._group_display_toggle_sync = True
            try:
                if self._group_display_toggle_controller:
                    self._group_display_toggle_controller.set_active_name(current_display_mode)
            finally:
                self._group_display_toggle_sync = False

            if isinstance(self._group_display_toggle_controller, _GroupDisplayToggleFallback):
                self._group_display_toggle_controller.connect(
                    self.on_group_row_display_changed
                )
            elif self.group_display_toggle_group is not None:
                self.group_display_toggle_group.connect(
                    'notify::active-name', self.on_group_row_display_changed
                )

            group_layout_group.add(self.group_display_toggle_row)

            # Preview showing both layout styles
            self.group_display_preview_row = Adw.ActionRow()
            self.group_display_preview_row.set_title("Layout Preview")
            self.group_display_preview_row.set_activatable(False)
            try:
                self.group_display_preview_row.set_focusable(False)
            except Exception:
                pass

            preview_stack = Gtk.Stack()
            preview_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
            preview_stack.set_transition_duration(150)

            preview_stack.set_margin_top(4)
            preview_stack.set_margin_bottom(4)

            preview_fullwidth_wrapper, preview_fullwidth_row = self._create_group_display_preview(
                'fullwidth', 'Fullwidth'
            )
            preview_nested_wrapper, preview_nested_row = self._create_group_display_preview(
                'nested', 'Nested'
            )

            preview_stack.add_named(preview_fullwidth_wrapper, 'fullwidth')
            preview_stack.add_named(preview_nested_wrapper, 'nested')

            self.group_display_preview_row.set_child(preview_stack)
            self._group_display_preview_rows = {
                'fullwidth': preview_fullwidth_row,
                'nested': preview_nested_row,
            }
            self._group_display_preview_stack = preview_stack

            self._update_group_display_preview(current_display_mode)

            group_layout_group.add(self.group_display_preview_row)
            groups_page.add(group_layout_group)

            # Create Interface preferences page
            interface_page = Adw.PreferencesPage()
            interface_page.set_title("Interface")
            interface_page.set_icon_name("applications-graphics-symbolic")

            # App startup behavior
            startup_group = Adw.PreferencesGroup(title="App Startup")

            # Radio buttons for startup behavior
            self.terminal_startup_radio = Gtk.CheckButton(label="Show Terminal")
            self.terminal_startup_radio.set_can_focus(True)
            self.welcome_startup_radio = Gtk.CheckButton(label="Show Start Page")
            self.welcome_startup_radio.set_can_focus(True)
            self.previous_session_startup_radio = Gtk.CheckButton(label="Restore previous session")
            self.previous_session_startup_radio.set_can_focus(True)
            self.saved_session_startup_radio = Gtk.CheckButton(label="Open a saved session")
            self.saved_session_startup_radio.set_can_focus(True)

            # Make them behave like radio buttons
            self.welcome_startup_radio.set_group(self.terminal_startup_radio)
            self.previous_session_startup_radio.set_group(self.terminal_startup_radio)
            self.saved_session_startup_radio.set_group(self.terminal_startup_radio)

            # Set current preference (default to welcome/start page)
            startup_behavior = self.config.get_setting('app-startup-behavior', 'welcome')
            if startup_behavior == 'terminal':
                self.terminal_startup_radio.set_active(True)
            elif startup_behavior == 'previous-session':
                self.previous_session_startup_radio.set_active(True)
            elif startup_behavior == 'saved-session':
                self.saved_session_startup_radio.set_active(True)
            else:
                self.welcome_startup_radio.set_active(True)

            # Selector for which saved session to open on startup. This is built
            # as a plain (non-row) widget so it stacks directly below the
            # "Open a saved session" radio rather than being hoisted into the
            # group's internal list box above the bare check buttons.
            self._startup_session_names = []
            try:
                session_manager = getattr(self.parent_window, 'session_manager', None)
                if session_manager is not None:
                    self._startup_session_names = list(session_manager.list_session_names())
            except Exception:
                self._startup_session_names = []

            session_model = Gtk.StringList()
            if self._startup_session_names:
                for name in self._startup_session_names:
                    session_model.append(name)
            else:
                session_model.append("(no saved sessions)")

            self.startup_session_row = Gtk.DropDown()
            self.startup_session_row.set_model(session_model)
            self.startup_session_row.set_valign(Gtk.Align.CENTER)

            self.startup_session_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            self.startup_session_box.set_margin_start(24)
            self.startup_session_box.set_margin_top(2)
            session_label = Gtk.Label(label="Saved session")
            session_label.set_xalign(0)
            session_label.set_hexpand(True)
            self.startup_session_box.append(session_label)
            self.startup_session_box.append(self.startup_session_row)

            selected_session = self.config.get_setting('app-startup-session-name', '')
            if selected_session in self._startup_session_names:
                self.startup_session_row.set_selected(
                    self._startup_session_names.index(selected_session)
                )
            self.startup_session_box.set_sensitive(
                bool(self._startup_session_names)
                and self.saved_session_startup_radio.get_active()
            )

            # Connect radio button changes
            self.terminal_startup_radio.connect('toggled', self.on_startup_behavior_changed)
            self.welcome_startup_radio.connect('toggled', self.on_startup_behavior_changed)
            self.previous_session_startup_radio.connect('toggled', self.on_startup_behavior_changed)
            self.saved_session_startup_radio.connect('toggled', self.on_startup_behavior_changed)
            self.startup_session_row.connect('notify::selected', self.on_startup_session_changed)

            # Add radio buttons to group, with the saved-session selector placed
            # immediately below its corresponding radio option.
            startup_group.add(self.terminal_startup_radio)
            startup_group.add(self.welcome_startup_radio)
            startup_group.add(self.previous_session_startup_radio)
            startup_group.add(self.saved_session_startup_radio)
            startup_group.add(self.startup_session_box)

            interface_page.add(startup_group)

            # Appearance group
            interface_appearance_group = Adw.PreferencesGroup(title="Appearance")

            # Theme selection
            self.theme_row = Adw.ComboRow()
            self.theme_row.set_title("Application Theme")
            self.theme_row.set_subtitle("Choose light, dark, or follow system theme")

            themes = Gtk.StringList()
            themes.append("Follow System")
            themes.append("Light")
            themes.append("Dark")
            self.theme_row.set_model(themes)
            
            # Load saved theme preference
            saved_theme = self.config.get_setting('app-theme', 'default')
            theme_mapping = {'default': 0, 'light': 1, 'dark': 2}
            self.theme_row.set_selected(theme_mapping.get(saved_theme, 0))

            self.theme_row.connect('notify::selected', self.on_theme_changed)

            interface_appearance_group.add(self.theme_row)


            # Color overrides section
            color_override_group = Adw.PreferencesGroup(title="Color Overrides")
            color_override_group.set_description("Override the accent color")

            # Accent color override
            self.accent_color_row = Adw.ActionRow()
            self.accent_color_row.set_title("Accent Color")
            self.accent_color_row.set_subtitle("Override the accent color for highlights")

            self.accent_color_button = Gtk.ColorButton()
            self.accent_color_button.set_use_alpha(False)
            self.accent_color_button.set_tooltip_text("Choose accent color")
            self.accent_color_button.set_valign(Gtk.Align.CENTER)
            self.accent_color_button.set_size_request(60, 32)
            self.accent_color_button.connect('color-set', self.on_accent_color_changed)
            self.accent_color_row.add_suffix(self.accent_color_button)
            color_override_group.add(self.accent_color_row)
            
            # Reset colors button
            reset_colors_row = Adw.ActionRow()
            reset_colors_row.set_title("Reset to Default")
            reset_colors_row.set_subtitle("Remove the accent override and use the system accent")
            
            reset_button = Gtk.Button()
            reset_button.set_label("Reset")
            reset_button.add_css_class("destructive-action")
            reset_button.set_valign(Gtk.Align.CENTER)
            reset_button.connect('clicked', self.on_reset_colors_clicked)
            reset_colors_row.add_suffix(reset_button)
            color_override_group.add(reset_colors_row)

            interface_page.add(interface_appearance_group)
            interface_page.add(color_override_group)

            # Initialize color button states
            self.refresh_color_buttons()
            
            # Window group
            window_group = Adw.PreferencesGroup(title="Window")

            # Remember window size switch
            remember_size_switch = Adw.SwitchRow()
            remember_size_switch.set_title("Remember Window Size")
            remember_size_switch.set_subtitle("Restore window size on startup")
            remember_size_switch.set_active(True)
            
            window_group.add(remember_size_switch)
            interface_page.add(window_group)

            # Sidebar group (at bottom of Interface page)
            sidebar_group = Adw.PreferencesGroup(title="Sidebar")
            
            # Maximum width slider
            max_width_row = Adw.ActionRow()
            max_width_row.set_title("Maximum Width")
            
            # Load saved value or use default
            saved_max_width = self.config.get_setting('ui.max-sidebar-width', 280)
            
            # Create a scale/slider for max width (100-800 sp)
            max_width_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 100, 800, 10)
            max_width_scale.set_draw_value(True)
            max_width_scale.set_value_pos(Gtk.PositionType.RIGHT)
            max_width_scale.set_size_request(200, -1)
            max_width_scale.set_valign(Gtk.Align.CENTER)
            max_width_scale.set_value(float(saved_max_width))
            
            # Update subtitle with current value
            def update_subtitle(value):
                max_width_row.set_subtitle(f"Adjust the maximum width of the sidebar ({int(value)} sp)")
            
            update_subtitle(saved_max_width)
            
            # Connect change handler
            def on_max_width_changed(scale):
                value = int(scale.get_value())
                update_subtitle(value)
                self.config.set_setting('ui.max-sidebar-width', value)
                # Update main window if available
                if self.parent_window and hasattr(self.parent_window, 'update_sidebar_max_width'):
                    self.parent_window.update_sidebar_max_width(value)
            
            max_width_scale.connect('value-changed', on_max_width_changed)
            
            max_width_row.add_suffix(max_width_scale)
            sidebar_group.add(max_width_row)

            flat_rows_switch = Adw.SwitchRow()
            flat_rows_switch.set_title(_("Flat Sidebar Rows"))
            flat_rows_switch.set_subtitle(
                _("Use flat list rows instead of cards in the connection sidebar")
            )
            flat_rows_switch.set_active(
                bool(self.config.get_setting('ui.sidebar_flat_rows', False))
            )
            flat_rows_switch.connect(
                'notify::active', self.on_sidebar_flat_rows_changed
            )
            sidebar_group.add(flat_rows_switch)
            
            # Display user@hostname toggle
            show_user_hostname_switch = Adw.SwitchRow()
            show_user_hostname_switch.set_title("Display user@hostname")
            show_user_hostname_switch.set_subtitle("Show username@hostname in connection rows")
            show_user_hostname_switch.set_active(
                self.config.get_setting('ui.sidebar_show_user_hostname', True)
            )
            show_user_hostname_switch.connect('notify::active', self.on_sidebar_show_user_hostname_changed)
            sidebar_group.add(show_user_hostname_switch)
            
            # Display connection count in groups toggle
            show_group_count_switch = Adw.SwitchRow()
            show_group_count_switch.set_title("Display Connection Count in Groups")
            show_group_count_switch.set_subtitle("Show the number of connections in each group")
            show_group_count_switch.set_active(
                self.config.get_setting('ui.sidebar_show_group_count', True)
            )
            show_group_count_switch.connect('notify::active', self.on_sidebar_show_group_count_changed)
            sidebar_group.add(show_group_count_switch)
            
            # Display connection status toggle
            show_status_switch = Adw.SwitchRow()
            show_status_switch.set_title("Display Connection Status")
            show_status_switch.set_subtitle("Show connection status indicator in connection rows")
            show_status_switch.set_active(
                self.config.get_setting('ui.sidebar_show_connection_status', True)
            )
            show_status_switch.connect('notify::active', self.on_sidebar_show_connection_status_changed)
            sidebar_group.add(show_status_switch)
            
            # Display port forwarding labels toggle
            show_port_forwarding_switch = Adw.SwitchRow()
            show_port_forwarding_switch.set_title("Display Port Forwarding Labels")
            show_port_forwarding_switch.set_subtitle("Show port forwarding indicators (L/R/D) in connection rows")
            show_port_forwarding_switch.set_active(
                self.config.get_setting('ui.sidebar_show_port_forwarding', True)
            )
            show_port_forwarding_switch.connect('notify::active', self.on_sidebar_show_port_forwarding_changed)
            sidebar_group.add(show_port_forwarding_switch)
            
            # Display connection icon toggle
            show_connection_icon_switch = Adw.SwitchRow()
            show_connection_icon_switch.set_title("Display Connection Icon")
            show_connection_icon_switch.set_subtitle("Show the computer icon in connection rows")
            show_connection_icon_switch.set_active(
                self.config.get_setting('ui.sidebar_show_connection_icon', True)
            )
            show_connection_icon_switch.connect('notify::active', self.on_sidebar_show_connection_icon_changed)
            sidebar_group.add(show_connection_icon_switch)
            
            interface_page.add(sidebar_group)

            # Sidebar behavior
            sidebar_behavior_group = Adw.PreferencesGroup(title="Sidebar behavior")

            hide_on_startup_switch = Adw.SwitchRow()
            hide_on_startup_switch.set_title("Hide Sidebar on Startup")
            hide_on_startup_switch.set_subtitle("Start with the sidebar collapsed")
            hide_on_startup_switch.set_active(
                bool(self.config.get_setting('ui.sidebar_hide_on_startup', False))
            )
            hide_on_startup_switch.connect(
                'notify::active',
                lambda r, _p: self.config.set_setting('ui.sidebar_hide_on_startup', bool(r.get_active())),
            )
            sidebar_behavior_group.add(hide_on_startup_switch)

            hide_on_terminal_switch = Adw.SwitchRow()
            hide_on_terminal_switch.set_title("Hide When a Terminal Opens")
            hide_on_terminal_switch.set_subtitle("Collapse the sidebar when any session opens, including local terminals")
            hide_on_terminal_switch.set_active(
                bool(self.config.get_setting('ui.sidebar_hide_on_terminal_open', False))
            )
            hide_on_terminal_switch.connect(
                'notify::active',
                lambda r, _p: self.config.set_setting('ui.sidebar_hide_on_terminal_open', bool(r.get_active())),
            )
            sidebar_behavior_group.add(hide_on_terminal_switch)

            show_when_no_tabs_switch = Adw.SwitchRow()
            show_when_no_tabs_switch.set_title("Show When No Tab Is Open")
            show_when_no_tabs_switch.set_subtitle("Reveal the sidebar when returning to the welcome screen")
            show_when_no_tabs_switch.set_active(
                bool(self.config.get_setting('ui.sidebar_show_when_no_tabs', False))
            )
            show_when_no_tabs_switch.connect(
                'notify::active',
                lambda r, _p: self.config.set_setting('ui.sidebar_show_when_no_tabs', bool(r.get_active())),
            )
            sidebar_behavior_group.add(show_when_no_tabs_switch)

            interface_page.add(sidebar_behavior_group)

            # Header bar button visibility
            headerbar_group = Adw.PreferencesGroup(title="Header Bar Buttons")

            def _add_headerbar_switch(title, subtitle, key, default=True):
                row = Adw.SwitchRow()
                row.set_title(title)
                row.set_subtitle(subtitle)
                row.set_active(bool(self.config.get_setting(key, default)))

                def _on_toggled(r, _p, _k=key):
                    self.config.set_setting(_k, bool(r.get_active()))
                    if self.parent_window and hasattr(self.parent_window, 'update_headerbar_buttons'):
                        self.parent_window.update_headerbar_buttons()

                row.connect('notify::active', _on_toggled)
                headerbar_group.add(row)

            _add_headerbar_switch(
                "Split View Button",
                "Show the split-view button (the grid icon that starts a split view)",
                'ui.headerbar_show_split_view',
                default=False,
            )
            _add_headerbar_switch(
                "Commands Button",
                "Show the command snippets toggle button",
                'ui.headerbar_show_commands',
            )
            _add_headerbar_switch(
                "Theme Menu",
                "Show the application theme menu beside the commands button",
                'ui.headerbar_show_theme_toggle',
            )
            _add_headerbar_switch(
                "Local Terminal Button",
                "Show the button that opens a local terminal",
                'ui.headerbar_show_local_terminal',
            )

            interface_page.add(headerbar_group)

            # Tips group at the bottom of the Interface page. Lets users
            # re-enable the terminal tips banner after dismissing it with
            # "Don't show again".
            tips_group = Adw.PreferencesGroup(title="Tips")
            show_tips_switch = Adw.SwitchRow()
            show_tips_switch.set_title("Show Terminal Tips")
            show_tips_switch.set_subtitle("Show usage tips in a banner when a terminal opens")
            show_tips_switch.set_active(
                bool(self.config.get_setting('terminal.show_tips', True))
            )
            show_tips_switch.connect('notify::active', self.on_show_tips_toggled)
            tips_group.add(show_tips_switch)
            interface_page.add(tips_group)

            # Shortcuts page with inline editor
            shortcuts_page = Adw.PreferencesPage()
            shortcuts_page.set_title("Shortcuts")
            shortcuts_page.set_icon_name("preferences-desktop-keyboard-shortcuts-symbolic")

            shortcuts_intro_group = Adw.PreferencesGroup(title="Keyboard Shortcuts")

            shortcuts_button_row = Adw.ActionRow()
            shortcuts_button_row.set_title("Shortcut Overview")
            shortcuts_button_row.set_subtitle("Open the shortcuts window for a full reference")

            shortcuts_button = Gtk.Button(label="Open")
            try:
                shortcuts_button.add_css_class("flat")
            except Exception:
                pass
            shortcuts_button.set_valign(Gtk.Align.CENTER)
            shortcuts_button.connect('clicked', self.on_view_shortcuts_clicked)
            shortcuts_button_row.add_suffix(shortcuts_button)
            shortcuts_button_row.set_activatable_widget(shortcuts_button)

            self._shortcuts_row = shortcuts_button_row
            self._shortcuts_button = shortcuts_button

            shortcuts_intro_group.add(shortcuts_button_row)
            shortcuts_page.add(shortcuts_intro_group)

            try:
                self.shortcuts_editor_page = ShortcutsPreferencesPage(
                    parent_widget=self.parent_window,
                    app=self.parent_window.get_application() if self.parent_window else None,
                    config=self.config,
                    owner_window=self.parent_window,
                )

                groups_added = len(list(self.shortcuts_editor_page.iter_groups()))
                self.shortcuts_editor_page.create_editor_widget()

                notice_widget = getattr(
                    self.shortcuts_editor_page, 'get_pass_through_notice_widget', None
                )
                if callable(notice_widget):
                    notice_widget = notice_widget()
                if notice_widget is not None:
                    parent = notice_widget.get_parent()
                    if parent is not None:
                        remove_method = getattr(parent, 'remove', None)
                        if callable(remove_method):
                            remove_method(notice_widget)
                        else:
                            remove_child = getattr(parent, 'remove_child', None)
                            if callable(remove_child):
                                remove_child(notice_widget)

                    # Create a preferences group for the notice widget
                    # Adw.PreferencesGroup accepts Adw.ActionRow, so we need to wrap it
                    notice_group = Adw.PreferencesGroup()
                    
                    # Try to add the widget directly first (works for some widget types)
                    try:
                        notice_group.add(notice_widget)
                    except (TypeError, AttributeError):
                        # If direct add fails, wrap in an ActionRow
                        # For Banner/Label widgets, we'll create a simple row
                        notice_row = Adw.ActionRow()
                        notice_row.set_activatable(False)
                        notice_row.set_selectable(False)
                        # Set the notice widget as the child of the row
                        notice_row.set_child(notice_widget)
                        notice_group.add(notice_row)
                    
                    shortcuts_page.add(notice_group)

                for group in self.shortcuts_editor_page.iter_groups():
                    parent = group.get_parent()
                    if parent is not None:
                        remove_method = getattr(parent, 'remove', None)
                        if callable(remove_method):
                            remove_method(group)
                        else:
                            remove_child = getattr(parent, 'remove_child', None)
                            if callable(remove_child):
                                remove_child(group)
                    shortcuts_page.add(group)
                logger.debug(
                    "Added shortcut editor widget with %d groups", groups_added
                )

                try:
                    self.shortcuts_editor_page.set_pass_through_enabled(
                        getattr(self, '_pass_through_enabled', False)
                    )
                except Exception:
                    pass

                logger.info(f"Shortcut editor successfully added to preferences with {groups_added} groups")
            except Exception as e:
                logger.error(f"Failed to create shortcut editor: {e}", exc_info=True)
                # Add a fallback message to the shortcuts page
                fallback_group = Adw.PreferencesGroup(title="Shortcut Editor")
                fallback_row = Adw.ActionRow()
                fallback_row.set_title("Shortcut Editor Unavailable")
                fallback_row.set_subtitle("The shortcut editor could not be loaded. Please check the logs for details.")
                fallback_group.add(fallback_row)
                shortcuts_page.add(fallback_group)

            # Advanced SSH settings
            advanced_page = Adw.PreferencesPage()
            advanced_page.set_title("Advanced")
            advanced_page.set_icon_name("applications-system-symbolic")

            # Operation mode selection
            operation_group = Adw.PreferencesGroup(title="Operation Mode")


            # Default mode row
            self.default_mode_row = Adw.ActionRow()
            self.default_mode_row.set_title("Default Mode")
            self.default_mode_row.set_subtitle("SSH Pilot loads and modifies ~/.ssh/config")
            self.default_mode_radio = Gtk.CheckButton()


            # Isolated mode row
            self.isolated_mode_row = Adw.ActionRow()
            self.isolated_mode_row.set_title("Isolated Mode")
            config_path = get_config_dir()
            self.isolated_mode_row.set_subtitle(
                f"SSH Pilot stores its configuration file in {config_path}/"
            )
            self.isolated_mode_radio = Gtk.CheckButton()

            # Group the radios for exclusive selection
            self.isolated_mode_radio.set_group(self.default_mode_radio)

            self.default_mode_row.add_prefix(self.default_mode_radio)
            self.default_mode_row.set_activatable_widget(self.default_mode_radio)

            operation_group.add(self.default_mode_row)

            self.isolated_mode_row.add_prefix(self.isolated_mode_radio)
            self.isolated_mode_row.set_activatable_widget(self.isolated_mode_radio)
            operation_group.add(self.isolated_mode_row)

            use_isolated = bool(self.config.get_setting('ssh.use_isolated_config', False))
            self.isolated_mode_radio.set_active(use_isolated)
            self.default_mode_radio.set_active(not use_isolated)

            self.default_mode_radio.connect('toggled', self.on_operation_mode_toggled)
            self.isolated_mode_radio.connect('toggled', self.on_operation_mode_toggled)

            self._update_operation_mode_styles()

            advanced_page.add(operation_group)

            # Secret storage backend selection
            secrets_group = Adw.PreferencesGroup(
                title="Secret Storage",
                description="Where sshPilot stores passwords and key passphrases. "
                            "Switching does not migrate existing secrets.",
            )
            self.secret_backend_row = Adw.ComboRow()
            self.secret_backend_row.set_title("Secret storage backend")
            try:
                from .secret_storage import get_secret_manager
                _mgr = get_secret_manager()
                available = set(_mgr.available_backends())
                registered = _mgr.registered_backends()
            except Exception:
                available = set()
                registered = []
            current_backend = str(self.config.get_setting('secrets.backend', 'auto'))
            backend_labels = {
                'libsecret': _("System keyring (libsecret)"),
                'keyring': _("System keyring"),
                'pass': _("pass (password store)"),
                'bitwarden': _("Bitwarden"),
                'vaultwarden': _("Vaultwarden"),
                'agent': _("SSH Agent Only"),
            }
            # Offer EVERY registered backend (not just the available ones) so the
            # user can pick e.g. Vaultwarden in order to reveal its server-URL row
            # (it isn't "available" until that URL is set). Unavailable ones are
            # labelled so the choice is honest.
            preferred_order = ['libsecret', 'keyring', 'pass',
                               'bitwarden', 'vaultwarden', 'agent']
            ordered_names = [n for n in preferred_order if n in registered]
            ordered_names += [n for n in registered if n not in preferred_order]
            self._secret_backend_ids = ['auto'] + ordered_names
            secret_model = Gtk.StringList()
            secret_model.append(_("Automatic"))
            for name in ordered_names:
                label = backend_labels.get(name, name)
                if name not in available:
                    label = _("{backend} (unavailable)").format(backend=label)
                secret_model.append(label)
            # Keep an unknown saved backend visible so the UI matches the config.
            if current_backend not in self._secret_backend_ids:
                self._secret_backend_ids.append(current_backend)
                label = backend_labels.get(current_backend, current_backend)
                secret_model.append(_("{backend} (unavailable)").format(backend=label))
            self.secret_backend_row.set_model(secret_model)
            try:
                current_index = self._secret_backend_ids.index(current_backend)
            except ValueError:
                current_index = 0
            self.secret_backend_row.set_selected(current_index)
            self.secret_backend_row.connect('notify::selected', self.on_secret_backend_changed)
            secrets_group.add(self.secret_backend_row)

            # Vaultwarden self-hosted server URL — only shown when the Vaultwarden
            # backend is selected (toggled by _update_secret_rows_visibility).
            self.vaultwarden_server_row = Adw.EntryRow(title=_("Vaultwarden server URL"))
            self.vaultwarden_server_row.set_text(
                str(self.config.get_setting('secrets.vaultwarden.server', '') or '')
            )
            self.vaultwarden_server_row.connect('changed', self.on_vaultwarden_server_changed)
            secrets_group.add(self.vaultwarden_server_row)

            # Idle minutes before a session-backed vault (Bitwarden/Vaultwarden)
            # re-asks for the master password. 0 = keep unlocked until app exit.
            self.secret_session_timeout_row = Adw.SpinRow.new_with_range(0, 1440, 5)
            self.secret_session_timeout_row.set_title(_("Vault lock timeout (minutes)"))
            self.secret_session_timeout_row.set_subtitle(
                _("Re-ask for the master password after this idle time. 0 = until app exits.")
            )
            self.secret_session_timeout_row.set_value(
                int(self.config.get_setting('secrets.session_timeout', 0) or 0)
            )
            self.secret_session_timeout_row.connect(
                'notify::value', self.on_secret_session_timeout_changed
            )
            secrets_group.add(self.secret_session_timeout_row)

            # Show the Vaultwarden/timeout rows only when they apply to the
            # currently-selected backend.
            self._update_secret_rows_visibility(current_backend)

            advanced_page.add(secrets_group)

            # Application behavior group
            behavior_group = Adw.PreferencesGroup(title="Application Behavior")
            
            # Confirm before disconnecting
            self.confirm_disconnect_switch = Adw.SwitchRow()
            self.confirm_disconnect_switch.set_title("Confirm before disconnecting")
            self.confirm_disconnect_switch.set_subtitle("Show a confirmation dialog when disconnecting from a host")
            self.confirm_disconnect_switch.set_active(
                self.config.get_setting('confirm-disconnect', True)
            )
            self.confirm_disconnect_switch.connect('notify::active', self.on_confirm_disconnect_changed)
            behavior_group.add(self.confirm_disconnect_switch)

            # SSH askpass helper (master switch for sshPilot's passphrase handling)
            self.askpass_switch = Adw.SwitchRow()
            self.askpass_switch.set_title("Use SSH askpass helper")
            self.askpass_switch.set_subtitle(
                "Lets SSH Pilot autofill key passphrases from the keyring. "
                "When off, SSH handles passphrase prompts itself."
            )
            askpass_on = bool(self.config.get_setting('use-askpass', True))
            self.askpass_switch.set_active(askpass_on)
            self.askpass_switch.connect('notify::active', self.on_use_askpass_changed)
            behavior_group.add(self.askpass_switch)

            # Built-in passphrase prompt (only relevant when the askpass helper is on)
            self.builtin_passphrase_prompt_switch = Adw.SwitchRow()
            self.builtin_passphrase_prompt_switch.set_title("Use built-in passphrase prompt")
            self.builtin_passphrase_prompt_switch.set_subtitle(
                "When off, SSH and the system keyring handle key passphrase prompts"
            )
            self.builtin_passphrase_prompt_switch.set_active(
                bool(self.config.get_setting('use-builtin-passphrase-prompt', False))
            )
            self.builtin_passphrase_prompt_switch.set_sensitive(askpass_on)
            self.builtin_passphrase_prompt_switch.connect(
                'notify::active', self.on_builtin_passphrase_prompt_changed
            )
            behavior_group.add(self.builtin_passphrase_prompt_switch)

            # Preload keys into ssh-agent on connect (uses the askpass helper to
            # unlock the key so a gnome-keyring-locked key can sign; the agent is
            # never disabled).
            self.agent_preload_switch = Adw.SwitchRow()
            self.agent_preload_switch.set_title("Preload keys into ssh-agent")
            self.agent_preload_switch.set_subtitle(
                "Add keys with a saved passphrase to ssh-agent so they connect "
                "without a prompt"
            )
            self.agent_preload_switch.set_active(
                bool(self.config.get_setting('ssh.agent_preload_keys', True))
            )
            self.agent_preload_switch.set_sensitive(askpass_on)
            self.agent_preload_switch.connect(
                'notify::active', self.on_agent_preload_changed
            )
            behavior_group.add(self.agent_preload_switch)

            advanced_page.add(behavior_group)

            # Logging group ------------------------------------------------
            logging_group = Adw.PreferencesGroup(title="Logging")
            logging_group.set_description(
                "Controls how verbose sshPilot is in the console and the rotated log file. "
                "The command-line flags --verbose / --quiet always override this."
            )

            self.logging_level_row = Adw.ComboRow()
            self.logging_level_row.set_title("Log Level")
            self.logging_level_row.set_subtitle(
                "Default is concise; switch to Debug if you're filing a bug or chasing an issue"
            )
            log_levels_model = Gtk.StringList()
            log_levels_model.append("Info — concise (recommended)")
            log_levels_model.append("Debug — verbose")
            self.logging_level_row.set_model(log_levels_model)
            saved_level = str(
                self.config.get_setting('logging.level', 'info') or 'info'
            ).lower()
            self.logging_level_row.set_selected(1 if saved_level == 'debug' else 0)
            self.logging_level_row.connect(
                'notify::selected', self.on_logging_level_changed
            )
            logging_group.add(self.logging_level_row)
            advanced_page.add(logging_group)

            # File management preferences moved to dedicated page

            ssh_settings_page = Adw.PreferencesPage()
            ssh_settings_page.set_title("SSH")
            ssh_settings_page.set_icon_name("network-workgroup-symbolic")

            help_group = Adw.PreferencesGroup()
            help_row = Adw.ActionRow()
            help_row.set_title("Custom SSH Options")
            help_row.set_subtitle(
                "These settings override values from your ~/.ssh/config."
            )
            if hasattr(help_row, "set_activatable"):
                help_row.set_activatable(False)
            if hasattr(help_row, "set_selectable"):
                help_row.set_selectable(False)
            help_group.add(help_row)

            advanced_group = Adw.PreferencesGroup(title="SSH Settings")

            # Connect timeout
            self.connect_timeout_row = Adw.SpinRow.new_with_range(0, 120, 1)
            self.connect_timeout_row.set_title("Connect Timeout (s)")
            connect_timeout_value = self.config.get_setting('ssh.connection_timeout', None)
            try:
                connect_timeout_value = int(connect_timeout_value)
            except (TypeError, ValueError):
                connect_timeout_value = 0
            if connect_timeout_value < 0:
                connect_timeout_value = 0
            self.connect_timeout_row.set_value(connect_timeout_value)
            advanced_group.add(self.connect_timeout_row)

            # Connection attempts
            self.connection_attempts_row = Adw.SpinRow.new_with_range(0, 10, 1)
            self.connection_attempts_row.set_title("Connection Attempts")
            connection_attempts_value = self.config.get_setting('ssh.connection_attempts', None)
            try:
                connection_attempts_value = int(connection_attempts_value)
            except (TypeError, ValueError):
                connection_attempts_value = 0
            if connection_attempts_value < 0:
                connection_attempts_value = 0
            self.connection_attempts_row.set_value(connection_attempts_value)
            advanced_group.add(self.connection_attempts_row)

            # Default keepalive opt-out. When on (default) and the user hasn't
            # set an explicit ServerAlive interval below or in ~/.ssh/config,
            # SSH Pilot applies a sane default so dropped links are detected.
            self.apply_default_keepalive_row = Adw.SwitchRow()
            self.apply_default_keepalive_row.set_title("Apply default keepalive")
            self.apply_default_keepalive_row.set_subtitle(
                "Detect dropped connections automatically when no ServerAlive "
                "value is set here or in ~/.ssh/config. Explicit values always win."
            )
            self.apply_default_keepalive_row.set_active(
                bool(self.config.get_setting('ssh.apply_default_keepalive', True))
            )
            advanced_group.add(self.apply_default_keepalive_row)

            # Keepalive interval
            self.keepalive_interval_row = Adw.SpinRow.new_with_range(0, 300, 5)
            self.keepalive_interval_row.set_title("ServerAlive Interval (s)")
            keepalive_interval_value = self.config.get_setting('ssh.keepalive_interval', None)
            try:
                keepalive_interval_value = int(keepalive_interval_value)
            except (TypeError, ValueError):
                keepalive_interval_value = 0
            if keepalive_interval_value < 0:
                keepalive_interval_value = 0
            self.keepalive_interval_row.set_value(keepalive_interval_value)
            advanced_group.add(self.keepalive_interval_row)

            # Keepalive count max
            self.keepalive_count_row = Adw.SpinRow.new_with_range(0, 10, 1)
            self.keepalive_count_row.set_title("ServerAlive CountMax")
            keepalive_count_value = self.config.get_setting('ssh.keepalive_count_max', None)
            try:
                keepalive_count_value = int(keepalive_count_value)
            except (TypeError, ValueError):
                keepalive_count_value = 0
            if keepalive_count_value < 0:
                keepalive_count_value = 0
            self.keepalive_count_row.set_value(keepalive_count_value)
            advanced_group.add(self.keepalive_count_row)

            # Strict host key checking
            self.strict_host_row = Adw.ComboRow()
            self.strict_host_row.set_title("StrictHostKeyChecking")
            strict_model = Gtk.StringList()
            for item in ["accept-new", "yes", "no", "ask"]:
                strict_model.append(item)
            self.strict_host_row.set_model(strict_model)
            # Map current value
            current_strict = str(self.config.get_setting('ssh.strict_host_key_checking', 'accept-new'))
            try:
                idx = ["accept-new", "yes", "no", "ask"].index(current_strict)
            except ValueError:
                idx = 0
            self.strict_host_row.set_selected(idx)
            advanced_group.add(self.strict_host_row)

            # BatchMode (non-interactive)
            self.batch_mode_row = Adw.SwitchRow()
            self.batch_mode_row.set_title("BatchMode (disable prompts)")
            self.batch_mode_row.set_active(bool(self.config.get_setting('ssh.batch_mode', False)))
            advanced_group.add(self.batch_mode_row)

            # Compression
            self.compression_row = Adw.SwitchRow()
            self.compression_row.set_title("Enable Compression (-C)")
            self.compression_row.set_active(bool(self.config.get_setting('ssh.compression', False)))
            advanced_group.add(self.compression_row)

            # Connection multiplexing (ControlMaster). When on, the first
            # connection to a host opens a master socket that later connections
            # reuse (no re-handshake) — faster reconnects and chatty operations.
            self.controlmaster_row = Adw.SwitchRow()
            self.controlmaster_row.set_title("Enable SSH connection multiplexing")
            self.controlmaster_row.set_subtitle(
                "Reuse one connection per host (ControlMaster) for faster reconnects"
            )
            self.controlmaster_row.set_active(bool(self.config.get_setting('ssh.controlmaster', False)))
            advanced_group.add(self.controlmaster_row)

            # SSH verbosity (-v levels)
            self.verbosity_row = Adw.SpinRow.new_with_range(0, 3, 1)
            self.verbosity_row.set_title("SSH Verbosity (-v)")
            self.verbosity_row.set_value(int(self.config.get_setting('ssh.verbosity', 0)))
            advanced_group.add(self.verbosity_row)

            # Debug logging toggle
            self.debug_enabled_row = Adw.SwitchRow()
            self.debug_enabled_row.set_title("Enable SSH Debug Logging")
            self.debug_enabled_row.set_active(bool(self.config.get_setting('ssh.debug_enabled', False)))
            advanced_group.add(self.debug_enabled_row)


            # Reset button
            # Add spacing before reset button
            advanced_group.add(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            
            # Use Adw.ActionRow for proper spacing and layout
            reset_row = Adw.ActionRow()
            reset_row.set_title("Reset Advanced SSH Settings")
            reset_row.set_subtitle("Restore all advanced SSH settings to their default values")
            
            reset_btn = Gtk.Button.new_with_label("Reset")
            reset_btn.add_css_class('destructive-action')
            reset_btn.set_valign(Gtk.Align.CENTER)
            reset_btn.connect('clicked', self.on_reset_advanced_ssh)
            reset_row.add_suffix(reset_btn)
            
            advanced_group.add(reset_row)

            ssh_settings_page.add(help_group)
            ssh_settings_page.add(advanced_group)

            # Ensure shortcut overview controls reflect current state
            self._set_shortcut_controls_enabled(not self._pass_through_enabled)

            # Create File Management preferences page
            file_management_page = Adw.PreferencesPage()
            file_management_page.set_title("File Management")
            file_management_page.set_icon_name("folder-symbolic")
            
            # File Management group
            if has_internal_file_manager():
                file_manager_group = Adw.PreferencesGroup(title="File Manager Options")
                file_manager_group.set_description(
                    "These preferences only affect sshPilot's built-in SFTP file manager."
                )

                self.force_internal_file_manager_row = None
                if should_show_force_internal_file_manager_toggle():
                    self.force_internal_file_manager_row = Adw.SwitchRow()
                    self.force_internal_file_manager_row.set_title("Always Use Built-in File Manager")
                    self.force_internal_file_manager_row.set_subtitle(
                        "Use the in-app file manager even when system integrations are available"
                    )
                    self.force_internal_file_manager_row.set_active(
                        bool(self.config.get_setting('file_manager.force_internal', False))
                    )
                    self.force_internal_file_manager_row.connect(
                        'notify::active', self.on_force_internal_file_manager_changed
                    )

                    file_manager_group.add(self.force_internal_file_manager_row)

                self.open_file_manager_externally_row = Adw.SwitchRow()
                self.open_file_manager_externally_row.set_title("Open File Manager in Separate Window")
                self.open_file_manager_externally_row.set_subtitle(
                    "Show the built-in file manager in its own window instead of a tab"
                )
                self.open_file_manager_externally_row.set_active(
                    bool(self.config.get_setting('file_manager.open_externally', False))
                )
                self.open_file_manager_externally_row.connect(
                    'notify::active', self.on_open_file_manager_externally_changed
                )

                file_manager_group.add(self.open_file_manager_externally_row)

                file_manager_defaults = {}
                try:
                    defaults = self.config.get_default_config()
                    file_manager_defaults = defaults.get('file_manager', {}) if isinstance(defaults, dict) else {}
                except Exception:
                    file_manager_defaults = {}

                file_manager_config: Dict[str, Any] = {}
                if hasattr(self.config, 'get_file_manager_config'):
                    try:
                        file_manager_config = self.config.get_file_manager_config() or {}
                    except Exception as exc:
                        logger.debug("Failed to read file manager configuration: %s", exc)
                        file_manager_config = {}

                def _fm_default_int(key: str, fallback: int = 0) -> int:
                    value = 0
                    if isinstance(file_manager_defaults, dict):
                        try:
                            value = int(file_manager_defaults.get(key, fallback))
                        except (TypeError, ValueError):
                            value = fallback
                    else:
                        value = fallback
                    return value if value >= 0 else fallback

                def _fm_config_int(key: str, fallback: int) -> int:
                    if isinstance(file_manager_config, dict):
                        try:
                            value = int(file_manager_config.get(key, fallback))
                        except (TypeError, ValueError):
                            value = fallback
                        if value < 0:
                            return fallback
                        return value
                    return fallback

                keepalive_interval_default = _fm_default_int('sftp_keepalive_interval', 0)
                keepalive_interval_value = _fm_config_int('sftp_keepalive_interval', keepalive_interval_default)
                keepalive_interval_value = max(0, min(keepalive_interval_value, 3600))

                sftp_advanced_group = Adw.PreferencesGroup(title="Advanced SFTP Settings")
                sftp_advanced_group.set_description(
                    "Fine-tune options that only apply to sshPilot's built-in SFTP file manager."
                )


                self.sftp_keepalive_interval_row = Adw.SpinRow.new_with_range(0, 3600, 5)
                self.sftp_keepalive_interval_row.set_title("SFTP Keepalive Interval (seconds)")
                self.sftp_keepalive_interval_row.set_subtitle(
                    "How often the built-in file manager sends keepalives. "
                    "Set to 0 to disable."
                )
                self.sftp_keepalive_interval_row.set_value(keepalive_interval_value)
                sftp_advanced_group.add(self.sftp_keepalive_interval_row)


                keepalive_count_default = _fm_default_int('sftp_keepalive_count_max', 0)
                keepalive_count_value = _fm_config_int('sftp_keepalive_count_max', keepalive_count_default)
                keepalive_count_value = max(0, min(keepalive_count_value, 10))

                self.sftp_keepalive_count_row = Adw.SpinRow.new_with_range(0, 10, 1)
                self.sftp_keepalive_count_row.set_title("SFTP Keepalive Retry Limit")
                self.sftp_keepalive_count_row.set_subtitle(
                    "Number of failed keepalives tolerated by the built-in file "
                    "manager before raising an error."
                )
                self.sftp_keepalive_count_row.set_value(keepalive_count_value)
                sftp_advanced_group.add(self.sftp_keepalive_count_row)


                connect_timeout_default = _fm_default_int('sftp_connect_timeout', 0)
                connect_timeout_value = _fm_config_int('sftp_connect_timeout', connect_timeout_default)
                connect_timeout_value = max(0, min(connect_timeout_value, 600))

                self.sftp_connect_timeout_row = Adw.SpinRow.new_with_range(0, 600, 1)
                self.sftp_connect_timeout_row.set_title("SFTP Connection Timeout (seconds)")
                self.sftp_connect_timeout_row.set_subtitle(
                    "Time allowed for the built-in file manager to establish a "
                    "session; 0 uses the default."
                )
                self.sftp_connect_timeout_row.set_value(connect_timeout_value)
                sftp_advanced_group.add(self.sftp_connect_timeout_row)


                self._update_external_file_manager_row()
                file_management_page.add(file_manager_group)
                file_management_page.add(sftp_advanced_group)
            else:
                # If no internal file manager, create empty page with message
                no_file_manager_group = Adw.PreferencesGroup(title="File Manager")
                no_file_manager_row = Adw.ActionRow()
                no_file_manager_row.set_title("File Manager Not Available")
                no_file_manager_row.set_subtitle("Built-in file manager is not available on this system")
                no_file_manager_group.add(no_file_manager_row)
                file_management_page.add(no_file_manager_group)

            # Updates page
            updates_page = Adw.PreferencesPage()
            updates_page.set_title("Updates")
            updates_page.set_icon_name("software-update-available-symbolic")
            
            updates_group = Adw.PreferencesGroup(title="Update Notifications")
            updates_group.set_description("Configure how SSH Pilot checks for updates")
            
            # Check for updates on startup switch
            self.check_updates_switch = Adw.SwitchRow()
            self.check_updates_switch.set_title("Check for updates on startup")
            self.check_updates_switch.set_subtitle("Automatically check for new versions when the application starts")
            self.check_updates_switch.set_active(
                self.config.get_setting('updates.check_on_startup', True)
            )
            self.check_updates_switch.connect('notify::active', self.on_check_updates_changed)
            updates_group.add(self.check_updates_switch)
            
            updates_page.add(updates_group)

            # Add pages to the custom layout
            self.add_page_to_layout("Interface", "applications-graphics-symbolic", interface_page)
            self.add_page_to_layout("Terminal", "utilities-terminal-symbolic", terminal_page)
            self.add_page_to_layout("File Management", "folder-symbolic", file_management_page)
            self.add_page_to_layout("Shortcuts", "preferences-desktop-keyboard-shortcuts-symbolic", shortcuts_page)
            self.add_page_to_layout("Groups", "folder-open-symbolic", groups_page)
            self.add_page_to_layout("SSH Options", "network-workgroup-symbolic", ssh_settings_page)
            self.add_page_to_layout("Updates", "software-update-available-symbolic", updates_page)
            plugins_page = self._create_plugins_page()
            self.add_page_to_layout("Plugins", "application-x-addon-symbolic", plugins_page)
            command_blocks_page = self._create_command_blocks_page()
            self.add_page_to_layout("Command Blocks", "view-list-symbolic", command_blocks_page)
            self.add_page_to_layout("Advanced", "applications-system-symbolic", advanced_page)
            
            logger.info("Preferences window initialized")
        except Exception as e:
            logger.error(f"Failed to setup preferences: {e}")

    def on_close_request(self, *args):
        """Persist settings when the preferences window closes"""
        try:
            if hasattr(self, 'shortcuts_editor_page'):
                self.shortcuts_editor_page.flush_changes()
            self.save_advanced_ssh_settings()
            # Ensure preferences are flushed to disk
            if hasattr(self.config, 'save_json_config'):
                self.config.save_json_config()
        except Exception:
            pass
        return False  # allow close

    def on_view_shortcuts_clicked(self, _button):
        """Open the standalone shortcuts window from preferences."""

        if self.parent_window and hasattr(self.parent_window, 'show_shortcuts_window'):
            try:
                self.parent_window.show_shortcuts_window()
            except Exception as exc:
                logger.error("Failed to open shortcuts window: %s", exc)

    def on_pass_through_mode_toggled(self, switch, _pspec):
        """Persist changes to the terminal pass-through preference."""
        active = bool(switch.get_active())
        self._pass_through_enabled = active
        self._set_shortcut_controls_enabled(not active)
        try:
            self.config.set_setting('terminal.pass_through_mode', active)
        except Exception as exc:
            logger.error("Failed to update pass-through mode: %s", exc)

        if hasattr(self, 'shortcuts_editor_page') and self.shortcuts_editor_page is not None:
            try:
                self.shortcuts_editor_page.set_pass_through_enabled(active)
            except Exception as exc:
                logger.debug("Failed to propagate pass-through state to shortcut editor: %s", exc)

    def _update_secret_rows_visibility(self, name):
        """Show the Vaultwarden server-URL row only for the Vaultwarden backend,
        and the session-timeout row only for session-backed backends."""
        try:
            name = (name or 'auto').strip().lower()
            session = False
            try:
                from .secret_storage import get_secret_manager
                session = get_secret_manager().is_session_backed(name)
            except Exception:
                session = name in ('bitwarden', 'vaultwarden')
            if hasattr(self, 'vaultwarden_server_row'):
                self.vaultwarden_server_row.set_visible(name == 'vaultwarden')
            if hasattr(self, 'secret_session_timeout_row'):
                self.secret_session_timeout_row.set_visible(session)
        except Exception as exc:
            logger.debug("Failed to update secret rows visibility: %s", exc)

    def on_secret_backend_changed(self, combo, _pspec):
        """Persist the selected secret storage backend and apply it live."""
        try:
            index = combo.get_selected()
            ids = getattr(self, '_secret_backend_ids', ['auto'])
            name = ids[index] if 0 <= index < len(ids) else 'auto'
            self.config.set_setting('secrets.backend', name)
            from .secret_storage import get_secret_manager
            manager = get_secret_manager()
            manager.set_selected(name)
            # Propagate to child processes (e.g. the askpass helper).
            os.environ['SSHPILOT_SECRET_BACKEND'] = name
            self._update_secret_rows_visibility(name)
            logger.info("Secret storage backend set to: %s", name)
            # A session-backed backend must be unlocked before it can store/read.
            if manager.selected_needs_unlock():
                try:
                    from .secret_unlock_dialog import prompt_unlock

                    # prompt_unlock owns its own messaging (login-required /
                    # wrong-password); we only log the outcome here.
                    def _done(success):
                        logger.info("Secret backend unlock %s",
                                    "succeeded" if success else "not completed")
                    prompt_unlock(self, on_done=_done)
                except Exception as exc:
                    logger.error("Failed to prompt secret backend unlock: %s", exc)
        except Exception as exc:
            logger.error("Failed to update secret storage backend: %s", exc)

    def on_vaultwarden_server_changed(self, row):
        """Persist and propagate the Vaultwarden self-hosted server URL."""
        try:
            url = (row.get_text() or '').strip()
            self.config.set_setting('secrets.vaultwarden.server', url)
            if url:
                os.environ['SSHPILOT_VAULTWARDEN_SERVER'] = url
            else:
                os.environ.pop('SSHPILOT_VAULTWARDEN_SERVER', None)
        except Exception as exc:
            logger.error("Failed to update Vaultwarden server URL: %s", exc)

    def on_secret_session_timeout_changed(self, row, _pspec):
        """Persist and propagate the session-backend idle unlock timeout."""
        try:
            minutes = int(row.get_value())
            self.config.set_setting('secrets.session_timeout', minutes)
            os.environ['SSHPILOT_SECRET_SESSION_TIMEOUT'] = str(max(0, minutes) * 60)
        except Exception as exc:
            logger.error("Failed to update secret session timeout: %s", exc)

    def on_copy_on_select_toggled(self, switch, _pspec):
        """Persist the terminal copy-on-selection preference."""
        try:
            self.config.set_setting('terminal.copy_on_select', bool(switch.get_active()))
        except Exception as exc:
            logger.error("Failed to update copy-on-select mode: %s", exc)

    def on_paste_on_right_click_toggled(self, switch, _pspec):
        """Persist the terminal paste-on-right-click preference."""
        try:
            self.config.set_setting('terminal.paste_on_right_click', bool(switch.get_active()))
        except Exception as exc:
            logger.error("Failed to update paste-on-right-click mode: %s", exc)

    def _set_shortcut_controls_enabled(self, enabled: bool):
        for widget in (getattr(self, '_shortcuts_row', None), getattr(self, '_shortcuts_button', None)):
            if widget is None:
                continue
            try:
                widget.set_sensitive(bool(enabled))
            except Exception:
                logger.debug("Failed to update shortcut control sensitivity", exc_info=True)

    def on_font_button_clicked(self, button):
        """Handle font button click"""
        logger.info("Font button clicked")
        
        # Get current font from config
        current_font = self.config.get_setting('terminal.font', 'Monospace 12')
        
        # Create custom monospace font dialog
        font_dialog = MonospaceFontDialog(parent=self, current_font=current_font)
        
        def on_font_selected(font_string):
            self.font_row.set_subtitle(font_string)
            logger.info(f"Font selected: {font_string}")
            
            # Save to config
            self.config.set_setting('terminal.font', font_string)
            
            # Apply to all active terminals
            self.apply_font_to_terminals(font_string)
        
        font_dialog.set_callback(on_font_selected)
        font_dialog.present()
    
    def apply_font_to_terminals(self, font_string):
        """Apply font to all active terminal widgets"""
        try:
            parent_window = self.get_transient_for()
            if parent_window and hasattr(parent_window, 'connection_to_terminals'):
                font_desc = Pango.FontDescription.from_string(font_string)
                count = 0
                for terms in parent_window.connection_to_terminals.values():
                    for terminal in terms:
                        # Use backend abstraction instead of direct vte access
                        if hasattr(terminal, 'backend') and terminal.backend:
                            terminal.backend.set_font(font_desc)
                            count += 1
                        elif hasattr(terminal, 'vte') and terminal.vte:
                            terminal.vte.set_font(font_desc)
                            count += 1
                logger.info(f"Applied font {font_string} to {count} terminals")
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
            self.config.set_setting('app-theme', 'default')
        elif selected == 1:  # Light
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            self.config.set_setting('app-theme', 'light')
        elif selected == 2:  # Dark
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            self.config.set_setting('app-theme', 'dark')

    def on_group_color_display_changed(self, combo_row, _param):
        """Persist sidebar group color display preference changes."""
        if getattr(self, '_group_display_sync', False):
            return

        try:
            selected_index = combo_row.get_selected()
        except Exception:
            selected_index = 0

        try:
            mode = self._group_color_display_values[selected_index]
        except Exception:
            mode = 'fill'

        try:
            current_mode = str(
                self.config.get_setting('ui.group_color_display', 'fill')
            ).lower()
        except Exception:
            current_mode = 'fill'

        if current_mode == mode:
            return

        try:
            self.config.set_setting('ui.group_color_display', mode)
        except Exception as exc:
            logger.error("Failed to update group color display preference: %s", exc)
            return

        if not getattr(self, '_config_signal_id', None):
            self._trigger_sidebar_refresh()

    def on_group_row_display_changed(self, toggle_group, _param):
        """Persist sidebar group display layout preference."""
        if getattr(self, '_group_display_toggle_sync', False):
            return

        try:
            active_name = toggle_group.get_active_name() or 'fullwidth'
        except Exception:
            active_name = 'fullwidth'

        valid_modes = getattr(self, '_group_display_modes', ['fullwidth', 'nested'])
        if active_name not in valid_modes:
            active_name = 'nested'

        try:
            current_value = str(
                self.config.get_setting('ui.group_row_display', 'nested')
            ).lower()
        except Exception:
            current_value = 'nested'

        if current_value not in valid_modes:
            current_value = 'nested'

        if current_value == active_name:
            self._update_group_display_preview(active_name)
            return

        try:
            self.config.set_setting('ui.group_row_display', active_name)
        except Exception as exc:
            logger.error(
                "Failed to update group display preference: %s", exc
            )
            self._sync_group_display_toggle_group(current_value)
            return

        self._update_group_display_preview(active_name)

        if not getattr(self, '_config_signal_id', None):
            self._trigger_sidebar_refresh()

    def on_use_group_color_in_tab_toggled(self, switch_row, _param):
        if getattr(self, '_tab_color_sync', False):
            return

        new_value = bool(switch_row.get_active())

        try:
            current_value = bool(
                self.config.get_setting('ui.use_group_color_in_tab', False)
            )
        except Exception:
            current_value = False

        if new_value == current_value:
            self._trigger_terminal_style_refresh()
            return

        try:
            self.config.set_setting('ui.use_group_color_in_tab', new_value)
        except Exception as exc:
            logger.error(
                "Failed to update tab group color preference: %s", exc,
            )
            self._sync_use_group_color_in_tab(current_value)
            return

        if not getattr(self, '_config_signal_id', None):
            self._trigger_terminal_style_refresh()

    def on_use_group_color_in_terminal_toggled(self, switch_row, _param):
        if getattr(self, '_terminal_color_sync', False):
            return

        new_value = bool(switch_row.get_active())

        try:
            current_value = bool(
                self.config.get_setting('ui.use_group_color_in_terminal', False)
            )
        except Exception:
            current_value = False

        if new_value == current_value:
            self._trigger_terminal_style_refresh()
            return

        try:
            self.config.set_setting('ui.use_group_color_in_terminal', new_value)
        except Exception as exc:
            logger.error(
                "Failed to update terminal group color preference: %s", exc,
            )
            self._sync_use_group_color_in_terminal(current_value)
            return

        if not getattr(self, '_config_signal_id', None):
            self._trigger_terminal_style_refresh()




    def _trigger_sidebar_refresh(self):
        parent = self.get_transient_for() or self.parent_window
        if not parent:
            return

        if hasattr(parent, 'rebuild_connection_list'):
            try:
                parent.rebuild_connection_list()
            except Exception as exc:
                logger.debug("Failed to rebuild connection list after preference change: %s", exc)

    def _trigger_terminal_style_refresh(self):
        parent = self.get_transient_for() or self.parent_window
        if not parent:
            return

        manager = getattr(parent, 'terminal_manager', None)
        if not manager or not hasattr(manager, 'restyle_open_terminals'):
            return

        try:
            manager.restyle_open_terminals()
        except Exception as exc:
            logger.debug(
                "Failed to restyle terminals after preference change: %s", exc
            )

    def _create_plugins_page(self):
        """Build the Plugins preferences page (enable/disable, install/remove)."""
        self._plugins_page = Adw.PreferencesPage()
        self._plugin_groups = []
        self._populate_plugins_page()
        return self._plugins_page

    def _rebuild_plugins_page(self):
        """Re-render the page in place after an install/remove."""
        page = getattr(self, '_plugins_page', None)
        if page is None:
            return
        for group in getattr(self, '_plugin_groups', []):
            try:
                page.remove(group)
            except Exception:
                pass
        self._plugin_groups = []
        self._populate_plugins_page()

    def _populate_plugins_page(self):
        from .plugins.loader import discover_plugins, _user_plugin_dir

        page = self._plugins_page
        try:
            infos = discover_plugins()
        except Exception:
            logger.exception("Plugin discovery failed")
            infos = []
        loaded_ids = {
            getattr(p, 'plugin_id', None)
            for p in getattr(self.parent_window, 'loaded_plugins', []) or []
        }
        disabled = set(self.config.get_setting('plugins.disabled', []) or [])
        enabled = set(self.config.get_setting('plugins.enabled', []) or [])

        def _subtitle(info):
            if not info.api_compatible:
                return _("Incompatible (targets API v{})").format(info.api_version)
            if info.required:
                return _("Required — always enabled")
            if info.plugin_id in loaded_ids:
                return _("Active")
            return _("Inactive")

        builtin_group = Adw.PreferencesGroup(title=_("Built-in Plugins"))
        builtin_group.set_description(_("Changes take effect after restarting sshPilot"))
        for info in [i for i in infos if i.builtin]:
            # ActionRow + manual switch so the suffixes order as
            # gear, toggle, info (the SwitchRow toggle is forced to the far end).
            row = Adw.ActionRow()
            row.set_title(info.name)
            row.set_subtitle(_subtitle(info))
            togglable = not (info.required or not info.api_compatible)
            switch = self._make_plugin_switch(
                info.required or info.plugin_id not in disabled, togglable)
            if togglable:
                switch.connect(
                    'notify::active',
                    lambda s, _p, pid=info.plugin_id, r=row:
                        self._on_builtin_plugin_toggled(pid, s.get_active(), r),
                )
                row.set_activatable_widget(switch)
            self._add_plugin_page_gear(row, info.plugin_id)  # gear (leftmost)
            row.add_suffix(switch)                            # toggle
            self._add_plugin_info(row, info)                  # info
            builtin_group.add(row)
        page.add(builtin_group)
        self._plugin_groups.append(builtin_group)

        user_group = Adw.PreferencesGroup(title=_("User Plugins"))
        user_group.set_description(
            _("Third-party plugins run with full application privileges; "
              "only enable plugins you trust. Restart required."))
        user_group.set_header_suffix(self._make_install_button())
        user_infos = [i for i in infos if not i.builtin]
        # pid -> (row, info, update_button); the update button is built hidden and
        # revealed by the async registry load when a newer version is available.
        self._user_plugin_rows = {}
        if user_infos:
            for info in user_infos:
                # ActionRow + manual switch so suffixes order as
                # gear, toggle, info, update, delete (delete stays rightmost).
                row = Adw.ActionRow()
                row.set_title(info.name)
                row.set_subtitle(_subtitle(info))
                switch = self._make_plugin_switch(
                    info.plugin_id in enabled, info.api_compatible)
                if info.api_compatible:
                    switch.connect(
                        'notify::active',
                        lambda s, _p, pid=info.plugin_id, perms=info.permissions, r=row:
                            self._on_user_plugin_toggled(pid, s.get_active(), r,
                                                         perms, switch=s),
                    )
                    row.set_activatable_widget(switch)
                self._add_plugin_page_gear(row, info.plugin_id)  # gear (leftmost)
                row.add_suffix(switch)                            # toggle
                self._add_plugin_info(row, info)                  # info
                update_btn = Gtk.Button()
                update_btn.set_icon_name('software-update-available-symbolic')
                update_btn.add_css_class('flat')
                update_btn.add_css_class('accent')
                update_btn.set_valign(Gtk.Align.CENTER)
                update_btn.set_visible(False)
                row.add_suffix(update_btn)                         # update (hidden)
                self._user_plugin_rows[info.plugin_id] = (row, info, update_btn)
                remove = Gtk.Button()
                remove.set_icon_name('user-trash-symbolic')
                remove.add_css_class('flat')
                remove.add_css_class('error')
                remove.set_valign(Gtk.Align.CENTER)
                remove.set_tooltip_text(_("Remove plugin"))
                remove.connect('clicked',
                               lambda _b, i=info: self._confirm_remove_plugin(i))
                row.add_suffix(remove)                            # delete (rightmost)
                user_group.add(row)
        else:
            empty_row = Adw.ActionRow()
            empty_row.set_title(_("No user plugins installed"))
            empty_row.set_subtitle(str(_user_plugin_dir()))
            user_group.add(empty_row)
        page.add(user_group)
        self._plugin_groups.append(user_group)

        # Available plugins fetched from the discovery registry. Installed/builtin
        # ids are filtered out; toggling one on downloads + verifies + installs it.
        self._installed_ids = {i.plugin_id for i in infos}
        self._available_group = Adw.PreferencesGroup(title=_("Available Plugins"))
        self._available_group.set_description(
            _("From the sshPilot plugin registry. Toggling one on downloads, "
              "verifies, and installs it (restart to load)."))
        self._available_loading = Adw.ActionRow(title=_("Checking the plugin registry…"))
        spinner = Gtk.Spinner()
        spinner.start()
        self._available_loading.add_prefix(spinner)
        self._available_group.add(self._available_loading)
        page.add(self._available_group)
        self._plugin_groups.append(self._available_group)
        self._load_registry_async()

    # --- available plugins (discovery registry) ----------------------
    def _registry_url(self):
        from .plugins.registry_client import DEFAULT_REGISTRY_URL
        return self.config.get_setting('plugins.registry_url', DEFAULT_REGISTRY_URL) \
            or DEFAULT_REGISTRY_URL

    def _load_registry_async(self):
        url = self._registry_url()
        group = self._available_group

        def worker():
            from .plugins import registry_client
            try:
                entries = registry_client.list_entries(registry_client.fetch_index(url))
            except Exception as exc:
                GLib.idle_add(self._on_registry_loaded, group, None, str(exc))
                return
            GLib.idle_add(self._on_registry_loaded, group, entries, None)
        threading.Thread(target=worker, daemon=True).start()

    def _on_registry_loaded(self, group, entries, error):
        if group is not getattr(self, '_available_group', None):
            return  # page was rebuilt; ignore a stale fetch
        if self._available_loading is not None:
            try:
                group.remove(self._available_loading)
            except Exception:
                pass
            self._available_loading = None
        self._available_rows = []  # _build_available_row appends here

        def _note(title, subtitle=None):
            row = Adw.ActionRow(title=title)
            if subtitle:
                row.set_subtitle(subtitle)
            group.add(row)
            self._available_rows.append(row)

        if error is not None:
            _note(_("Couldn't reach the plugin registry"), error)
            return
        shown = 0
        for entry in entries or []:
            if entry['id'] in getattr(self, '_installed_ids', set()):
                continue  # already installed or a built-in
            group.add(self._build_available_row(entry))
            shown += 1
        if shown == 0:
            _note(_("No new plugins available"))
        # Flag installed user plugins that have a newer version in the registry.
        self._apply_plugin_updates(entries)

    def _apply_plugin_updates(self, entries):
        """Reveal the per-row update button when the registry has a newer,
        compatible version than the installed plugin's manifest version."""
        # Keep the entries so the per-plugin info dialog can fall back to the
        # registry's homepage when a manifest doesn't declare one.
        self._registry_entries = list(entries or [])
        from .update_checker import compare_versions
        rows = getattr(self, '_user_plugin_rows', {})
        by_id = {e['id']: e for e in (entries or [])}
        for pid, (row, info, update_btn) in rows.items():
            entry = by_id.get(pid)
            installed = getattr(info, 'version', None)
            latest = entry.get('version') if entry else None
            if (entry is None or not entry.get('compatible', True)
                    or not installed or not latest):
                continue
            if not compare_versions(installed, latest):
                continue  # up to date (latest not greater)
            update_btn.set_tooltip_text(
                _("Update available (v{} → v{})").format(installed, latest))
            if not getattr(update_btn, '_wired', False):
                update_btn.connect(
                    'clicked', lambda b, e=entry: self._on_update_clicked(b, e))
                update_btn._wired = True
            update_btn.set_visible(True)

    def _on_update_clicked(self, button, entry):
        button.set_sensitive(False)
        button.set_tooltip_text(_("Updating…"))

        def on_fail():
            button.set_sensitive(True)
            button.set_tooltip_text(_("Update available"))

        self._start_registry_install(entry, on_fail)

    def _available_subtitle(self, entry):
        bits = [b for b in (entry.get('description'),
                            (_("by {}").format(entry['author']) if entry.get('author') else None),
                            ("v" + entry['version']) if entry.get('version') else None) if b]
        return " · ".join(bits)

    def _build_available_row(self, entry):
        row = Adw.SwitchRow()
        row.set_title(entry['name'])
        row.set_subtitle(self._available_subtitle(entry))
        row.set_active(False)
        self._add_permissions_info(row, entry.get('permissions'))
        if not entry.get('compatible', True):
            row.set_sensitive(False)
            row.set_subtitle(_("Incompatible (targets API v{})").format(entry.get('api_version')))
        else:
            row.connect('notify::active',
                        lambda r, _p, e=entry: self._on_available_plugin_toggled(e, r))
        # keep a handle so _on_registry_loaded can clear it on refresh
        self._available_rows = getattr(self, '_available_rows', [])
        self._available_rows.append(row)
        return row

    def _on_available_plugin_toggled(self, entry, row):
        if getattr(self, '_suppress_toggle', False) or not row.get_active():
            return

        def revert():
            self._suppress_toggle = True
            row.set_active(False)
            self._suppress_toggle = False
            row.set_sensitive(True)
            row.set_subtitle(self._available_subtitle(entry))

        row.set_sensitive(False)
        row.set_subtitle(_("Fetching…"))
        self._start_registry_install(entry, revert)

    def _start_registry_install(self, entry, on_fail):
        """Download + verify a registry package off-thread, then install it.
        Shared by the Available-Plugins toggle and the per-row update button.
        ``on_fail`` re-enables/reverts the triggering widget."""
        def worker():
            from .plugins.registry_client import download_package
            try:
                tmpdir = tempfile.mkdtemp(prefix='sshpilot-reg-')
                dest = os.path.join(tmpdir, 'package.zip')
                download_package(entry['download_url'], entry['checksum_url'], dest)
            except Exception as exc:
                GLib.idle_add(self._registry_fetch_failed, str(exc), on_fail)
                return
            GLib.idle_add(self._registry_fetch_done, dest, tmpdir, on_fail)
        threading.Thread(target=worker, daemon=True).start()

    def _registry_fetch_failed(self, msg, revert):
        revert()
        self._alert(_("Download failed"), msg)

    def _registry_fetch_done(self, dest, tmpdir, revert):
        try:
            # _install_plugin_from_zip extracts synchronously, so the downloaded
            # zip is no longer needed once it returns (consent runs off the
            # extracted copy).
            self._install_plugin_from_zip(Path(dest), verified=True, on_abort=revert)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # --- plugin install / remove -------------------------------------
    def _make_install_button(self):
        button = Gtk.MenuButton()
        button.set_icon_name('list-add-symbolic')
        button.set_tooltip_text(_("Install a plugin"))
        button.add_css_class('flat')
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        for label, handler in ((_("From folder…"), self._install_from_folder),
                               (_("From ZIP file…"), self._install_from_zip)):
            b = Gtk.Button(label=label)
            b.add_css_class('flat')
            b.set_halign(Gtk.Align.FILL)
            b.get_child().set_halign(Gtk.Align.START)
            b.connect('clicked', lambda _b, h=handler: (popover.popdown(), h()))
            box.append(b)
        popover.set_child(box)
        button.set_popover(popover)
        return button

    def _alert(self, heading, body):
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response('ok', _("OK"))
        dlg.present(self)

    def _add_permissions_info(self, row, permissions):
        """Add an info button to a plugin row listing its declared permissions."""
        if not permissions:
            return
        btn = Gtk.MenuButton()
        btn.set_icon_name('dialog-information-symbolic')
        btn.add_css_class('flat')
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_tooltip_text(_("Permissions"))
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for m in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(box, m)(8)
        head = Gtk.Label(label=_("Declared permissions"))
        head.add_css_class('heading')
        head.set_halign(Gtk.Align.START)
        box.append(head)
        for p in permissions:
            lbl = Gtk.Label(label="• " + str(p))
            lbl.set_halign(Gtk.Align.START)
            box.append(lbl)
        pop.set_child(box)
        btn.set_popover(pop)
        row.add_suffix(btn)

    def _plugin_homepage(self, info):
        """Source/homepage URL for an installed plugin: its manifest's
        ``homepage`` if present, else the registry entry's homepage."""
        url = getattr(info, 'homepage', None)
        if url:
            return url
        for entry in getattr(self, '_registry_entries', []):
            if entry.get('id') == info.plugin_id:
                return entry.get('homepage') or None
        return None

    def _make_plugin_switch(self, active, sensitive=True):
        """A right-aligned toggle for a plugin ActionRow (replaces SwitchRow's
        built-in switch, which is forced to the far end and can't be reordered)."""
        sw = Gtk.Switch()
        sw.set_active(bool(active))
        sw.set_valign(Gtk.Align.CENTER)
        sw.set_sensitive(bool(sensitive))
        return sw

    def _add_plugin_info(self, row, info):
        """Add an info button to an installed plugin row. Opens a dialog with
        the plugin's version, API level, declared permissions, and a clickable
        link to its source (replaces the old declare-only permissions popover)."""
        from sshpilot import icon_utils
        btn = Gtk.Button()
        btn.set_child(icon_utils.new_image_from_icon_name('info-outline-symbolic', 16))
        btn.add_css_class('flat')
        btn.add_css_class('image-button')
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_tooltip_text(_("Plugin info"))
        btn.connect('clicked', lambda _b, i=info: self._show_plugin_info(i))
        row.add_suffix(btn)

    def _show_plugin_info(self, info):
        dlg = Adw.AlertDialog(heading=info.name)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        def _field(caption, value):
            lbl = Gtk.Label()
            lbl.set_markup(
                f"<b>{GLib.markup_escape_text(caption)}:</b> "
                f"{GLib.markup_escape_text(str(value))}")
            lbl.set_halign(Gtk.Align.START)
            lbl.set_xalign(0)
            lbl.set_wrap(True)
            box.append(lbl)

        _field(_("Version"), getattr(info, 'version', None) or "—")
        if getattr(info, 'api_version', None) is not None:
            _field(_("API version"), info.api_version)
        if info.permissions:
            _field(_("Permissions"), ", ".join(info.permissions))

        url = self._plugin_homepage(info)
        if url:
            link = Gtk.LinkButton.new_with_label(url, _("View source ↗"))
            link.set_halign(Gtk.Align.START)
            box.append(link)
        else:
            _field(_("Source"), _("unknown"))

        dlg.set_extra_child(box)
        dlg.add_response('close', _("Close"))
        dlg.present(self)

    def _plugin_consent(self, *, name, permissions, action, on_accept,
                        on_decline=None, sha256=None, verified=False):
        """Trust dialog shown before enabling/installing third-party code:
        lists declared permissions and (for a .zip) the archive's SHA-256, with
        an optional expected-hash check."""
        dlg = Adw.AlertDialog(
            heading=_("{action} “{name}”?").format(action=action, name=name),
            body=_("Plugins run with full application privileges and are not "
                   "sandboxed. Only continue if you trust the source."))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        head = Gtk.Label(label=_("Requested permissions:") if permissions
                         else _("No special permissions declared."))
        head.add_css_class('heading')
        head.set_halign(Gtk.Align.START)
        head.set_wrap(True)
        body.append(head)
        if permissions:
            lbl = Gtk.Label(label="\n".join("• " + str(p) for p in permissions))
            lbl.set_halign(Gtk.Align.START)
            lbl.set_xalign(0)
            body.append(lbl)
        note = Gtk.Label(label=_("Permissions are declared by the author for "
                                 "transparency; they are not enforced. Changes "
                                 "take effect after restarting sshPilot."))
        note.add_css_class('dim-label')
        note.add_css_class('caption')
        note.set_halign(Gtk.Align.START)
        note.set_xalign(0)
        note.set_wrap(True)
        body.append(note)
        expected_entry = None
        if sha256 is not None:
            sha_head = Gtk.Label(label=_("SHA-256"))
            sha_head.add_css_class('heading')
            sha_head.set_halign(Gtk.Align.START)
            sha_val = Gtk.Label(label=sha256)
            sha_val.add_css_class('caption')
            sha_val.set_selectable(True)
            sha_val.set_wrap(True)
            sha_val.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            sha_val.set_xalign(0)
            sha_val.set_halign(Gtk.Align.START)
            body.append(sha_head)
            body.append(sha_val)
            if verified:
                ok = Gtk.Label(label=_("✓ Verified against the registry checksum"))
                ok.add_css_class('success')
                ok.add_css_class('caption')
                ok.set_halign(Gtk.Align.START)
                body.append(ok)
            else:
                expected_entry = Gtk.Entry()
                expected_entry.set_placeholder_text(_("Expected SHA-256 (optional)"))
                body.append(expected_entry)
        dlg.set_extra_child(body)
        dlg.add_response('cancel', _("Cancel"))
        dlg.add_response('ok', action)
        dlg.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)

        def _resp(_d, response):
            if response != 'ok':
                if on_decline:
                    on_decline()
                return
            if sha256 is not None and expected_entry is not None:
                want = expected_entry.get_text().strip()
                if want and want.lower() != sha256.lower():
                    self._alert(_("Checksum mismatch"),
                                _("The archive's SHA-256 doesn't match the "
                                  "expected value; install cancelled."))
                    if on_decline:
                        on_decline()
                    return
            on_accept()
        dlg.connect('response', _resp)
        dlg.present(self)

    def _install_from_folder(self):
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select plugin folder"))

        def _done(dlg, result):
            try:
                gfile = dlg.select_folder_finish(result)
            except Exception:
                return
            if gfile and gfile.get_path():
                self._install_plugin_from_dir(Path(gfile.get_path()))
        dialog.select_folder(self, None, _done)

    def _install_from_zip(self):
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select plugin ZIP"))
        try:
            filt = Gtk.FileFilter()
            filt.set_name(_("ZIP archives"))
            filt.add_pattern('*.zip')
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(filt)
            dialog.set_filters(filters)
        except Exception:
            pass

        def _done(dlg, result):
            try:
                gfile = dlg.open_finish(result)
            except Exception:
                return
            if gfile and gfile.get_path():
                self._install_plugin_from_zip(Path(gfile.get_path()))
        dialog.open(self, None, _done)

    @staticmethod
    def _locate_manifest_dir(root: Path):
        """The directory containing plugin.json: root itself, or its single
        plugin subdirectory (handles zips that wrap the plugin in a folder)."""
        if (root / 'plugin.json').is_file():
            return root
        candidates = [d for d in root.iterdir()
                      if d.is_dir() and (d / 'plugin.json').is_file()]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _sha256_file(path) -> str:
        h = hashlib.sha256()
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()

    @classmethod
    def _verify_sha256(cls, path, expected):
        """True/False if an expected hash is given, else None (nothing to check)."""
        expected = (expected or '').strip().lower()
        if not expected:
            return None
        return cls._sha256_file(path) == expected

    def _install_plugin_from_zip(self, zip_path: Path, verified: bool = False,
                                 on_abort=None):
        try:
            sha256 = self._sha256_file(zip_path)
            tmp = Path(tempfile.mkdtemp(prefix='sshpilot-plugin-'))
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.namelist():
                    # zip-slip guard: refuse paths escaping the temp dir.
                    dest = (tmp / member).resolve()
                    if not str(dest).startswith(str(tmp.resolve()) + os.sep) and dest != tmp.resolve():
                        self._alert(_("Install failed"),
                                    _("The archive contains an unsafe path."))
                        shutil.rmtree(tmp, ignore_errors=True)
                        if on_abort:
                            on_abort()
                        return
                zf.extractall(tmp)
            self._install_plugin_from_dir(tmp, _cleanup=tmp, sha256=sha256,
                                          verified=verified, on_abort=on_abort)
        except Exception as exc:
            logger.exception("plugin zip install failed")
            self._alert(_("Install failed"), str(exc))
            if on_abort:
                on_abort()

    def _install_plugin_from_dir(self, src: Path, _cleanup: Optional[Path] = None,
                                 sha256: Optional[str] = None, verified: bool = False,
                                 on_abort=None):
        from .plugins.loader import _user_plugin_dir, discover_plugins
        from .plugins.api import API_VERSION

        def _abort(heading=None, body=None):
            if _cleanup is not None:
                shutil.rmtree(_cleanup, ignore_errors=True)
            if heading is not None:
                self._alert(heading, body)
            if on_abort:
                on_abort()

        mdir = self._locate_manifest_dir(src)
        if mdir is None:
            return _abort(_("Install failed"), _("No plugin.json found."))
        try:
            meta = json.loads((mdir / 'plugin.json').read_text(encoding='utf-8'))
        except Exception as exc:
            return _abort(_("Install failed"), _("Invalid plugin.json: {}").format(exc))
        pid = meta.get('id')
        if not pid:
            return _abort(_("Install failed"), _("plugin.json has no 'id'."))
        if meta.get('api_version') != API_VERSION[0]:
            return _abort(_("Incompatible plugin"),
                          _("This plugin targets API v{}, but this app provides v{}.").format(
                              meta.get('api_version'), API_VERSION[0]))
        if not (mdir / '__init__.py').is_file():
            return _abort(_("Install failed"), _("The plugin has no __init__.py."))
        builtin_ids = {i.plugin_id for i in discover_plugins() if i.builtin}
        if pid in builtin_ids:
            return _abort(_("Install failed"),
                          _("'{}' conflicts with a built-in plugin.").format(pid))

        dest = _user_plugin_dir() / pid
        permissions = [str(p) for p in (meta.get('permissions') or []) if isinstance(p, str)]

        def _proceed():
            if dest.exists():
                confirm = Adw.AlertDialog(
                    heading=_("Replace plugin?"),
                    body=_("A plugin '{}' is already installed. Replace it?").format(pid))
                confirm.add_response('cancel', _("Cancel"))
                confirm.add_response('replace', _("Replace"))
                confirm.set_response_appearance('replace', Adw.ResponseAppearance.DESTRUCTIVE)

                def _resp(dlg, response):
                    if response == 'replace':
                        self._finish_install(mdir, dest, meta, _cleanup)
                    else:
                        _abort()
                confirm.connect('response', _resp)
                confirm.present(self)
                return
            self._finish_install(mdir, dest, meta, _cleanup)

        # Trust gate: show permissions + the archive's SHA-256 before installing.
        self._plugin_consent(name=meta.get('name', pid), permissions=permissions,
                             sha256=sha256, verified=verified, action=_("Install"),
                             on_accept=_proceed, on_decline=lambda: _abort())

    def _finish_install(self, mdir: Path, dest: Path, meta: dict,
                        cleanup: Optional[Path]):
        try:
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(mdir, dest)
        except Exception as exc:
            logger.exception("plugin copy failed")
            self._alert(_("Install failed"), str(exc))
            return
        finally:
            if cleanup is not None:
                shutil.rmtree(cleanup, ignore_errors=True)
        pid = meta['id']
        enabled = set(self.config.get_setting('plugins.enabled', []) or [])
        enabled.add(pid)
        self.config.set_setting('plugins.enabled', sorted(enabled))
        self._rebuild_plugins_page()
        self._alert(_("Plugin installed"),
                    _("Installed '{}'. Restart sshPilot to load it.").format(
                        meta.get('name', pid)))

    def _confirm_remove_plugin(self, info):
        confirm = Adw.AlertDialog(
            heading=_("Remove plugin?"),
            body=_("Delete '{}' from disk? This cannot be undone.").format(info.name))
        confirm.add_response('cancel', _("Cancel"))
        confirm.add_response('remove', _("Remove"))
        confirm.set_response_appearance('remove', Adw.ResponseAppearance.DESTRUCTIVE)
        confirm.connect('response',
                        lambda _d, r, i=info: self._remove_plugin(i) if r == 'remove' else None)
        confirm.present(self)

    def _remove_plugin(self, info):
        try:
            shutil.rmtree(info.path)
        except Exception as exc:
            logger.exception("plugin remove failed")
            self._alert(_("Remove failed"), str(exc))
            return
        for key in ('plugins.enabled', 'plugins.disabled'):
            ids = set(self.config.get_setting(key, []) or [])
            if info.plugin_id in ids:
                ids.discard(info.plugin_id)
                self.config.set_setting(key, sorted(ids))
        self._rebuild_plugins_page()
        self._alert(_("Plugin removed"),
                    _("Removed '{}'. Restart sshPilot to unload it if it was active.").format(
                        info.name))

    def _add_plugin_page_gear(self, row, plugin_id):
        """If an *active* plugin has registered a UI page, add a gear button to
        its row that opens that page (and closes Preferences)."""
        host = getattr(self.parent_window, 'plugin_host', None)
        ui = getattr(host, 'ui', None) if host else None
        pages = ui.page_ids_for_plugin(plugin_id) if ui else []
        if not pages:
            return
        from sshpilot import icon_utils
        gear = Gtk.Button()
        # Sized like the other icon buttons (16px symbolic + .image-button) so the
        # bundled icon doesn't change row height.
        gear.set_child(icon_utils.new_image_from_icon_name('settings-symbolic', 16))
        gear.add_css_class('flat')
        gear.add_css_class('image-button')
        gear.set_valign(Gtk.Align.CENTER)
        gear.set_tooltip_text(_("Open plugin page"))
        gear.connect('clicked', lambda _b, fid=pages[0]: self._open_plugin_page(fid))
        # Added as the first suffix → leftmost of the action cluster, before the
        # toggle (gear, toggle, info, delete).
        row.add_suffix(gear)

    def _open_plugin_page(self, full_id):
        host = getattr(self.parent_window, 'plugin_host', None)
        ui = getattr(host, 'ui', None) if host else None
        if ui is not None:
            ui.open_page(full_id)
        self.close()

    def _on_builtin_plugin_toggled(self, plugin_id, active, row=None):
        disabled = set(self.config.get_setting('plugins.disabled', []) or [])
        if active:
            disabled.discard(plugin_id)
        else:
            disabled.add(plugin_id)
        self.config.set_setting('plugins.disabled', sorted(disabled))
        if row is not None:
            row.set_subtitle(_("Restart sshPilot to apply"))

    def _set_user_plugin_enabled(self, plugin_id, on):
        enabled = set(self.config.get_setting('plugins.enabled', []) or [])
        if on:
            enabled.add(plugin_id)
        else:
            enabled.discard(plugin_id)
        self.config.set_setting('plugins.enabled', sorted(enabled))

    def _on_user_plugin_toggled(self, plugin_id, active, row=None,
                                permissions=None, switch=None):
        if getattr(self, '_suppress_toggle', False):
            return
        if not active:
            self._set_user_plugin_enabled(plugin_id, False)
            if row is not None:
                row.set_subtitle(_("Restart sshPilot to apply"))
            return

        # Enabling runs third-party code with full privileges — get consent first.
        def _accept():
            self._set_user_plugin_enabled(plugin_id, True)
            if row is not None:
                row.set_subtitle(_("Restart sshPilot to apply"))

        def _decline():
            # Revert the toggle (the manual switch; ActionRow has no set_active).
            target = switch if switch is not None else row
            if target is not None:
                self._suppress_toggle = True
                target.set_active(False)
                self._suppress_toggle = False

        self._plugin_consent(name=plugin_id, permissions=permissions or [],
                             action=_("Enable"), on_accept=_accept, on_decline=_decline)

    def _create_command_blocks_page(self):
        """Build the Command Blocks preferences page."""
        page = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(title=_("Behavior"))

        always_show_row = Adw.SwitchRow()
        always_show_row.set_title(_("Always Show Sidebar"))
        always_show_row.set_subtitle(_("Keep the commands sidebar open on startup"))
        always_show_row.set_active(bool(self.config.get_setting('command_blocks.always_show_sidebar', False)))
        always_show_row.connect('notify::active', lambda r, _: self.config.set_setting('command_blocks.always_show_sidebar', r.get_active()))
        group.add(always_show_row)

        insert_only_row = Adw.SwitchRow()
        insert_only_row.set_title(_("Insert Only (no execute)"))
        insert_only_row.set_subtitle(_("Paste the command into the terminal without running it"))
        insert_only_row.set_active(bool(self.config.get_setting('command_blocks.insert_only', False)))
        insert_only_row.connect('notify::active', lambda r, _: self.config.set_setting('command_blocks.insert_only', r.get_active()))
        group.add(insert_only_row)

        auto_hide_row = Adw.SwitchRow()
        auto_hide_row.set_title(_("Auto-hide Sidebar After Sending"))
        auto_hide_row.set_subtitle(_("Hide the command panel as soon as a command is sent"))
        auto_hide_row.set_active(bool(self.config.get_setting('command_blocks.auto_hide_sidebar', False)))
        self._cb_auto_hide_row = auto_hide_row
        auto_hide_row.connect(
            'notify::active',
            lambda r, _p: self.config.set_setting('command_blocks.auto_hide_sidebar', r.get_active()),
        )
        group.add(auto_hide_row)

        page.add(group)
        return page

    def _create_group_display_preview(self, mode: str, title: str):
        """Create a small sample widget that illustrates the layout mode."""
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        wrapper.add_css_class('group-display-preview-container')
        wrapper.set_hexpand(True)

        title_label = Gtk.Label(label=title)
        title_label.set_halign(Gtk.Align.START)
        title_label.set_xalign(0.0)
        title_label.add_css_class('group-display-preview-title')
        wrapper.append(title_label)

        sample_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sample_box.set_hexpand(True)

        parent_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        parent_row.add_css_class('group-display-preview-parent')
        parent_row.set_margin_start(0)
        parent_row.set_margin_end(8)

        from sshpilot import icon_utils
        parent_icon = icon_utils.new_image_from_icon_name('folder-symbolic')
        parent_row.append(parent_icon)

        parent_labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        parent_title = Gtk.Label(label='Production Servers')
        parent_title.set_halign(Gtk.Align.START)
        parent_title.set_xalign(0.0)
        parent_title.add_css_class('group-display-preview-parent-title')

        parent_sub = Gtk.Label(label='3 connections')
        parent_sub.set_halign(Gtk.Align.START)
        parent_sub.set_xalign(0.0)
        parent_sub.add_css_class('group-display-preview-parent-subtitle')

        parent_labels.append(parent_title)
        parent_labels.append(parent_sub)
        parent_row.append(parent_labels)

        tinted = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        tinted.add_css_class('group-display-preview-row')
        tinted.set_hexpand(True)
        tinted.set_margin_top(2)
        tinted.set_margin_bottom(2)
        tinted.set_margin_end(8)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        if mode == 'fullwidth':
            tinted.set_margin_start(0)
            content_box.set_margin_start(24)
        else:  # nested mode shifts entire row
            tinted.set_margin_start(24)
            content_box.set_margin_start(0)

        title_text = Gtk.Label(label='db-backups')
        title_text.set_halign(Gtk.Align.START)
        title_text.set_xalign(0.0)
        title_text.add_css_class('group-display-preview-row-title')

        subtitle_text = Gtk.Label(label='ops@example.com')
        subtitle_text.set_halign(Gtk.Align.START)
        subtitle_text.set_xalign(0.0)
        subtitle_text.add_css_class('group-display-preview-row-subtitle')

        content_box.append(title_text)
        content_box.append(subtitle_text)

        tinted.append(content_box)
        sample_box.append(parent_row)
        sample_box.append(tinted)
        wrapper.append(sample_box)

        return wrapper, tinted

    def _update_group_display_preview(self, active_mode: str):
        preview_map = getattr(self, '_group_display_preview_rows', None)
        if not preview_map:
            return

        valid_modes = getattr(self, '_group_display_modes', ['fullwidth', 'nested'])
        if active_mode not in valid_modes:
            active_mode = valid_modes[0]

        for mode, tinted in preview_map.items():
            if not tinted:
                continue
            if mode == active_mode:
                tinted.add_css_class('active')
            else:
                tinted.remove_css_class('active')

        stack = getattr(self, '_group_display_preview_stack', None)
        if stack:
            try:
                stack.set_visible_child_name(active_mode)
            except Exception:
                pass

    def _sync_group_display_toggle_group(self, value):
        controller = getattr(self, '_group_display_toggle_controller', None)
        if controller is None:
            return

        valid_modes = getattr(self, '_group_display_modes', ['fullwidth', 'nested'])
        normalized = 'fullwidth'
        try:
            normalized = str(value).lower()
        except Exception:
            pass
        if normalized not in valid_modes:
            normalized = 'fullwidth'

        try:
            current_active = controller.get_active_name()
        except Exception:
            current_active = 'fullwidth'

        if current_active == normalized:
            self._update_group_display_preview(normalized)
            return

        self._group_display_toggle_sync = True
        try:
            controller.set_active_name(normalized)
        finally:
            self._group_display_toggle_sync = False

        self._update_group_display_preview(normalized)

    def _sync_group_color_display_row(self, value):
        if not hasattr(self, 'group_color_display_row') or self.group_color_display_row is None:
            return

        try:
            normalized = str(value).lower()
        except Exception:
            normalized = 'fill'

        if normalized not in getattr(self, '_group_color_display_values', ['fill', 'badge']):
            normalized = 'fill'

        target_index = self._group_color_display_values.index(normalized)
        if self.group_color_display_row.get_selected() == target_index:
            return

        self._group_display_sync = True
        try:
            self.group_color_display_row.set_selected(target_index)
        finally:
            self._group_display_sync = False

    def _sync_group_tab_color_switch(self, value):
        if not hasattr(self, 'group_color_tab_switch') or self.group_color_tab_switch is None:
            return

        desired_state = bool(value)
        if self.group_color_tab_switch.get_active() == desired_state:
            return

        self._group_tab_color_sync = True
        try:
            self.group_color_tab_switch.set_active(desired_state)
        finally:
            self._group_tab_color_sync = False

    def _sync_group_terminal_color_switch(self, value):
        if not hasattr(self, 'group_color_terminal_switch') or self.group_color_terminal_switch is None:
            return

        desired_state = bool(value)
        if self.group_color_terminal_switch.get_active() == desired_state:
            return

        self._group_terminal_color_sync = True
        try:
            self.group_color_terminal_switch.set_active(desired_state)
        finally:
            self._group_terminal_color_sync = False

    def _on_config_setting_changed(self, _config, key, value):
        if key == 'ui.group_color_display':
            self._sync_group_color_display_row(value)
            self._trigger_sidebar_refresh()
        elif key == 'ui.group_row_display':
            self._sync_group_display_toggle_group(value)
            self._trigger_sidebar_refresh()
        elif key == 'ui.use_group_color_in_tab':
            self._sync_use_group_color_in_tab(value)
            self._trigger_terminal_style_refresh()
        elif key == 'ui.use_group_color_in_terminal':
            self._sync_use_group_color_in_terminal(value)
            self._trigger_terminal_style_refresh()
        elif key == 'terminal.encoding':
            if self._suppress_encoding_config_handler:
                return
            # Don't show toast if the change was initiated by user selection
            notify_user = not self._user_initiated_encoding_change
            self._user_initiated_encoding_change = False  # Reset flag
            GLib.idle_add(self._sync_encoding_row_selection, value or '', notify_user)

    def _sync_use_group_color_in_tab(self, value):
        if not hasattr(self, 'tab_group_color_row') or self.tab_group_color_row is None:
            return

        target_state = bool(value)
        if self.tab_group_color_row.get_active() == target_state:
            return

        self._tab_color_sync = True
        try:
            self.tab_group_color_row.set_active(target_state)
        finally:
            self._tab_color_sync = False

    def _sync_use_group_color_in_terminal(self, value):
        if not hasattr(self, 'terminal_group_color_row') or self.terminal_group_color_row is None:
            return

        target_state = bool(value)
        if self.terminal_group_color_row.get_active() == target_state:
            return

        self._terminal_color_sync = True
        try:
            self.terminal_group_color_row.set_active(target_state)
        finally:
            self._terminal_color_sync = False


    def _on_destroy(self, *_args):
        if getattr(self, '_config_signal_id', None) and hasattr(self.config, 'disconnect'):
            try:
                self.config.disconnect(self._config_signal_id)
            except Exception:
                pass
            self._config_signal_id = None

    def on_accent_color_changed(self, color_button):
        """Handle accent color change"""
        color = color_button.get_rgba()
        color_str = color.to_string()
        self.config.set_setting('accent-color-override', color_str)
        logger.info(f"Accent color changed to: {color_str}")
        self.refresh_color_buttons()
        self.apply_color_overrides()

    def on_reset_colors_clicked(self, button):
        """Reset color overrides to default"""
        self.config.set_setting('accent-color-override', None)
        self.refresh_color_buttons()
        logger.info("Accent color override reset to default")
        self.apply_color_overrides()

    def refresh_color_buttons(self):
        """Update color button appearance to reflect settings"""
        self._set_color_button(
            self.accent_color_button,
            self.accent_color_row,
            'accent-color-override',
            Gdk.RGBA(0.2, 0.5, 0.9, 1.0),
            'Using system accent color',
        )

    def _set_color_button(self, button, row, setting_name, default_rgba, default_subtitle):
        saved_color = self.config.get_setting(setting_name, None)
        if saved_color:
            color = Gdk.RGBA()
            color.parse(saved_color)
            button.set_rgba(color)
            button.set_opacity(1.0)
            row.set_subtitle(saved_color)
        else:
            button.set_rgba(default_rgba)
            button.set_opacity(0.4)
            row.set_subtitle(default_subtitle)

    def apply_color_overrides(self):
        """Apply color overrides to the application"""
        try:
            accent_color = self.config.get_setting('accent-color-override', None)

            if not accent_color:
                self.remove_color_override_provider()
                return

            # Build CSS with accent color overrides
            css_rules = [
                f"@define-color accent_color {accent_color};",
                f"@define-color accent_bg_color {accent_color};",
                "@define-color accent_fg_color white;",
                f"@define-color theme_selected_bg_color {accent_color};",
                "@define-color theme_selected_fg_color white;",
                f"@define-color theme_unfocused_selected_bg_color {accent_color};",
                "@define-color theme_unfocused_selected_fg_color white;",
            ]

            if css_rules:
                # Add specific CSS rules for row selection
                css_rules.append("")
                css_rules.append("/* Force row selection to use custom colors */")
                css_rules.append("row:selected {")
                css_rules.append("  background-color: @theme_selected_bg_color;")
                css_rules.append("  color: @theme_selected_fg_color;")
                css_rules.append("}")
                css_rules.append("")
                css_rules.append("row:selected:focus {")
                css_rules.append("  background-color: @theme_selected_bg_color;")
                css_rules.append("  color: @theme_selected_fg_color;")
                css_rules.append("}")
                css_rules.append("")
                css_rules.append("list row:selected {")
                css_rules.append("  background-color: @theme_selected_bg_color;")
                css_rules.append("  color: @theme_selected_fg_color;")
                css_rules.append("}")
                
                # Apply custom CSS
                provider = Gtk.CssProvider()
                css = "\n".join(css_rules)
                provider.load_from_data(css.encode('utf-8'))
                
                # Add provider to display
                display = Gdk.Display.get_default()
                if display:
                    # Remove any existing color override provider first
                    self.remove_color_override_provider()

                    Gtk.StyleContext.add_provider_for_display(
                        display, 
                        provider, 
                        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                    # Store provider reference for cleanup
                    display._color_override_provider = provider
                    logger.info("Applied accent color override")
            else:
                # Remove any existing color override provider
                self.remove_color_override_provider()
                
        except Exception as e:
            logger.error(f"Failed to apply color overrides: {e}")

    def remove_color_override_provider(self):
        """Remove color override CSS provider"""
        try:
            display = Gdk.Display.get_default()
            if display and hasattr(display, '_color_override_provider'):
                Gtk.StyleContext.remove_provider_for_display(display, display._color_override_provider)
                delattr(display, '_color_override_provider')
        except Exception as e:
            logger.error(f"Failed to remove color override provider: {e}")

    def save_advanced_ssh_settings(self):
        """Persist advanced SSH settings from the preferences UI"""
        try:
            connect_timeout = None
            connection_attempts = None
            keepalive_interval = None
            keepalive_count = None
            strict_host_value = ''
            batch_mode_enabled = False
            compression_enabled = False
            verbosity_value = 0
            debug_enabled = False

            if hasattr(self, 'connect_timeout_row'):
                connect_timeout_value = int(self.connect_timeout_row.get_value())
                if connect_timeout_value <= 0:
                    connect_timeout = None
                else:
                    connect_timeout = connect_timeout_value
                self.config.set_setting('ssh.connection_timeout', connect_timeout)
            if hasattr(self, 'connection_attempts_row'):
                connection_attempts_value = int(self.connection_attempts_row.get_value())
                if connection_attempts_value <= 0:
                    connection_attempts = None
                else:
                    connection_attempts = connection_attempts_value
                self.config.set_setting('ssh.connection_attempts', connection_attempts)
            if hasattr(self, 'apply_default_keepalive_row'):
                self.config.set_setting(
                    'ssh.apply_default_keepalive',
                    bool(self.apply_default_keepalive_row.get_active()),
                )
            if hasattr(self, 'keepalive_interval_row'):
                keepalive_interval_value = int(self.keepalive_interval_row.get_value())
                if keepalive_interval_value <= 0:
                    keepalive_interval = None
                else:
                    keepalive_interval = keepalive_interval_value
                self.config.set_setting('ssh.keepalive_interval', keepalive_interval)
            if hasattr(self, 'keepalive_count_row'):
                keepalive_count_value = int(self.keepalive_count_row.get_value())
                if keepalive_count_value <= 0:
                    keepalive_count = None
                else:
                    keepalive_count = keepalive_count_value
                self.config.set_setting('ssh.keepalive_count_max', keepalive_count)
            if hasattr(self, 'strict_host_row'):
                options = ["accept-new", "yes", "no", "ask"]
                idx = self.strict_host_row.get_selected()
                strict_host_value = options[idx] if 0 <= idx < len(options) else 'accept-new'
                self.config.set_setting('ssh.strict_host_key_checking', strict_host_value)
            if hasattr(self, 'batch_mode_row'):
                batch_mode_enabled = bool(self.batch_mode_row.get_active())
                self.config.set_setting('ssh.batch_mode', batch_mode_enabled)
            if hasattr(self, 'compression_row'):
                compression_enabled = bool(self.compression_row.get_active())
                self.config.set_setting('ssh.compression', compression_enabled)
            if hasattr(self, 'verbosity_row'):
                verbosity_value = int(self.verbosity_row.get_value())
                self.config.set_setting('ssh.verbosity', verbosity_value)
            if hasattr(self, 'debug_enabled_row'):
                debug_enabled = bool(self.debug_enabled_row.get_active())
                self.config.set_setting('ssh.debug_enabled', debug_enabled)
            controlmaster_enabled = False
            if hasattr(self, 'controlmaster_row'):
                controlmaster_enabled = bool(self.controlmaster_row.get_active())
                self.config.set_setting('ssh.controlmaster', controlmaster_enabled)

            overrides: List[str] = []
            if batch_mode_enabled:
                overrides.extend(['-o', 'BatchMode=yes'])
            if connect_timeout is not None:
                overrides.extend(['-o', f'ConnectTimeout={connect_timeout}'])
            if connection_attempts is not None:
                overrides.extend(['-o', f'ConnectionAttempts={connection_attempts}'])
            if keepalive_interval is not None:
                overrides.extend(['-o', f'ServerAliveInterval={keepalive_interval}'])
            if keepalive_count is not None:
                overrides.extend(['-o', f'ServerAliveCountMax={keepalive_count}'])
            if strict_host_value:
                overrides.extend(['-o', f'StrictHostKeyChecking={strict_host_value}'])
            if compression_enabled:
                overrides.append('-C')

            safe_verbosity = max(0, min(3, verbosity_value))
            for _ in range(safe_verbosity):
                overrides.append('-v')

            log_level = None
            if safe_verbosity == 1:
                log_level = 'VERBOSE'
            elif safe_verbosity == 2:
                log_level = 'DEBUG2'
            elif safe_verbosity >= 3:
                log_level = 'DEBUG3'
            elif debug_enabled:
                log_level = 'DEBUG'

            if log_level:
                overrides.extend(['-o', f'LogLevel={log_level}'])

            # Connection multiplexing applies to every connection via the same
            # shared socket the per-plugin pool uses, so they never conflict.
            if controlmaster_enabled:
                from .ssh_multiplex import controlmaster_args
                overrides.extend(controlmaster_args())

            self.config.set_setting('ssh.ssh_overrides', overrides)
            if getattr(self, 'force_internal_file_manager_row', None) is not None:
                self.config.set_setting(
                    'file_manager.force_internal',
                    bool(self.force_internal_file_manager_row.get_active()),
                )
            if getattr(self, 'open_file_manager_externally_row', None) is not None:
                self.config.set_setting(
                    'file_manager.open_externally',
                    bool(self.open_file_manager_externally_row.get_active()),
                )
            if getattr(self, 'sftp_keepalive_interval_row', None) is not None:
                interval_value = int(self.sftp_keepalive_interval_row.get_value())
                if interval_value < 0:
                    interval_value = 0
                self.config.set_setting('file_manager.sftp_keepalive_interval', interval_value)
            if getattr(self, 'sftp_keepalive_count_row', None) is not None:
                keepalive_count_value = int(self.sftp_keepalive_count_row.get_value())
                if keepalive_count_value < 0:
                    keepalive_count_value = 0
                self.config.set_setting('file_manager.sftp_keepalive_count_max', keepalive_count_value)
            if getattr(self, 'sftp_connect_timeout_row', None) is not None:
                connect_timeout_value = int(self.sftp_connect_timeout_row.get_value())
                if connect_timeout_value < 0:
                    connect_timeout_value = 0
                self.config.set_setting('file_manager.sftp_connect_timeout', connect_timeout_value)

            manager = None
            if self.parent_window and hasattr(self.parent_window, 'connection_manager'):
                manager = self.parent_window.connection_manager
            if manager and hasattr(manager, 'invalidate_cached_commands'):
                manager.invalidate_cached_commands()
        except Exception as e:
            logger.error(f"Failed to save advanced SSH settings: {e}")

    def _apply_default_advanced_settings(self):
        """Restore advanced SSH settings to defaults and update the UI."""
        try:
            defaults = self.config.get_default_config().get('ssh', {})

            if hasattr(self, 'connect_timeout_row'):
                self.config.set_setting('ssh.connection_timeout', None)
                self.connect_timeout_row.set_value(0)
            if hasattr(self, 'connection_attempts_row'):
                self.config.set_setting('ssh.connection_attempts', None)
                self.connection_attempts_row.set_value(0)
            if hasattr(self, 'apply_default_keepalive_row'):
                default_apply = bool(defaults.get('apply_default_keepalive', True))
                self.config.set_setting('ssh.apply_default_keepalive', default_apply)
                self.apply_default_keepalive_row.set_active(default_apply)
            if hasattr(self, 'keepalive_interval_row'):
                self.config.set_setting('ssh.keepalive_interval', None)
                self.keepalive_interval_row.set_value(0)
            if hasattr(self, 'keepalive_count_row'):
                self.config.set_setting('ssh.keepalive_count_max', None)
                self.keepalive_count_row.set_value(0)
            if hasattr(self, 'strict_host_row'):
                try:
                    self.strict_host_row.set_selected(["accept-new", "yes", "no", "ask"].index('accept-new'))
                except ValueError:
                    self.strict_host_row.set_selected(0)
                self.config.set_setting('ssh.strict_host_key_checking', 'accept-new')
            self.config.set_setting('ssh.auto_add_host_keys', defaults.get('auto_add_host_keys'))
            if hasattr(self, 'batch_mode_row'):
                self.config.set_setting('ssh.batch_mode', bool(defaults.get('batch_mode', False)))
                self.batch_mode_row.set_active(bool(defaults.get('batch_mode', False)))
            if hasattr(self, 'compression_row'):
                self.config.set_setting('ssh.compression', bool(defaults.get('compression', False)))
                self.compression_row.set_active(bool(defaults.get('compression', False)))
            if hasattr(self, 'verbosity_row'):
                self.config.set_setting('ssh.verbosity', defaults.get('verbosity'))
                self.verbosity_row.set_value(int(defaults.get('verbosity', 0)))
            if hasattr(self, 'debug_enabled_row'):
                self.config.set_setting('ssh.debug_enabled', bool(defaults.get('debug_enabled', False)))
                self.debug_enabled_row.set_active(bool(defaults.get('debug_enabled', False)))

            file_manager_defaults = self.config.get_default_config().get('file_manager', {})
            default_force_internal = bool(file_manager_defaults.get('force_internal', False))
            self.config.set_setting('file_manager.force_internal', default_force_internal)
            if getattr(self, 'force_internal_file_manager_row', None) is not None:
                self.force_internal_file_manager_row.set_active(default_force_internal)
            default_open_external = bool(file_manager_defaults.get('open_externally', False))
            self.config.set_setting('file_manager.open_externally', default_open_external)
            if getattr(self, 'open_file_manager_externally_row', None) is not None:
                self.open_file_manager_externally_row.set_active(default_open_external)
            keepalive_interval_default = int(file_manager_defaults.get('sftp_keepalive_interval', 0) or 0)
            self.config.set_setting('file_manager.sftp_keepalive_interval', max(0, keepalive_interval_default))
            if getattr(self, 'sftp_keepalive_interval_row', None) is not None:
                self.sftp_keepalive_interval_row.set_value(max(0, keepalive_interval_default))
            keepalive_count_default = int(file_manager_defaults.get('sftp_keepalive_count_max', 0) or 0)
            self.config.set_setting('file_manager.sftp_keepalive_count_max', max(0, keepalive_count_default))
            if getattr(self, 'sftp_keepalive_count_row', None) is not None:
                self.sftp_keepalive_count_row.set_value(max(0, keepalive_count_default))
            connect_timeout_default = int(file_manager_defaults.get('sftp_connect_timeout', 0) or 0)
            self.config.set_setting('file_manager.sftp_connect_timeout', max(0, connect_timeout_default))
            if getattr(self, 'sftp_connect_timeout_row', None) is not None:
                self.sftp_connect_timeout_row.set_value(max(0, connect_timeout_default))
            self._update_external_file_manager_row()

            self.save_advanced_ssh_settings()
        except Exception as e:
            logger.error(f"Failed to apply default advanced SSH settings: {e}")

    def on_reset_advanced_ssh(self, *args):
        """Reset only advanced SSH keys to defaults and update UI."""
        try:
            self._apply_default_advanced_settings()
        except Exception as e:
            logger.error(f"Failed to reset advanced SSH settings: {e}")

    def on_operation_mode_toggled(self, button):
        """Handle switching between default and isolated SSH modes"""
        try:
            if not button.get_active():
                return

            use_isolated = self.isolated_mode_radio.get_active()

            self.config.set_setting('ssh.use_isolated_config', bool(use_isolated))

            self._update_operation_mode_styles()

            parent_window = self.get_transient_for()
            if parent_window and hasattr(parent_window, 'connection_manager'):
                parent_window.connection_manager.set_isolated_mode(bool(use_isolated))

            # Offer an immediate restart to apply the mode change
            if parent_window:
                use_alert = hasattr(Adw, 'AlertDialog')
                if use_alert:
                    rdialog = Adw.AlertDialog(
                        heading=_("Restart Required"),
                        body=_(
                            "Restart SSH Pilot to fully apply the new operation mode."
                        ),
                    )
                else:
                    rdialog = Adw.MessageDialog(
                        transient_for=parent_window,
                        modal=True,
                        heading=_("Restart Required"),
                        body=_(
                            "Restart SSH Pilot to fully apply the new operation mode."
                        ),
                    )
                rdialog.add_response('later', _("Later"))
                rdialog.add_response('restart', _("Restart Now"))
                rdialog.set_default_response('restart')
                rdialog.set_close_response('later')
                try:
                    rdialog.set_response_appearance(
                        'restart', Adw.ResponseAppearance.SUGGESTED
                    )
                except Exception:
                    pass

                def _on_restart_response(_d, response):
                    if response == 'restart':
                        from .platform_utils import restart_app
                        restart_app()

                rdialog.connect('response', _on_restart_response)
                if use_alert:
                    rdialog.present(self)
                else:
                    rdialog.present()

        except Exception as e:
            logger.error(f"Failed to toggle isolated SSH mode: {e}")

    def _update_operation_mode_styles(self):
        """Ensure neither operation mode row appears disabled."""
        for row in (self.default_mode_row, self.isolated_mode_row):
            row.remove_css_class('dim-label')

    def get_theme_name_mapping(self):
        """Get mapping between display names and config keys"""
        return {
            "Default": "default",
            "Black on White": "black_on_white",
            "Solarized Dark": "solarized_dark",
            "Solarized Light": "solarized_light",
            "Monokai": "monokai",
            "Dracula": "dracula",
            "Nord": "nord",
            "Gruvbox Dark": "gruvbox_dark",
            "One Dark": "one_dark",
            "Tomorrow Night": "tomorrow_night",
            "Material Dark": "material_dark",
            "Rosé Pine": "rose_pine",
            "Rosé Pine Moon": "rose_pine_moon",
            "Rosé Pine Dawn": "rose_pine_dawn",
            "Catppuccin Latte": "catppuccin_latte",
            "Catppuccin Frappé": "catppuccin_frappe",
            "Catppuccin Macchiato": "catppuccin_macchiato",
            "Catppuccin Mocha": "catppuccin_mocha",
        }
    
    def get_reverse_theme_mapping(self):
        """Get mapping from config keys to display names"""
        mapping = self.get_theme_name_mapping()
        return {v: k for k, v in mapping.items()}

    def _initialize_encoding_selector(self, appearance_group):
        self.encoding_row = Adw.ComboRow()
        self.encoding_row.set_title("Encoding")
        self.encoding_row.set_subtitle("Character encoding for the integrated terminal")

        self._encoding_options = self._collect_supported_encodings()
        self._encoding_codes = [code for code, _ in self._encoding_options]

        encoding_list = Gtk.StringList()
        for code, description in self._encoding_options:
            display_label = description or code
            if description and description != code:
                display_label = f"{code} — {description}"
            encoding_list.append(display_label)

        self.encoding_row.set_model(encoding_list)
        self.encoding_row.connect('notify::selected', self.on_encoding_selection_changed)
        appearance_group.add(self.encoding_row)

        current_encoding = self.config.get_setting('terminal.encoding', 'UTF-8')
        self._sync_encoding_row_selection(current_encoding, notify_user=True)
        
        # Update visibility based on current backend
        self._update_encoding_row_visibility()

    def _collect_supported_encodings(self):
        """Collect supported encodings based on current backend"""
        current_backend = self.config.get_setting('terminal.backend', 'vte').lower()
        
        # For PyXterm.js backend, provide xterm.js compatible encodings
        # According to https://xtermjs.org/docs/guides/encoding/
        # xterm.js uses UTF-8/UTF-16 natively, legacy encodings via luit/iconv
        if current_backend == 'pyxterm':
            # xterm.js native encodings
            options = [
                ('UTF-8', 'Unicode (UTF-8)'),
                ('UTF-16', 'Unicode (UTF-16)'),
            ]
            
            # Add common legacy encodings that can be handled via luit/iconv
            # These will be transcoded at the PTY bridge level
            legacy_encodings = [
                ('ISO-8859-1', 'Latin-1 (ISO-8859-1)'),
                ('ISO-8859-15', 'Latin-9 (ISO-8859-15)'),
                ('Windows-1252', 'Western European (Windows-1252)'),
                ('GB2312', 'Simplified Chinese (GB2312)'),
                ('GBK', 'Chinese (GBK)'),
                ('GB18030', 'Chinese (GB18030)'),
                ('Big5', 'Traditional Chinese (Big5)'),
                ('Shift_JIS', 'Japanese (Shift_JIS)'),
                ('EUC-JP', 'Japanese (EUC-JP)'),
                ('EUC-KR', 'Korean (EUC-KR)'),
                ('KOI8-R', 'Cyrillic (KOI8-R)'),
                ('KOI8-U', 'Ukrainian (KOI8-U)'),
            ]
            options.extend(legacy_encodings)
            return options
        
        # For VTE backend, use VTE's native encoding support
        options = []
        try:
            terminal = Vte.Terminal()
            encodings = terminal.get_encodings() or []
            for item in encodings:
                code = None
                description = None
                if isinstance(item, (list, tuple)):
                    if len(item) >= 1:
                        code = item[0]
                    if len(item) >= 2:
                        description = item[1]
                elif isinstance(item, str):
                    code = item
                if code:
                    options.append((code, description or code))
        except Exception as exc:  # pragma: no cover - depends on VTE runtime
            logger.debug("Unable to retrieve VTE encodings: %s", exc)

        if not options:
            options = [('UTF-8', 'Unicode (UTF-8)')]
        else:
            codes = [code for code, _ in options]
            if 'UTF-8' in codes:
                utf_index = codes.index('UTF-8')
                if utf_index != 0:
                    options.insert(0, options.pop(utf_index))
            else:
                options.insert(0, ('UTF-8', 'Unicode (UTF-8)'))

        return options

    def _sync_encoding_row_selection(self, encoding, notify_user=False):
        if not hasattr(self, 'encoding_row') or self.encoding_row is None:
            return

        requested = encoding.strip() if isinstance(encoding, str) else ''
        fallback_code = self._encoding_codes[0] if self._encoding_codes else 'UTF-8'
        fallback_required = False
        index = 0

        canonical_index = None
        if requested:
            if requested in self._encoding_codes:
                canonical_index = self._encoding_codes.index(requested)
            else:
                lower_requested = requested.lower()
                for idx, code in enumerate(self._encoding_codes):
                    if code.lower() == lower_requested:
                        canonical_index = idx
                        break
            if canonical_index is not None:
                index = canonical_index
            else:
                fallback_required = True
        else:
            canonical_index = 0 if self._encoding_codes else None

        if canonical_index is None:
            try:
                index = self._encoding_codes.index(fallback_code)
            except ValueError:
                index = 0
            target_code = fallback_code
        else:
            target_code = self._encoding_codes[index]
            if fallback_required:
                target_code = fallback_code
                index = self._encoding_codes.index(target_code) if target_code in self._encoding_codes else 0
        if fallback_required and canonical_index is None:
            target_code = fallback_code
            index = self._encoding_codes.index(target_code) if target_code in self._encoding_codes else 0

        if self.encoding_row.get_selected() != index:
            self._encoding_selection_sync = True
            try:
                self.encoding_row.set_selected(index)
            finally:
                self._encoding_selection_sync = False

        if not requested:
            self._update_encoding_config_if_needed(target_code)
        elif fallback_required:
            if notify_user:
                self._handle_invalid_encoding_selection(requested or encoding, target_code)
            self._update_encoding_config_if_needed(target_code)
        elif target_code != requested:
            self._update_encoding_config_if_needed(target_code)

    def _handle_invalid_encoding_selection(self, requested, fallback):
        message = f"Encoding '{requested}' is not available. Using {fallback} instead."
        logger.warning(message)
        self._show_toast(message)

    def _show_toast(self, message):
        try:
            toast = Adw.Toast.new(message)
            self.add_toast(toast)
        except Exception:
            # Fallback to logging when toast overlay isn't available
            logger.info(message)

    def _update_encoding_config_if_needed(self, target_code):
        current_value = self.config.get_setting('terminal.encoding', 'UTF-8')
        if current_value == target_code:
            return
        self._suppress_encoding_config_handler = True
        try:
            self.config.set_setting('terminal.encoding', target_code)
        finally:
            self._suppress_encoding_config_handler = False

    def _update_encoding_row_visibility(self):
        """Update encoding row visibility based on current backend"""
        if not hasattr(self, 'encoding_row') or self.encoding_row is None:
            return
        
        current_backend = self.config.get_setting('terminal.backend', 'vte').lower()
        
        # Hide encoding dropdown for VTE backend (VTE handles encoding internally)
        # Show encoding dropdown for PyXterm.js backend (encoding handled at PTY bridge level)
        if current_backend == 'vte':
            self.encoding_row.set_visible(False)
            logger.debug("Hiding encoding dropdown for VTE backend")
        elif current_backend == 'pyxterm':
            self.encoding_row.set_visible(True)
            # Refresh encoding options for PyXterm.js
            self._encoding_options = self._collect_supported_encodings()
            self._encoding_codes = [code for code, _ in self._encoding_options]
            
            # Update the model
            encoding_list = Gtk.StringList()
            for code, description in self._encoding_options:
                display_label = description or code
                if description and description != code:
                    display_label = f"{code} — {description}"
                encoding_list.append(display_label)
            self.encoding_row.set_model(encoding_list)
            
            # Sync selection
            current_encoding = self.config.get_setting('terminal.encoding', 'UTF-8')
            self._sync_encoding_row_selection(current_encoding, notify_user=False)
            logger.debug("Showing encoding dropdown for PyXterm.js backend")
        else:
            # Default: show for unknown backends
            self.encoding_row.set_visible(True)

    def on_encoding_selection_changed(self, combo_row, _param):
        if self._encoding_selection_sync:
            return

        index = combo_row.get_selected()
        if index < 0 or index >= len(self._encoding_codes):
            return

        target_code = self._encoding_codes[index]
        # Mark this as a user-initiated change to suppress toast
        self._user_initiated_encoding_change = True
        self._update_encoding_config_if_needed(target_code)

    def _detect_pyxterm_backend(self):
        external_error: Optional[str] = None

        try:
            spec = importlib.util.find_spec('pyxtermjs')
            if spec is None:
                external_error = 'pyxtermjs module not found'
            else:
                __import__('pyxtermjs')
                return True, None
        except Exception as exc:
            external_error = str(exc)

        vendored_error: Optional[str] = None

        try:
            vendored_spec = importlib.util.find_spec('sshpilot.vendor.pyxtermjs')
            if vendored_spec is not None:
                return True, None
            vendored_error = 'vendored pyxtermjs module not found'
        except Exception as vendored_exc:
            vendored_error = str(vendored_exc)

        message_parts = [part for part in (external_error, vendored_error) if part]
        if not message_parts:
            message_parts.append('pyxtermjs backend unavailable')

        return False, '; '.join(message_parts)

    def _build_backend_choices(self):
        choices = [
            {
                'id': 'vte',
                'label': 'VTE (default)',
                'description': 'Native VTE-based terminal',
                'available': True,
                'error': None,
            }
        ]
        # PyXterm.js is not supported on macOS (webkitgtk is Linux-only)
        if not is_macos():
            pyxterm_available, pyxterm_error = self._detect_pyxterm_backend()
            if pyxterm_available:
                choices.append(
                    {
                        'id': 'pyxterm',
                        'label': 'PyXterm.js',
                        'description': 'Web-based terminal (pyxtermjs)',
                        'available': True,
                        'error': None,
                    }
                )
            else:
                choices.append(
                    {
                        'id': 'pyxterm',
                        'label': 'PyXterm.js (requires pyxtermjs)',
                        'description': 'pyxtermjs package not available',
                        'available': False,
                        'error': pyxterm_error,
                    }
                )
        return choices

    def _update_backend_row_subtitle(self, index: int):
        if not hasattr(self, 'backend_row'):
            return
        if 0 <= index < len(self._backend_choice_data):
            desc = self._backend_choice_data[index].get('description')
            if desc:
                self.backend_row.set_subtitle(desc)

    def _on_backend_row_changed(self, combo_row, _param):
        index = combo_row.get_selected()
        if index < 0 or index >= len(self._backend_choice_data):
            return
        option = self._backend_choice_data[index]
        if not option.get('available'):
            combo_row.set_selected(self._backend_last_valid_index)
            logger.warning("PyXterm backend unavailable: %s", option.get('error'))
            return
        
        backend_id = option.get('id', 'vte')
        current_backend = self.config.get_setting('terminal.backend', 'vte')
        
        # If backend hasn't actually changed, do nothing
        if backend_id.lower() == current_backend.lower():
            return
        
        # Check if there are any open terminal tabs
        open_terminals = self._get_open_terminals()
        
        if open_terminals:
            # Show info dialog explaining the change only applies to new terminals
            self._show_backend_change_info(backend_id, open_terminals, index)
        else:
            # No open terminals, proceed with backend switch
            self._apply_backend_change(index, backend_id)
    
    def _get_open_terminals(self):
        """Get all currently open terminal tabs (connected or not)"""
        terminals = []
        if not self.parent_window:
            return terminals
        
        # Check active_terminals
        active_terminals = getattr(self.parent_window, 'active_terminals', {})
        for connection, terminal in active_terminals.items():
            if terminal:
                terminals.append((connection, terminal))
        
        # Also check connection_to_terminals for any other terminals
        connection_to_terminals = getattr(self.parent_window, 'connection_to_terminals', {})
        for connection, terminal_list in connection_to_terminals.items():
            for terminal in terminal_list:
                if terminal:
                    # Avoid duplicates
                    if (connection, terminal) not in terminals:
                        terminals.append((connection, terminal))
        
        # Check tab_view for any terminal pages
        tab_view = getattr(self.parent_window, 'tab_view', None)
        if tab_view is not None and hasattr(tab_view, 'get_n_pages'):
            try:
                for page_idx in range(tab_view.get_n_pages()):
                    page = tab_view.get_nth_page(page_idx)
                    if page is None:
                        continue
                    terminal = page.get_child()
                    if terminal and terminal not in [t for _, t in terminals]:
                        # Try to find the connection for this terminal
                        terminal_to_connection = getattr(self.parent_window, 'terminal_to_connection', {})
                        connection = terminal_to_connection.get(terminal)
                        if connection:
                            terminals.append((connection, terminal))
                        else:
                            # Terminal without connection (e.g., local terminal)
                            terminals.append((None, terminal))
            except Exception:
                pass
        
        return terminals
    
    def _show_backend_change_info(self, backend_id, open_terminals, index):
        """Show an info dialog explaining that backend change only applies to new terminals"""
        backend_name = 'PyXterm.js' if backend_id.lower() == 'pyxterm' else 'VTE'
        num_terminals = len(open_terminals)
        
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=_("Terminal Backend Change"),
            secondary_text=_(
                f"The terminal backend has been changed to {backend_name}.\n\n"
                f"This change will only apply to new terminal tabs.\n"
                f"Existing {num_terminals} terminal tab{'s' if num_terminals > 1 else ''} will continue using their current backend.\n\n"
                "To use the new backend for existing terminals, close and reopen those tabs."
            )
        )
        def _on_info_response(d, response_id):
            d.destroy()
            self._apply_backend_change(index, backend_id)
        dialog.connect("response", _on_info_response)
        dialog.present()
    
    def _apply_backend_change(self, index, backend_id):
        """Apply the backend change (only affects new terminals, not existing ones)"""
        self._backend_last_valid_index = index
        self.config.set_setting('terminal.backend', backend_id)
        self._update_backend_row_subtitle(index)
        
        # Update encoding row visibility when backend changes
        self._update_encoding_row_visibility()
        
        # Note: We do NOT call refresh_backends() here
        # This ensures existing terminals keep their current backend
        # Only new terminals will use the new backend setting
        logger.info(f"Terminal backend changed to {backend_id} (will apply to new terminals only)")

    def on_color_scheme_changed(self, combo_row, param):
        """Handle terminal color scheme change"""
        selected = combo_row.get_selected()
        scheme_names = [
            "Default", "Black on White", "Solarized Dark", "Solarized Light",
            "Monokai", "Dracula", "Nord",
            "Gruvbox Dark", "One Dark", "Tomorrow Night", "Material Dark",
            "Rosé Pine", "Rosé Pine Moon", "Rosé Pine Dawn",
            "Catppuccin Latte", "Catppuccin Frappé", "Catppuccin Macchiato", "Catppuccin Mocha"
        ]
        selected_scheme = scheme_names[selected] if selected < len(scheme_names) else "Default"
        
        logger.info(f"Terminal color scheme changed to: {selected_scheme}")
        
        # Convert display name to config key
        theme_mapping = self.get_theme_name_mapping()
        config_key = theme_mapping.get(selected_scheme, "default")
        
        # Save to config using the consistent key
        self.config.set_setting('terminal.theme', config_key)
        
        # Apply to all active terminals
        self.apply_color_scheme_to_terminals(config_key)
        
        # Refresh the color preview
        if hasattr(self, 'color_preview_terminal'):
            self.color_preview_terminal.queue_draw()
    
    def draw_color_preview(self, drawing_area, cr, width, height):
        """Draw a preview of the selected color scheme"""
        # Get current color scheme
        current_scheme_key = self.config.get_setting('terminal.theme', 'default')
        
        # Get color scheme colors
        colors = self.get_color_scheme_colors(current_scheme_key)
        
        # Draw background
        bg_color = colors.get('background', '#000000')
        r, g, b, a = self.hex_to_rgba(bg_color)
        cr.set_source_rgba(r, g, b, a)
        cr.paint()
        
        # Draw terminal-like content
        cr.set_font_size(10)
        
        # Draw prompt line
        prompt_color = colors.get('foreground', '#ffffff')
        r, g, b, a = self.hex_to_rgba(prompt_color)
        cr.set_source_rgba(r, g, b, a)
        cr.move_to(10, 25)
        cr.show_text("user@host:~$ ")
        
        # Draw command
        command_color = colors.get('foreground', '#ffffff')
        r, g, b, a = self.hex_to_rgba(command_color)
        cr.set_source_rgba(r, g, b, a)
        cr.move_to(120, 25)
        cr.show_text("ls -la")
        
        # Draw output lines
        output_color = colors.get('foreground', '#ffffff')
        r, g, b, a = self.hex_to_rgba(output_color)
        cr.set_source_rgba(r, g, b, a)
        
        # Directory line
        cr.move_to(10, 45)
        cr.show_text("drwxr-xr-x  2 user user 4096 Jan 15 10:30 .")
        
        # File line with different color
        file_color = colors.get('blue', '#0088ff')
        r, g, b, a = self.hex_to_rgba(file_color)
        cr.set_source_rgba(r, g, b, a)
        cr.move_to(10, 65)
        cr.show_text("-rw-r--r--  1 user user  1234 Jan 15 10:25 file.txt")
        
        # Executable file
        exec_color = colors.get('green', '#00ff00')
        r, g, b, a = self.hex_to_rgba(exec_color)
        cr.set_source_rgba(r, g, b, a)
        cr.move_to(10, 85)
        cr.show_text("-rwxr-xr-x  1 user user 5678 Jan 15 10:20 script.sh")
        
        # Prompt line 2
        prompt_color = colors.get('foreground', '#ffffff')
        r, g, b, a = self.hex_to_rgba(prompt_color)
        cr.set_source_rgba(r, g, b, a)
        cr.move_to(10, 105)
        cr.show_text("user@host:~$ ")
        
    def get_color_scheme_colors(self, scheme_key):
        """Get colors for a specific color scheme"""
        schemes = {
            'default': {
                'background': '#000000',
                'foreground': '#ffffff',
                'blue': '#0088ff',
                'green': '#00ff00',
                'red': '#ff0000',
                'yellow': '#ffff00',
                'magenta': '#ff00ff',
                'cyan': '#00ffff'
            },
            'black_on_white': {
                'background': '#ffffff',
                'foreground': '#000000',
                'blue': '#0000ff',
                'green': '#00ff00',
                'red': '#ff0000',
                'yellow': '#ffff00',
                'magenta': '#ff00ff',
                'cyan': '#00ffff'
            },
            'solarized_dark': {
                'background': '#002b36',
                'foreground': '#839496',
                'blue': '#268bd2',
                'green': '#859900',
                'red': '#dc322f',
                'yellow': '#b58900',
                'magenta': '#d33682',
                'cyan': '#2aa198'
            },
            'solarized_light': {
                'background': '#fdf6e3',
                'foreground': '#657b83',
                'blue': '#268bd2',
                'green': '#859900',
                'red': '#dc322f',
                'yellow': '#b58900',
                'magenta': '#d33682',
                'cyan': '#2aa198'
            },
            'monokai': {
                'background': '#272822',
                'foreground': '#f8f8f2',
                'blue': '#66d9ef',
                'green': '#a6e22e',
                'red': '#f92672',
                'yellow': '#e6db74',
                'magenta': '#fd5ff0',
                'cyan': '#a1efe4'
            },
            'dracula': {
                'background': '#282a36',
                'foreground': '#f8f8f2',
                'blue': '#6272a4',
                'green': '#50fa7b',
                'red': '#ff5555',
                'yellow': '#f1fa8c',
                'magenta': '#bd93f9',
                'cyan': '#8be9fd'
            },
            'nord': {
                'background': '#2e3440',
                'foreground': '#eceff4',
                'blue': '#5e81ac',
                'green': '#a3be8c',
                'red': '#bf616a',
                'yellow': '#ebcb8b',
                'magenta': '#b48ead',
                'cyan': '#88c0d0'
            },
            'gruvbox_dark': {
                'background': '#282828',
                'foreground': '#ebdbb2',
                'blue': '#83a598',
                'green': '#b8bb26',
                'red': '#fb4934',
                'yellow': '#fabd2f',
                'magenta': '#d3869b',
                'cyan': '#8ec07c'
            },
            'one_dark': {
                'background': '#282c34',
                'foreground': '#abb2bf',
                'blue': '#61afef',
                'green': '#98c379',
                'red': '#e06c75',
                'yellow': '#e5c07b',
                'magenta': '#c678dd',
                'cyan': '#56b6c2'
            },
            'tomorrow_night': {
                'background': '#1d1f21',
                'foreground': '#c5c8c6',
                'blue': '#81a2be',
                'green': '#b5bd68',
                'red': '#cc6666',
                'yellow': '#f0c674',
                'magenta': '#b294bb',
                'cyan': '#8abeb7'
            },
            'material_dark': {
                'background': '#263238',
                'foreground': '#eeffff',
                'blue': '#82aaff',
                'green': '#c3e88d',
                'red': '#f07178',
                'yellow': '#ffcb6b',
                'magenta': '#c792ea',
                'cyan': '#89ddff'
            },
            'rose_pine': {
                'background': '#191724',
                'foreground': '#e0def4',
                'blue': '#9ccfd8',
                'green': '#31748f',
                'red': '#eb6f92',
                'yellow': '#f6c177',
                'magenta': '#c4a7e7',
                'cyan': '#ebbcba'
            },
            'rose_pine_moon': {
                'background': '#232136',
                'foreground': '#e0def4',
                'blue': '#9ccfd8',
                'green': '#3e8fb0',
                'red': '#eb6f92',
                'yellow': '#f6c177',
                'magenta': '#c4a7e7',
                'cyan': '#ea9a97'
            },
            'rose_pine_dawn': {
                'background': '#faf4ed',
                'foreground': '#464261',
                'blue': '#56949f',
                'green': '#286983',
                'red': '#b4637a',
                'yellow': '#ea9d34',
                'magenta': '#907aa9',
                'cyan': '#d7827e'
            },
            'catppuccin_latte': {
                'background': '#eff1f5',
                'foreground': '#4c4f69',
                'blue': '#1e66f5',
                'green': '#40a02b',
                'red': '#d20f39',
                'yellow': '#df8e1d',
                'magenta': '#8839ef',
                'cyan': '#179299'
            },
            'catppuccin_frappe': {
                'background': '#303446',
                'foreground': '#c6d0f5',
                'blue': '#8caaee',
                'green': '#a6d189',
                'red': '#e78284',
                'yellow': '#e5c890',
                'magenta': '#ca9ee6',
                'cyan': '#81c8be'
            },
            'catppuccin_macchiato': {
                'background': '#24273a',
                'foreground': '#cad3f5',
                'blue': '#8aadf4',
                'green': '#a6da95',
                'red': '#ed8796',
                'yellow': '#eed49f',
                'magenta': '#c6a0f6',
                'cyan': '#8bd5ca'
            },
            'catppuccin_mocha': {
                'background': '#1e1e2e',
                'foreground': '#cdd6f4',
                'blue': '#89b4fa',
                'green': '#a6e3a1',
                'red': '#f38ba8',
                'yellow': '#f9e2af',
                'magenta': '#cba6f7',
                'cyan': '#94e2d5'
            }
        }
        return schemes.get(scheme_key, schemes['default'])
    
    def hex_to_rgba(self, hex_color):
        """Convert hex color to RGBA values (0-1 range)"""
        hex_color = hex_color.lstrip('#')
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
        return (r, g, b, 1.0)

    def _is_internal_file_manager_enabled(self) -> bool:
        """Return ``True`` when the application uses the built-in file manager."""

        try:
            if not has_internal_file_manager():
                return False
        except Exception as exc:  # pragma: no cover - defensive capability detection
            logger.debug("Internal file manager check failed: %s", exc)
            return False

        try:
            from .sftp_utils import should_use_in_app_file_manager  # pylint: disable=import-outside-toplevel

            return bool(should_use_in_app_file_manager())
        except Exception as exc:  # pragma: no cover - defensive capability detection
            logger.debug("Failed to determine internal file manager usage: %s", exc)
            try:
                return bool(self.config.get_setting('file_manager.force_internal', False))
            except Exception:
                return False

    def _update_external_file_manager_row(self) -> None:
        """Sync the external window preference with the current availability."""

        row = getattr(self, 'open_file_manager_externally_row', None)
        if row is None:
            return

        use_internal = self._is_internal_file_manager_enabled()
        row.set_sensitive(use_internal)

        if not use_internal and row.get_active():
            row.set_active(False)

    def on_force_internal_file_manager_changed(self, switch, *args):
        """Persist the preference for forcing the in-app file manager."""
        try:
            active = bool(switch.get_active())
            self.config.set_setting('file_manager.force_internal', active)
            self._update_external_file_manager_row()
        except Exception as exc:
            logger.error("Failed to update file manager preference: %s", exc)

    def on_sidebar_flat_rows_changed(self, switch, *args):
        """Persist flat vs card styling for sidebar connection rows."""
        try:
            active = bool(switch.get_active())
            self.config.set_setting('ui.sidebar_flat_rows', active)
            if self.parent_window and hasattr(self.parent_window, 'update_sidebar_display'):
                self.parent_window.update_sidebar_display()
        except Exception as exc:
            logger.error("Failed to update sidebar flat rows preference: %s", exc)

    def on_sidebar_show_user_hostname_changed(self, switch, *args):
        """Persist the preference for showing user@hostname in sidebar."""
        try:
            active = bool(switch.get_active())
            self.config.set_setting('ui.sidebar_show_user_hostname', active)
            # Update sidebar if window is available
            if self.parent_window and hasattr(self.parent_window, 'update_sidebar_display'):
                self.parent_window.update_sidebar_display()
        except Exception as exc:
            logger.error("Failed to update sidebar show user hostname preference: %s", exc)

    def on_sidebar_show_group_count_changed(self, switch, *args):
        """Persist the preference for showing connection count in groups."""
        try:
            active = bool(switch.get_active())
            self.config.set_setting('ui.sidebar_show_group_count', active)
            # Update sidebar if window is available
            if self.parent_window and hasattr(self.parent_window, 'update_sidebar_display'):
                self.parent_window.update_sidebar_display()
        except Exception as exc:
            logger.error("Failed to update sidebar show group count preference: %s", exc)

    def on_sidebar_show_connection_status_changed(self, switch, *args):
        """Persist the preference for showing connection status in sidebar."""
        try:
            active = bool(switch.get_active())
            self.config.set_setting('ui.sidebar_show_connection_status', active)
            # Update sidebar if window is available
            if self.parent_window and hasattr(self.parent_window, 'update_sidebar_display'):
                self.parent_window.update_sidebar_display()
        except Exception as exc:
            logger.error("Failed to update sidebar show connection status preference: %s", exc)

    def on_sidebar_show_port_forwarding_changed(self, switch, *args):
        """Persist the preference for showing port forwarding labels in sidebar."""
        try:
            active = bool(switch.get_active())
            self.config.set_setting('ui.sidebar_show_port_forwarding', active)
            # Update sidebar if window is available
            if self.parent_window and hasattr(self.parent_window, 'update_sidebar_display'):
                self.parent_window.update_sidebar_display()
        except Exception as exc:
            logger.error("Failed to update sidebar show port forwarding preference: %s", exc)

    def on_show_tips_toggled(self, switch, *args):
        """Persist whether the terminal tips banner is shown."""
        try:
            self.config.set_setting('terminal.show_tips', bool(switch.get_active()))
        except Exception as exc:
            logger.error("Failed to update show terminal tips preference: %s", exc)

    def on_sidebar_show_connection_icon_changed(self, switch, *args):
        """Persist the preference for showing connection icon in sidebar."""
        try:
            active = bool(switch.get_active())
            self.config.set_setting('ui.sidebar_show_connection_icon', active)
            # Update sidebar if window is available
            if self.parent_window and hasattr(self.parent_window, 'update_sidebar_display'):
                self.parent_window.update_sidebar_display()
        except Exception as exc:
            logger.error("Failed to update sidebar show connection icon preference: %s", exc)

    def on_open_file_manager_externally_changed(self, switch, *args):
        """Persist whether the file manager should open in a separate window."""
        try:
            active = bool(switch.get_active())
            self.config.set_setting('file_manager.open_externally', active)
        except Exception as exc:
            logger.error("Failed to update external file manager preference: %s", exc)

    def on_confirm_disconnect_changed(self, switch, *args):
        """Handle confirm disconnect setting change"""
        confirm = switch.get_active()
        logger.info(f"Confirm before disconnect setting changed to: {confirm}")
        self.config.set_setting('confirm-disconnect', confirm)

    def on_builtin_passphrase_prompt_changed(self, switch, *args):
        """Handle built-in passphrase prompt setting change"""
        enabled = switch.get_active()
        logger.info(f"Use built-in passphrase prompt setting changed to: {enabled}")
        self.config.set_setting('use-builtin-passphrase-prompt', enabled)

    def on_agent_preload_changed(self, switch, *args):
        """Handle 'preload keys into ssh-agent' setting change"""
        enabled = switch.get_active()
        logger.info(f"Preload keys into ssh-agent setting changed to: {enabled}")
        self.config.set_setting('ssh.agent_preload_keys', enabled)

    def on_use_askpass_changed(self, switch, *args):
        """Handle 'use SSH askpass helper' setting change"""
        enabled = switch.get_active()
        logger.info(f"Use SSH askpass helper setting changed to: {enabled}")
        self.config.set_setting('use-askpass', enabled)
        # The built-in prompt and agent preload both rely on the askpass helper.
        if hasattr(self, 'builtin_passphrase_prompt_switch'):
            self.builtin_passphrase_prompt_switch.set_sensitive(enabled)
        if hasattr(self, 'agent_preload_switch'):
            self.agent_preload_switch.set_sensitive(enabled)

    def on_logging_level_changed(self, combo_row, _param):
        """Persist the chosen log level and apply it on the fly."""
        level = 'debug' if combo_row.get_selected() == 1 else 'info'
        try:
            self.config.set_setting('logging.level', level)
        except Exception as exc:
            logger.error("Failed to update logging.level: %s", exc)
            return
        # Apply immediately to the running process so the user sees the
        # change without restarting. CLI overrides still win if the app was
        # launched with --verbose / --quiet.
        try:
            import logging as _logging
            target = _logging.DEBUG if level == 'debug' else _logging.INFO
            _logging.getLogger().setLevel(target)
            for h in _logging.getLogger().handlers:
                h.setLevel(target)
            _logging.getLogger('sshpilot').setLevel(target)
        except Exception as exc:
            logger.debug("Could not apply log level on the fly: %s", exc)
    
    def on_check_updates_changed(self, switch, *args):
        """Handle check for updates on startup setting change"""
        check_on_startup = switch.get_active()
        logger.info(f"Check for updates on startup changed to: {check_on_startup}")
        self.config.set_setting('updates.check_on_startup', check_on_startup)
    
    def on_startup_behavior_changed(self, radio_button, *args):
        """Handle startup behavior radio button change"""
        if self.terminal_startup_radio.get_active():
            behavior = 'terminal'
        elif getattr(self, 'previous_session_startup_radio', None) and self.previous_session_startup_radio.get_active():
            behavior = 'previous-session'
        elif getattr(self, 'saved_session_startup_radio', None) and self.saved_session_startup_radio.get_active():
            behavior = 'saved-session'
        else:
            behavior = 'welcome'
        logger.info(f"App startup behavior changed to: {behavior}")
        self.config.set_setting('app-startup-behavior', behavior)

        # The saved-session selector is only relevant when that option is chosen.
        target = getattr(self, 'startup_session_box', None) or getattr(self, 'startup_session_row', None)
        if target is not None:
            target.set_sensitive(
                behavior == 'saved-session' and bool(getattr(self, '_startup_session_names', []))
            )

    def on_startup_session_changed(self, combo_row, *args):
        """Persist the chosen startup session name."""
        names = getattr(self, '_startup_session_names', [])
        if not names:
            return
        index = combo_row.get_selected()
        if 0 <= index < len(names):
            name = names[index]
            logger.info(f"Startup session changed to: {name}")
            self.config.set_setting('app-startup-session-name', name)
    
    def on_terminal_choice_changed(self, radio_button, *args):
        """Handle terminal choice radio button change"""
        use_external = self.external_terminal_radio.get_active()
        logger.info(f"Terminal choice changed to: {'external' if use_external else 'built-in'}")
        
        # Enable/disable external terminal options
        self.external_terminal_box.set_sensitive(use_external)
        
        # Save preference
        self.config.set_setting('use-external-terminal', use_external)
    
    def on_terminal_dropdown_changed(self, dropdown, *args):
        """Handle terminal dropdown selection change"""
        selected = dropdown.get_selected()
        if selected is None or selected < 0:
            return
        
        # Get the selected terminal from the model
        model = dropdown.get_model()
        if model and selected < model.get_n_items():
            terminal_name = model.get_string(selected)
            logger.info(f"Terminal dropdown selection changed to: {terminal_name}")

            # Show/hide custom path entry based on selection
            if hasattr(self, 'custom_terminal_box'):
                if terminal_name == "Custom":
                    self.custom_terminal_box.set_visible(True)
                else:
                    self.custom_terminal_box.set_visible(False)

            # Save the selected terminal
            if terminal_name != "Custom":
                command = self.terminal_commands.get(terminal_name, terminal_name)
                self.config.set_setting('external-terminal', command)
    
    def on_custom_terminal_path_changed(self, entry, *args):
        """Handle custom terminal path entry change"""
        custom_path = entry.get_text().strip()
        logger.info(f"Custom terminal path changed to: {custom_path}")
        
        # Save the path if not empty
        if custom_path:
            self.config.set_setting('custom-terminal-path', custom_path)
            self.config.set_setting('external-terminal', 'custom')
        else:
            # Clear empty path
            self.config.set_setting('custom-terminal-path', '')
    
    def _populate_terminal_dropdown(self):
        """Populate the terminal dropdown with available terminals"""
        try:
            # Create string list for dropdown
            terminals_list = Gtk.StringList()

            # Mapping of terminal labels to their launch commands
            common_terminals = [
                ("gnome-terminal", "gnome-terminal"),
                ("ptyxis", "ptyxis"),
                ("konsole", "konsole"),
                ("xfce4-terminal", "xfce4-terminal"),
                ("alacritty", "alacritty"),
                ("kitty", "kitty"),
                ("terminator", "terminator"),
                ("tilix", "tilix"),
                ("xterm", "xterm"),
                ("guake", "guake"),
                ("ghostty", "ghostty"),
                ("foot", "foot"),
                ("blackbox", "blackbox"),
            ]

            # Append macOS terminals when running on macOS
            if is_macos():
                common_terminals.extend(
                    [
                        ("Terminal", "open -a Terminal"),
                        ("iTerm2", "open -a iTerm"),
                        ("Alacritty", "open -a Alacritty"),
                        ("Ghostty", "open -a Ghostty"),
                        ("Warp", "open -a Warp"),
                    ]
                )

            # Prepare mapping for later lookup
            self.terminal_commands = {}

            def _macos_app_exists(app_name: str) -> bool:
                """Check if a macOS .app bundle exists"""
                app_dirs = [
                    "/Applications",
                    "/Applications/Utilities",
                    "/System/Applications",
                    "/System/Applications/Utilities",
                ]
                for dir_path in app_dirs:
                    if os.path.exists(os.path.join(dir_path, f"{app_name}.app")):
                        return True
                return False

            # Check which terminals are available
            available_terminals = []
            for label, command in common_terminals:
                try:
                    if command.startswith("open -a "):
                        app_name = command.split("open -a ", 1)[1]
                        if _macos_app_exists(app_name):
                            available_terminals.append((label, command))
                    else:
                        if shutil.which(command):
                            available_terminals.append((label, command))
                except Exception:
                    continue

            # Add available terminals to dropdown and mapping
            for label, command in available_terminals:
                terminals_list.append(label)
                self.terminal_commands[label] = command

            # Add "Custom" option
            terminals_list.append("Custom")

            # Set the model
            self.terminal_dropdown.set_model(terminals_list)

            logger.info(
                f"Populated terminal dropdown with {len(available_terminals)} available terminals"
            )
            
        except Exception as e:
            logger.error(f"Failed to populate terminal dropdown: {e}")
    
    def _set_terminal_dropdown_selection(self, terminal_name):
        """Set the dropdown selection to the specified terminal"""
        try:
            model = self.terminal_dropdown.get_model()
            if not model:
                return

            # Handle the case where terminal_name is 'custom' but dropdown has 'Custom'
            if terminal_name == 'custom':
                terminal_label = 'Custom'
            else:
                # Try to find corresponding label for stored command
                terminal_label = None
                for label, command in self.terminal_commands.items():
                    if terminal_name == command or terminal_name == label:
                        terminal_label = label
                        break
                if terminal_label is None:
                    terminal_label = terminal_name

            # Find the terminal in the model
            for i in range(model.get_n_items()):
                if model.get_string(i) == terminal_label:
                    self.terminal_dropdown.set_selected(i)

                    # Show/hide custom path entry based on selection
                    if hasattr(self, 'custom_terminal_box'):
                        if terminal_label == "Custom":
                            self.custom_terminal_box.set_visible(True)
                        else:
                            self.custom_terminal_box.set_visible(False)
                    return
            
            # If not found, default to first available terminal
            if model.get_n_items() > 0:
                self.terminal_dropdown.set_selected(0)
                
        except Exception as e:
            logger.error(f"Failed to set terminal dropdown selection: {e}")
    

    
    def apply_color_scheme_to_terminals(self, scheme_key):
        """Apply color scheme to all active terminal widgets"""
        try:
            parent_window = self.get_transient_for()
            if parent_window and hasattr(parent_window, 'connection_to_terminals'):
                count = 0
                for terms in parent_window.connection_to_terminals.values():
                    for terminal in terms:
                        if hasattr(terminal, 'apply_theme'):
                            terminal.apply_theme(scheme_key)
                            count += 1
                logger.info(f"Applied color scheme {scheme_key} to {count} terminals")
        except Exception as e:
            logger.error(f"Failed to apply color scheme to terminals: {e}")
