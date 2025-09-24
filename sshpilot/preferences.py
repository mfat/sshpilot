"""Preferences dialog and font selection utilities."""

import os
import logging
import subprocess
import shutil

from .platform_utils import get_config_dir, is_flatpak, is_macos
from .file_manager_integration import (
    has_internal_file_manager,
    has_native_gvfs_support,
)
from .shortcut_editor import ShortcutsPreferencesPage


import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, Adw, Pango, PangoFT2

logger = logging.getLogger(__name__)

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

class MonospaceFontDialog(Adw.Window):
    def __init__(self, parent=None, current_font="Monospace 12"):
        super().__init__()
        
        self.set_title("Select Terminal Font")
        self.set_default_size(500, 600)
        self.set_transient_for(parent)
        self.set_modal(True)
        
        # Store callback
        self.callback = None
        
        # Parse current font
        self.current_font_desc = Pango.FontDescription.from_string(current_font)
        
        # Create main content
        self.setup_ui()
        self.populate_fonts()
        
    def setup_ui(self):
        # Main box
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)
        
        # Header
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        
        # Cancel button
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self.on_cancel)
        header.pack_start(cancel_btn)
        
        # Select button
        select_btn = Gtk.Button(label="Select")
        select_btn.add_css_class("suggested-action")
        select_btn.connect("clicked", self.on_select)
        header.pack_end(select_btn)
        
        # Create search entry
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search fonts...")
        self.search_entry.connect("search-changed", self.on_search_changed)
        
        # Create font list
        self.font_model = Gtk.ListStore(str, str, object)  # display_name, family, font_desc
        self.font_filter = Gtk.TreeModelFilter(child_model=self.font_model)
        self.font_filter.set_visible_func(self.filter_fonts)
        
        self.font_view = Gtk.TreeView(model=self.font_filter)
        self.font_view.set_headers_visible(False)
        
        # Font name column
        name_renderer = Gtk.CellRendererText()
        name_column = Gtk.TreeViewColumn("Font", name_renderer, text=0)
        self.font_view.append_column(name_column)
        
        # Selection handling
        selection = self.font_view.get_selection()
        selection.connect("changed", self.on_selection_changed)
        
        # Scrolled window for font list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.font_view)
        scrolled.set_vexpand(True)
        
        # Size selection
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        size_label = Gtk.Label(label="Size:")
        size_label.set_halign(Gtk.Align.START)
        
        self.size_spin = Gtk.SpinButton.new_with_range(6, 72, 1)
        self.size_spin.set_value(self.current_font_desc.get_size() / Pango.SCALE)
        self.size_spin.connect("value-changed", self.on_size_changed)
        
        size_box.append(size_label)
        size_box.append(self.size_spin)
        
        # Preview text
        preview_frame = Gtk.Frame()
        preview_frame.set_label("Preview")
        
        self.preview_label = Gtk.Label()
        self.preview_label.set_text("The quick brown fox jumps over the lazy dog\n0123456789 !@#$%^&*()_+-=[]{}|;:,.<>?")
        self.preview_label.set_margin_top(12)
        self.preview_label.set_margin_bottom(12)
        self.preview_label.set_margin_start(12)
        self.preview_label.set_margin_end(12)
        self.preview_label.set_selectable(True)
        preview_frame.set_child(self.preview_label)
        
        # Add everything to main box
        main_box.append(header)
        main_box.append(self.search_entry)
        main_box.append(scrolled)
        main_box.append(size_box)
        main_box.append(preview_frame)
        
        self.set_content(main_box)
        
    def populate_fonts(self):
        # Get font map
        fontmap = PangoFT2.FontMap.new()
        families = fontmap.list_families()
        
        monospace_families = []
        for family in families:
            if family.is_monospace():
                family_name = family.get_name()
                
                # Get available faces for this family
                faces = family.list_faces()
                for face in faces:
                    face_name = face.get_face_name()
                    
                    # Create font description
                    desc = Pango.FontDescription()
                    desc.set_family(family_name)
                    desc.set_size(12 * Pango.SCALE)
                    
                    # Set style if not regular
                    if face_name.lower() != "regular":
                        if "bold" in face_name.lower():
                            desc.set_weight(Pango.Weight.BOLD)
                        if "italic" in face_name.lower() or "oblique" in face_name.lower():
                            desc.set_style(Pango.Style.ITALIC)
                    
                    # Display name
                    if face_name.lower() == "regular":
                        display_name = family_name
                    else:
                        display_name = f"{family_name} {face_name}"
                    
                    monospace_families.append((display_name, family_name, desc))
        
        # Sort by family name
        monospace_families.sort(key=lambda x: x[0].lower())
        
        # Add to model
        for display_name, family_name, desc in monospace_families:
            self.font_model.append([display_name, family_name, desc])
            
        # Select current font if possible
        self.select_current_font()
        
    def select_current_font(self):
        current_family = self.current_font_desc.get_family()
        if not current_family:
            return
            
        iter = self.font_model.get_iter_first()
        while iter:
            family = self.font_model.get_value(iter, 1)
            if family.lower() == current_family.lower():
                # Convert to filter iter
                filter_iter = self.font_filter.convert_child_iter_to_iter(iter)
                if filter_iter[1]:  # Check if conversion was successful
                    selection = self.font_view.get_selection()
                    selection.select_iter(filter_iter[1])
                    
                    # Scroll to selection
                    path = self.font_filter.get_path(filter_iter[1])
                    self.font_view.scroll_to_cell(path, None, False, 0.0, 0.0)
                break
            iter = self.font_model.iter_next(iter)
    
    def filter_fonts(self, model, iter, data):
        search_text = self.search_entry.get_text().lower()
        if not search_text:
            return True
            
        font_name = model.get_value(iter, 0).lower()
        return search_text in font_name
    
    def on_search_changed(self, entry):
        self.font_filter.refilter()
    
    def on_selection_changed(self, selection):
        model, iter = selection.get_selected()
        if iter:
            font_desc = model.get_value(iter, 2).copy()
            font_desc.set_size(int(self.size_spin.get_value()) * Pango.SCALE)
            self.update_preview(font_desc)
    
    def on_size_changed(self, spin):
        selection = self.font_view.get_selection()
        model, iter = selection.get_selected()
        if iter:
            font_desc = model.get_value(iter, 2).copy()
            font_desc.set_size(int(spin.get_value()) * Pango.SCALE)
            self.update_preview(font_desc)
    
    def update_preview(self, font_desc):
        # Remove previous CSS provider if it exists
        if hasattr(self, '_css_provider'):
            context = self.preview_label.get_style_context()
            context.remove_provider(self._css_provider)
        
        # Create CSS for the font
        css = f"""
        .preview-font {{
            font-family: "{font_desc.get_family()}";
            font-size: {font_desc.get_size() / Pango.SCALE}pt;
            font-weight: normal;
            font-style: normal;
        """
        
        if font_desc.get_weight() == Pango.Weight.BOLD:
            css += "font-weight: bold;"
        if font_desc.get_style() == Pango.Style.ITALIC:
            css += "font-style: italic;"
            
        css += "}"
        
        # Apply CSS
        self._css_provider = Gtk.CssProvider()
        self._css_provider.load_from_data(css.encode())
        
        context = self.preview_label.get_style_context()
        context.add_provider(self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        
        # Ensure the class is added (but only once)
        if not context.has_class("preview-font"):
            context.add_class("preview-font")
    
    def on_cancel(self, button):
        self.close()
    
    def on_select(self, button):
        selection = self.font_view.get_selection()
        model, iter = selection.get_selected()
        if iter and self.callback:
            font_desc = model.get_value(iter, 2).copy()
            font_desc.set_size(int(self.size_spin.get_value()) * Pango.SCALE)
            font_string = font_desc.to_string()
            self.callback(font_string)
        self.close()
    
    def set_callback(self, callback):
        """Set callback function that receives the selected font string"""
        self.callback = callback


class PreferencesWindow(Adw.PreferencesWindow):
    """Preferences dialog window"""
    
    def __init__(self, parent_window, config):
        super().__init__()
        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.parent_window = parent_window
        self.config = config
        
        # Set window properties
        self.set_title("Preferences")
        self.set_default_size(600, 500)
        
        # Initialize the preferences UI
        self.setup_preferences()
        
        # Apply any existing color overrides
        self.apply_color_overrides()

        # Save on close to persist advanced SSH settings
        self.connect('close-request', self.on_close_request)
    
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
            current_font = self.config.get_setting('terminal.font', 'Monospace 12')
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
                "Gruvbox Dark", "One Dark", "Tomorrow Night", "Material Dark"
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
            
            # Color scheme preview
            preview_group = Adw.PreferencesGroup()
            preview_group.set_title("Preview")
            
            # Create preview terminal widget
            self.color_preview_terminal = Gtk.DrawingArea()
            self.color_preview_terminal.set_draw_func(self.draw_color_preview)
            self.color_preview_terminal.set_size_request(400, 120)
            self.color_preview_terminal.add_css_class("terminal-preview")
            
            # Add some margin around the preview
            preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            preview_box.set_margin_top(6)
            preview_box.set_margin_bottom(6)
            preview_box.set_margin_start(12)
            preview_box.set_margin_end(12)
            preview_box.append(self.color_preview_terminal)
            
            preview_group.add(preview_box)
            appearance_group.add(preview_group)
            terminal_page.add(appearance_group)
            
            # Preferred Terminal group (shown when external terminals are available)
            if not should_hide_external_terminal_options():
                terminal_choice_group = Adw.PreferencesGroup()
                terminal_choice_group.set_title("Preferred Terminal")
                
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
            
            # Create Interface preferences page
            interface_page = Adw.PreferencesPage()
            interface_page.set_title("Interface")
            interface_page.set_icon_name("applications-graphics-symbolic")
            
            # App startup behavior
            startup_group = Adw.PreferencesGroup()
            startup_group.set_title("App Startup")
            
            # Radio buttons for startup behavior
            self.terminal_startup_radio = Gtk.CheckButton(label="Show Terminal")
            self.terminal_startup_radio.set_can_focus(True)
            self.welcome_startup_radio = Gtk.CheckButton(label="Show Start Page")
            self.welcome_startup_radio.set_can_focus(True)
            
            # Make them behave like radio buttons
            self.welcome_startup_radio.set_group(self.terminal_startup_radio)
            
            # Set current preference (default to terminal)
            startup_behavior = self.config.get_setting('app-startup-behavior', 'terminal')
            if startup_behavior == 'welcome':
                self.welcome_startup_radio.set_active(True)
            else:
                self.terminal_startup_radio.set_active(True)
            
            # Connect radio button changes
            self.terminal_startup_radio.connect('toggled', self.on_startup_behavior_changed)
            self.welcome_startup_radio.connect('toggled', self.on_startup_behavior_changed)
            
            # Add radio buttons to group
            startup_group.add(self.terminal_startup_radio)
            startup_group.add(self.welcome_startup_radio)
            
            interface_page.add(startup_group)
            
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
            
            # Load saved theme preference
            saved_theme = self.config.get_setting('app-theme', 'default')
            theme_mapping = {'default': 0, 'light': 1, 'dark': 2}
            self.theme_row.set_selected(theme_mapping.get(saved_theme, 0))
            
            self.theme_row.connect('notify::selected', self.on_theme_changed)
            
            interface_appearance_group.add(self.theme_row)
            
            # Color overrides section
            color_override_group = Adw.PreferencesGroup()
            color_override_group.set_title("Color Overrides")
            color_override_group.set_description("Override default app colors")
            
            # App color override
            self.app_color_row = Adw.ActionRow()
            self.app_color_row.set_title("App Color")
            self.app_color_row.set_subtitle("Override the primary app color")

            self.app_color_button = Gtk.ColorButton()
            self.app_color_button.set_use_alpha(False)
            self.app_color_button.set_tooltip_text("Choose app color")
            self.app_color_button.connect('color-set', self.on_app_color_changed)
            self.app_color_row.add_suffix(self.app_color_button)
            color_override_group.add(self.app_color_row)
            
            # Accent color override
            self.accent_color_row = Adw.ActionRow()
            self.accent_color_row.set_title("Accent Color")
            self.accent_color_row.set_subtitle("Override the accent color for highlights")

            self.accent_color_button = Gtk.ColorButton()
            self.accent_color_button.set_use_alpha(False)
            self.accent_color_button.set_tooltip_text("Choose accent color")
            self.accent_color_button.connect('color-set', self.on_accent_color_changed)
            self.accent_color_row.add_suffix(self.accent_color_button)
            color_override_group.add(self.accent_color_row)
            
            # Sidebar color override
            self.sidebar_color_row = Adw.ActionRow()
            self.sidebar_color_row.set_title("Sidebar Color")
            self.sidebar_color_row.set_subtitle("Override the sidebar background color")

            self.sidebar_color_button = Gtk.ColorButton()
            self.sidebar_color_button.set_use_alpha(False)
            self.sidebar_color_button.set_tooltip_text("Choose sidebar color")
            self.sidebar_color_button.connect('color-set', self.on_sidebar_color_changed)
            self.sidebar_color_row.add_suffix(self.sidebar_color_button)
            color_override_group.add(self.sidebar_color_row)
            
            # Reset colors button
            reset_colors_row = Adw.ActionRow()
            reset_colors_row.set_title("Reset to Default")
            reset_colors_row.set_subtitle("Remove color overrides and use system colors")
            
            reset_button = Gtk.Button()
            reset_button.set_label("Reset")
            reset_button.add_css_class("destructive-action")
            reset_button.connect('clicked', self.on_reset_colors_clicked)
            reset_colors_row.add_suffix(reset_button)
            color_override_group.add(reset_colors_row)

            interface_appearance_group.add(color_override_group)
            interface_page.add(interface_appearance_group)

            # Initialize color button states
            self.refresh_color_buttons()
            
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

            # Shortcuts page with inline editor
            shortcuts_page = Adw.PreferencesPage()
            shortcuts_page.set_title("Shortcuts")
            shortcuts_page.set_icon_name("preferences-desktop-keyboard-shortcuts-symbolic")

            shortcuts_intro_group = Adw.PreferencesGroup()
            shortcuts_intro_group.set_title("Keyboard Shortcuts")

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

            shortcuts_intro_group.add(shortcuts_button_row)
            shortcuts_page.add(shortcuts_intro_group)

            try:
                self.shortcuts_editor_page = ShortcutsPreferencesPage(
                    parent_widget=self.parent_window,
                    app=self.parent_window.get_application() if self.parent_window else None,
                    config=self.config,
                    owner_window=self.parent_window,
                )

                groups_added = 0
                for group in self.shortcuts_editor_page.iter_groups():
                    shortcuts_page.add(group)
                    groups_added += 1
                    logger.debug(f"Added shortcut group: {group.get_title()}")
                
                logger.info(f"Shortcut editor successfully added to preferences with {groups_added} groups")
            except Exception as e:
                logger.error(f"Failed to create shortcut editor: {e}", exc_info=True)
                # Add a fallback message to the shortcuts page
                fallback_group = Adw.PreferencesGroup()
                fallback_group.set_title("Shortcut Editor")
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
            operation_group = Adw.PreferencesGroup()
            operation_group.set_title("Operation Mode")


            # Default mode row
            self.default_mode_row = Adw.ActionRow()
            self.default_mode_row.set_title("Default Mode")
            self.default_mode_row.set_subtitle("sshPilot loads and modifies ~/.ssh/config")
            self.default_mode_radio = Gtk.CheckButton()


            # Isolated mode row
            self.isolated_mode_row = Adw.ActionRow()
            self.isolated_mode_row.set_title("Isolated Mode")
            config_path = get_config_dir()
            self.isolated_mode_row.set_subtitle(
                f"sshPilot stores its configuration file in {config_path}/"
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

            flatpak = is_flatpak()
            use_isolated = True if flatpak else bool(self.config.get_setting('ssh.use_isolated_config', False))
            self.isolated_mode_radio.set_active(use_isolated)
            self.default_mode_radio.set_active(not use_isolated)
            if flatpak:
                self.default_mode_row.set_sensitive(False)

            self.default_mode_radio.connect('toggled', self.on_operation_mode_toggled)
            self.isolated_mode_radio.connect('toggled', self.on_operation_mode_toggled)

            self._update_operation_mode_styles()

            advanced_page.add(operation_group)

            self.force_internal_file_manager_row = None
            if has_internal_file_manager():
                file_manager_group = Adw.PreferencesGroup()
                file_manager_group.set_title("File Management")

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
                advanced_page.add(file_manager_group)

            advanced_group = Adw.PreferencesGroup()
            advanced_group.set_title("SSH Settings")


            # Use custom options toggle
            self.apply_advanced_row = Adw.SwitchRow()
            self.apply_advanced_row.set_title("Use custom connection options")
            self.apply_advanced_row.set_subtitle("Enable and edit the options below")
            self.apply_advanced_row.set_active(bool(self.config.get_setting('ssh.apply_advanced', False)))
            advanced_group.add(self.apply_advanced_row)


            # Connect timeout
            self.connect_timeout_row = Adw.SpinRow.new_with_range(1, 120, 1)
            self.connect_timeout_row.set_title("Connect Timeout (s)")
            self.connect_timeout_row.set_value(self.config.get_setting('ssh.connection_timeout', 10))
            advanced_group.add(self.connect_timeout_row)

            # Connection attempts
            self.connection_attempts_row = Adw.SpinRow.new_with_range(1, 10, 1)
            self.connection_attempts_row.set_title("Connection Attempts")
            self.connection_attempts_row.set_value(self.config.get_setting('ssh.connection_attempts', 1))
            advanced_group.add(self.connection_attempts_row)

            # Keepalive interval
            self.keepalive_interval_row = Adw.SpinRow.new_with_range(0, 300, 5)
            self.keepalive_interval_row.set_title("ServerAlive Interval (s)")
            self.keepalive_interval_row.set_value(self.config.get_setting('ssh.keepalive_interval', 30))
            advanced_group.add(self.keepalive_interval_row)

            # Keepalive count max
            self.keepalive_count_row = Adw.SpinRow.new_with_range(1, 10, 1)
            self.keepalive_count_row.set_title("ServerAlive CountMax")
            self.keepalive_count_row.set_value(self.config.get_setting('ssh.keepalive_count_max', 3))
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
            self.batch_mode_row.set_active(bool(self.config.get_setting('ssh.batch_mode', True)))
            advanced_group.add(self.batch_mode_row)

            # Compression
            self.compression_row = Adw.SwitchRow()
            self.compression_row.set_title("Enable Compression (-C)")
            self.compression_row.set_active(bool(self.config.get_setting('ssh.compression', True)))
            advanced_group.add(self.compression_row)

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

            # Confirm before disconnecting
            self.confirm_disconnect_switch = Adw.SwitchRow()
            self.confirm_disconnect_switch.set_title("Confirm before disconnecting")
            self.confirm_disconnect_switch.set_subtitle("Show a confirmation dialog when disconnecting from a host")
            self.confirm_disconnect_switch.set_active(
                self.config.get_setting('confirm-disconnect', True)
            )
            self.confirm_disconnect_switch.connect('notify::active', self.on_confirm_disconnect_changed)
            advanced_group.add(self.confirm_disconnect_switch)

            # Reset button
            # Add spacing before reset button
            advanced_group.add(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            
            # Use Adw.ActionRow for proper spacing and layout
            reset_row = Adw.ActionRow()
            reset_row.set_title("Reset Advanced SSH Settings")
            reset_row.set_subtitle("Restore all advanced SSH settings to their default values")
            
            reset_btn = Gtk.Button.new_with_label("Reset")
            reset_btn.add_css_class('destructive-action')
            reset_btn.connect('clicked', self.on_reset_advanced_ssh)
            reset_row.add_suffix(reset_btn)
            
            advanced_group.add(reset_row)

            # Disable/enable advanced controls based on toggle
            def _sync_advanced_sensitivity(row=None, *_):
                enabled = bool(self.apply_advanced_row.get_active())
                for w in [self.connect_timeout_row, self.connection_attempts_row,
                          self.keepalive_interval_row, self.keepalive_count_row,
                          self.strict_host_row, self.batch_mode_row,
                          self.compression_row, self.verbosity_row,
                          self.debug_enabled_row]:
                    try:
                        w.set_sensitive(enabled)
                    except Exception:
                        pass
            _sync_advanced_sensitivity()
            self.apply_advanced_row.connect('notify::active', _sync_advanced_sensitivity)

            advanced_page.add(advanced_group)

            # Add pages to the preferences window
            self.add(interface_page)
            self.add(terminal_page)
            self.add(shortcuts_page)
            self.add(advanced_page)
            
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
                        if hasattr(terminal, 'vte'):
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

    def on_app_color_changed(self, color_button):
        """Handle app color change"""
        color = color_button.get_rgba()
        color_str = color.to_string()
        self.config.set_setting('app-color-override', color_str)
        logger.info(f"App color changed to: {color_str}")
        self.refresh_color_buttons()
        self.apply_color_overrides()

    def on_accent_color_changed(self, color_button):
        """Handle accent color change"""
        color = color_button.get_rgba()
        color_str = color.to_string()
        self.config.set_setting('accent-color-override', color_str)
        logger.info(f"Accent color changed to: {color_str}")
        self.refresh_color_buttons()
        self.apply_color_overrides()

    def on_sidebar_color_changed(self, color_button):
        """Handle sidebar color change"""
        color = color_button.get_rgba()
        color_str = color.to_string()
        self.config.set_setting('sidebar-color-override', color_str)
        logger.info(f"Sidebar color changed to: {color_str}")
        self.refresh_color_buttons()
        self.apply_color_overrides()

    def on_reset_colors_clicked(self, button):
        """Reset color overrides to default"""
        self.config.set_setting('app-color-override', None)
        self.config.set_setting('accent-color-override', None)
        self.config.set_setting('sidebar-color-override', None)

        self.refresh_color_buttons()
        logger.info("Color overrides reset to default")
        self.apply_color_overrides()

    def refresh_color_buttons(self):
        """Update color button appearance to reflect settings"""
        self._set_color_button(
            self.app_color_button,
            self.app_color_row,
            'app-color-override',
            Gdk.RGBA(0.2, 0.5, 0.9, 1.0),
            'Using system default',
        )
        self._set_color_button(
            self.accent_color_button,
            self.accent_color_row,
            'accent-color-override',
            Gdk.RGBA(0.2, 0.5, 0.9, 1.0),
            'Using system accent color',
        )
        self._set_color_button(
            self.sidebar_color_button,
            self.sidebar_color_row,
            'sidebar-color-override',
            Gdk.RGBA(0.9, 0.9, 0.9, 1.0),
            'Using system default',
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
            # Get color overrides from config
            app_color = self.config.get_setting('app-color-override', None)
            accent_color = self.config.get_setting('accent-color-override', None)
            sidebar_color = self.config.get_setting('sidebar-color-override', None)
            
            # Build CSS with color overrides using proper Adwaita named colors
            css_rules = []
            
            if app_color:
                # Override all accent-related colors for comprehensive theming
                css_rules.append(f"@define-color accent_bg_color {app_color};")
                css_rules.append(f"@define-color accent_fg_color white;")
                css_rules.append(f"@define-color accent_color {app_color};")
                # Override selected colors (used for selected rows, list items, etc.)
                css_rules.append(f"@define-color theme_selected_bg_color {app_color};")
                css_rules.append(f"@define-color theme_selected_fg_color white;")
                css_rules.append(f"@define-color theme_unfocused_selected_bg_color {app_color};")
                css_rules.append(f"@define-color theme_unfocused_selected_fg_color white;")
                # Override window background colors
                css_rules.append(f"@define-color window_bg_color {app_color};")
                css_rules.append(f"@define-color theme_bg_color {app_color};")
                css_rules.append(f"@define-color theme_unfocused_bg_color {app_color};")
                # Override sidebar colors
                css_rules.append(f"@define-color sidebar_bg_color {app_color};")
                css_rules.append(f"@define-color secondary_sidebar_bg_color {app_color};")
            
            if accent_color:
                # Override accent colors regardless of app color
                css_rules.append(f"@define-color accent_color {accent_color};")
                css_rules.append(f"@define-color accent_bg_color {accent_color};")
                css_rules.append(f"@define-color accent_fg_color white;")
                css_rules.append(f"@define-color theme_selected_bg_color {accent_color};")
                css_rules.append(f"@define-color theme_selected_fg_color white;")
                css_rules.append(
                    f"@define-color theme_unfocused_selected_bg_color {accent_color};"
                )
                css_rules.append(
                    f"@define-color theme_unfocused_selected_fg_color white;"
                )
            
            if sidebar_color:
                # Override sidebar colors independently
                css_rules.append(f"@define-color sidebar_bg_color {sidebar_color};")
                css_rules.append(f"@define-color secondary_sidebar_bg_color {sidebar_color};")
            
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
                    logger.info("Applied color overrides")
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
            if hasattr(self, 'apply_advanced_row'):
                self.config.set_setting('ssh.apply_advanced', bool(self.apply_advanced_row.get_active()))
            if hasattr(self, 'connect_timeout_row'):
                self.config.set_setting('ssh.connection_timeout', int(self.connect_timeout_row.get_value()))
            if hasattr(self, 'connection_attempts_row'):
                self.config.set_setting('ssh.connection_attempts', int(self.connection_attempts_row.get_value()))
            if hasattr(self, 'keepalive_interval_row'):
                self.config.set_setting('ssh.keepalive_interval', int(self.keepalive_interval_row.get_value()))
            if hasattr(self, 'keepalive_count_row'):
                self.config.set_setting('ssh.keepalive_count_max', int(self.keepalive_count_row.get_value()))
            if hasattr(self, 'strict_host_row'):
                options = ["accept-new", "yes", "no", "ask"]
                idx = self.strict_host_row.get_selected()
                value = options[idx] if 0 <= idx < len(options) else 'accept-new'
                self.config.set_setting('ssh.strict_host_key_checking', value)
            if hasattr(self, 'batch_mode_row'):
                self.config.set_setting('ssh.batch_mode', bool(self.batch_mode_row.get_active()))
            if hasattr(self, 'compression_row'):
                self.config.set_setting('ssh.compression', bool(self.compression_row.get_active()))
            if hasattr(self, 'verbosity_row'):
                self.config.set_setting('ssh.verbosity', int(self.verbosity_row.get_value()))
            if hasattr(self, 'debug_enabled_row'):
                self.config.set_setting('ssh.debug_enabled', bool(self.debug_enabled_row.get_active()))
            if getattr(self, 'force_internal_file_manager_row', None) is not None:
                self.config.set_setting(
                    'file_manager.force_internal',
                    bool(self.force_internal_file_manager_row.get_active()),
                )
        except Exception as e:
            logger.error(f"Failed to save advanced SSH settings: {e}")

    def on_reset_advanced_ssh(self, *args):
        """Reset only advanced SSH keys to defaults and update UI."""
        try:
            defaults = self.config.get_default_config().get('ssh', {})
            # Persist defaults and disable apply
            self.config.set_setting('ssh.apply_advanced', False)
            for key in ['connection_timeout', 'connection_attempts', 'keepalive_interval', 'keepalive_count_max', 'compression', 'auto_add_host_keys', 'verbosity', 'debug_enabled']:
                self.config.set_setting(f'ssh.{key}', defaults.get(key))
            # Update UI
            if hasattr(self, 'apply_advanced_row'):
                self.apply_advanced_row.set_active(False)
            if hasattr(self, 'connect_timeout_row'):
                self.connect_timeout_row.set_value(int(defaults.get('connection_timeout', 30)))
            if hasattr(self, 'connection_attempts_row'):
                self.connection_attempts_row.set_value(int(defaults.get('connection_attempts', 1)))
            if hasattr(self, 'keepalive_interval_row'):
                self.keepalive_interval_row.set_value(int(defaults.get('keepalive_interval', 60)))
            if hasattr(self, 'keepalive_count_row'):
                self.keepalive_count_row.set_value(int(defaults.get('keepalive_count_max', 3)))
            if hasattr(self, 'strict_host_row'):
                try:
                    self.strict_host_row.set_selected(["accept-new", "yes", "no", "ask"].index('accept-new'))
                except ValueError:
                    self.strict_host_row.set_selected(0)
            if hasattr(self, 'batch_mode_row'):
                self.batch_mode_row.set_active(False)
            if hasattr(self, 'compression_row'):
                self.compression_row.set_active(bool(defaults.get('compression', True)))
            if hasattr(self, 'verbosity_row'):
                self.verbosity_row.set_value(int(defaults.get('verbosity', 0)))
            if hasattr(self, 'debug_enabled_row'):
                self.debug_enabled_row.set_active(bool(defaults.get('debug_enabled', False)))
            file_manager_defaults = self.config.get_default_config().get('file_manager', {})
            default_force_internal = bool(file_manager_defaults.get('force_internal', False))
            self.config.set_setting('file_manager.force_internal', default_force_internal)
            if getattr(self, 'force_internal_file_manager_row', None) is not None:
                self.force_internal_file_manager_row.set_active(default_force_internal)
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

            # Inform user that restart is required for changes
            if parent_window:
                dialog = Adw.MessageDialog.new(
                    parent_window,
                    "Operation Mode Changed",
                    "Restart sshPilot to apply the new operation mode"
                )
                dialog.add_response("ok", "OK")
                dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
                dialog.set_modal(True)
                dialog.set_transient_for(parent_window)
                dialog.present()

        except Exception as e:
            logger.error(f"Failed to toggle isolated SSH mode: {e}")

    def _update_operation_mode_styles(self):
        """Visually de-emphasize the inactive operation mode"""
        if self.isolated_mode_radio.get_active():
            self.default_mode_row.add_css_class('dim-label')
            self.isolated_mode_row.remove_css_class('dim-label')
        else:
            self.isolated_mode_row.add_css_class('dim-label')
            self.default_mode_row.remove_css_class('dim-label')

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
        }
    
    def get_reverse_theme_mapping(self):
        """Get mapping from config keys to display names"""
        mapping = self.get_theme_name_mapping()
        return {v: k for k, v in mapping.items()}
    
    def on_color_scheme_changed(self, combo_row, param):
        """Handle terminal color scheme change"""
        selected = combo_row.get_selected()
        scheme_names = [
            "Default", "Black on White", "Solarized Dark", "Solarized Light",
            "Monokai", "Dracula", "Nord",
            "Gruvbox Dark", "One Dark", "Tomorrow Night", "Material Dark"
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

    def on_force_internal_file_manager_changed(self, switch, *args):
        """Persist the preference for forcing the in-app file manager."""
        try:
            active = bool(switch.get_active())
            self.config.set_setting('file_manager.force_internal', active)
        except Exception as exc:
            logger.error("Failed to update file manager preference: %s", exc)

    def on_confirm_disconnect_changed(self, switch, *args):
        """Handle confirm disconnect setting change"""
        confirm = switch.get_active()
        logger.info(f"Confirm before disconnect setting changed to: {confirm}")
        self.config.set_setting('confirm-disconnect', confirm)
    
    def on_startup_behavior_changed(self, radio_button, *args):
        """Handle startup behavior radio button change"""
        show_terminal = self.terminal_startup_radio.get_active()
        behavior = 'terminal' if show_terminal else 'welcome'
        logger.info(f"App startup behavior changed to: {behavior}")
        self.config.set_setting('app-startup-behavior', behavior)
    
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
                ("konsole", "konsole"),
                ("xfce4-terminal", "xfce4-terminal"),
                ("alacritty", "alacritty"),
                ("kitty", "kitty"),
                ("terminator", "terminator"),
                ("tilix", "tilix"),
                ("xterm", "xterm"),
                ("guake", "guake"),
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

