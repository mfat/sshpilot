"""Sidebar components and drag-and-drop helpers for sshPilot."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GObject, GLib, Adw, Graphene, Pango

from gettext import gettext as _

from .connection_manager import Connection
from .connection_display import (
    get_connection_alias as _get_connection_alias,
    get_connection_host as _get_connection_host,
    format_connection_host_display as _format_connection_host_display,
)
from .groups import GroupManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------


_COLOR_CSS_INSTALLED = False
_DEFAULT_ROW_MARGIN_START = 0
_DEFAULT_ROW_WIDGET_MARGIN_START = -1
_GROUP_DISPLAY_OPTIONS = {"fullwidth", "nested"}
_GROUP_ROW_INDENT_WIDTH = 20
_MIN_VALID_MARGIN = 0


def _install_sidebar_color_css():
    global _COLOR_CSS_INSTALLED
    if _COLOR_CSS_INSTALLED:
        return

    try:
        display = Gdk.Display.get_default()
        if not display:
            return

        provider = Gtk.CssProvider()
        css = """
        .accent-red { background-color: #ff5c57; }
        .accent-blue { background-color: #51a1ff; }
        .accent-green { background-color: #5fff8d; }
        .accent-orange { background-color: #ffb347; }
        .accent-purple { background-color: #d6a2ff; }
        .accent-cyan { background-color: #5be7ff; }
        .accent-gray { background-color: #d3d7db; }
        """
        provider.load_from_data(css.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _COLOR_CSS_INSTALLED = True
    except Exception:
        logger.debug("Failed to install sidebar color CSS", exc_info=True)


def _parse_color(value: Optional[str]) -> Optional[Gdk.RGBA]:
    if not value:
        return None

    rgba = Gdk.RGBA()
    try:
        if rgba.parse(str(value)):
            return rgba
    except Exception:
        logger.debug("Failed to parse color value '%s'", value, exc_info=True)
    return None


def _get_color_display_mode(config) -> str:
    try:
        mode = str(config.get_setting('ui.group_color_display', 'fill')).lower()
    except Exception:
        return 'fill'

    if mode not in {'fill', 'badge'}:
        return 'fill'
    return mode


def _fill_rgba(rgba: Optional[Gdk.RGBA]) -> Optional[Gdk.RGBA]:
    if rgba is None:
        return None

    fill = Gdk.RGBA()
    fill.red = rgba.red
    fill.green = rgba.green
    fill.blue = rgba.blue
    fill.alpha = 0.6 if rgba.alpha >= 1.0 else max(0.5, min(rgba.alpha, 0.7))
    return fill


def _get_color_class(rgba: Optional[Gdk.RGBA]) -> Optional[str]:
    if not rgba:
        return None

    import colorsys

    h, s, _v = colorsys.rgb_to_hsv(rgba.red, rgba.green, rgba.blue)

    if s < 0.3:
        return "accent-gray"
    if h < 0.1 or h > 0.9:
        return "accent-red"
    if h < 0.2:
        return "accent-orange"
    if h < 0.4:
        return "accent-green"
    if h < 0.6:
        return "accent-cyan"
    if h < 0.8:
        return "accent-blue"
    return "accent-purple"


def _set_tint_card_color(row: Gtk.Widget, rgba: Gdk.RGBA):
    try:
        base_color = rgba.to_string()

        hover_rgba = Gdk.RGBA()
        hover_rgba.red = rgba.red
        hover_rgba.green = rgba.green
        hover_rgba.blue = rgba.blue
        hover_rgba.alpha = min(1.0, rgba.alpha + 0.12)
        hover_color = hover_rgba.to_string()

        active_rgba = Gdk.RGBA()
        active_rgba.red = rgba.red
        active_rgba.green = rgba.green
        active_rgba.blue = rgba.blue
        active_rgba.alpha = min(1.0, rgba.alpha + 0.18)
        active_color = active_rgba.to_string()
    except Exception:
        logger.debug("Failed to convert RGBA to string", exc_info=True)
        return

    try:
        selected_rgba = Gdk.RGBA()
        selected_rgba.red = rgba.red
        selected_rgba.green = rgba.green
        selected_rgba.blue = rgba.blue
        selected_rgba.alpha = min(1.0, max(rgba.alpha + 0.24, 0.55))
        selected_color = selected_rgba.to_string()

        selected_hover_rgba = Gdk.RGBA()
        selected_hover_rgba.red = rgba.red
        selected_hover_rgba.green = rgba.green
        selected_hover_rgba.blue = rgba.blue
        selected_hover_rgba.alpha = min(1.0, selected_rgba.alpha + 0.08)
        selected_hover_color = selected_hover_rgba.to_string()

        selected_active_rgba = Gdk.RGBA()
        selected_active_rgba.red = rgba.red
        selected_active_rgba.green = rgba.green
        selected_active_rgba.blue = rgba.blue
        selected_active_rgba.alpha = min(1.0, selected_rgba.alpha + 0.12)
        selected_active_color = selected_active_rgba.to_string()

        border_rgba = Gdk.RGBA()
        border_rgba.red = rgba.red
        border_rgba.green = rgba.green
        border_rgba.blue = rgba.blue
        border_rgba.alpha = 1.0
        border_color = border_rgba.to_string()
    except Exception:
        logger.debug("Failed to derive selected tint colors", exc_info=True)
        return

    try:
        provider = Gtk.CssProvider()
        css_data = f"""
        .tinted {{
            transition: background-color 0s ease;
        }}

        .tinted:not(:selected) {{
            background-color: {base_color};
        }}

        .tinted:hover:not(:selected) {{
            background-color: {hover_color};
        }}

        .tinted:active:not(:selected) {{
            background-color: {active_color};
        }}

        .tinted:selected {{
            background-color: {selected_color};
            box-shadow: inset 0 0 0 1px {border_color};
        }}

        .tinted:selected:hover {{
            background-color: {selected_hover_color};
        }}

        .tinted:selected:active {{
            background-color: {selected_active_color};
        }}
        """
        provider.load_from_data(css_data.encode('utf-8'))

        if hasattr(row, '_tint_provider') and getattr(row, '_tint_provider'):
            try:
                row.get_style_context().remove_provider(row._tint_provider)
            except Exception:
                pass

        row._tint_provider = provider  # type: ignore[attr-defined]
        row.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
    except Exception:
        logger.debug("Failed to apply tinted color", exc_info=True)

# ---------------------------------------------------------------------------
# Row widgets
# ---------------------------------------------------------------------------


class DragIndicator(Gtk.Widget):
    """Custom widget to show drop indicator line"""
    
    def __init__(self):
        super().__init__()
        self.set_size_request(-1, 3)  # 3px height
        self.set_visible(False)
    
    def do_snapshot(self, snapshot):
        """Draw the horizontal line"""
        width = self.get_width()
        height = self.get_height()
        
        # Create a Graphene rectangle for the drop indicator
        rect = Graphene.Rect()
        rect.init(8, height // 2 - 1, width - 16, 2)  # x, y, width, height
        
        # Use accent color for the drop indicator
        color = Gdk.RGBA()
        color.parse("#3584e4")  # Adwaita blue
        
        snapshot.append_color(color, rect)


class GroupRow(Gtk.ListBoxRow):
    """Row widget for group headers."""

    __gsignals__ = {
        "group-toggled": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
    }

    def __init__(self, group_info: Dict, group_manager: GroupManager, connections_dict: Dict | None = None):
        super().__init__()
        _install_sidebar_color_css()
        #self.add_css_class("navigation-sidebar")
        self.add_css_class("card")        #self.add_css_class("navigation-sidebar")
        self.group_info = group_info
        self.group_manager = group_manager
        self.group_id = group_info["id"]
        self.connections_dict = connections_dict or {}
        self._tint_provider = None
        self._color_badge_provider = None
        self._tint_provider = None
        self._color_badge_provider = None

        # Main container with drop indicators
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Drop indicator (top)
        self.drop_indicator_top = DragIndicator()
        main_box.append(self.drop_indicator_top)

        # Main content
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

        from sshpilot import icon_utils
        icon = icon_utils.new_image_from_icon_name("folder-symbolic")
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        icon.set_valign(Gtk.Align.CENTER)  # Center vertically relative to text
        content.append(icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)  # Center vertically relative to icon

        self.name_label = Gtk.Label()
        self.name_label.set_halign(Gtk.Align.START)
        self.name_label.set_xalign(0.0)  # Left-align text within label (default is 0.5/center)
        self.name_label.set_valign(Gtk.Align.CENTER)  # Center vertically when count label is hidden
        # Ellipsize when text exceeds available width
        # Per GTK4 docs: For ellipsizing labels, width-chars sets minimum width,
        # max-width-chars limits natural width. Both help control size allocation.
        # Labels with ellipsize need hexpand to fill available space and ellipsize properly.
        self.name_label.set_hexpand(True)
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_label.set_width_chars(10)  # Minimum width
        self.name_label.set_max_width_chars(25)  # Maximum natural width (prevents expansion)
        info_box.append(self.name_label)

        self.count_label = Gtk.Label()
        self.count_label.set_halign(Gtk.Align.START)
        self.count_label.set_xalign(0.0)  # Left-align text within label (default is 0.5/center)
        self.count_label.add_css_class("dim-label")
        # Ellipsize when text exceeds available width
        # Per GTK4 docs: For ellipsizing labels, width-chars sets minimum width,
        # max-width-chars limits natural width. Both help control size allocation.
        # Labels with ellipsize need hexpand to fill available space and ellipsize properly.
        self.count_label.set_hexpand(True)
        self.count_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.count_label.set_width_chars(10)  # Minimum width
        self.count_label.set_max_width_chars(25)  # Maximum natural width (prevents expansion)
        # Set initial visibility based on preference
        config = getattr(self.group_manager, 'config', None)
        show_group_count = config.get_setting('ui.sidebar_show_group_count', True) if config else True
        self.count_label.set_visible(show_group_count)
        info_box.append(self.count_label)

        content.append(info_box)

        # Edit button - only visible on hover
        # Use opacity instead of visibility to reserve space and prevent row resizing
        self.edit_button = icon_utils.new_button_from_icon_name("document-edit-symbolic")
        self.edit_button.add_css_class("flat")
        self.edit_button.add_css_class("group-edit-button")
        self.edit_button.set_tooltip_text(_("Edit Group"))
        self.edit_button.set_valign(Gtk.Align.CENTER)
        self.edit_button.set_opacity(0.0)  # Hidden by default but reserves space
        self.edit_button.connect("clicked", self._on_edit_clicked)
        content.append(self.edit_button)
        
        # Set up hover events to show/hide button
        self._setup_edit_button_hover()

        self.color_badge = icon_utils.new_image_from_icon_name("tag-symbolic")
        self.color_badge.add_css_class("sidebar-color-badge")
        self.color_badge.set_icon_size(Gtk.IconSize.NORMAL)
        self.color_badge.set_valign(Gtk.Align.CENTER)
        self.color_badge.set_visible(False)
        content.append(self.color_badge)

        self.expand_button = Gtk.Button()
        icon_utils.set_button_icon(self.expand_button, "pan-end-symbolic")
        self.expand_button.add_css_class("flat")
        self.expand_button.add_css_class("group-expand-button")
        self.expand_button.set_can_focus(False)
        self.expand_button.connect("clicked", self._on_expand_clicked)
        content.append(self.expand_button)

        # Add drop target indicator (initially hidden)
        self.drop_target_indicator = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.drop_target_indicator.set_halign(Gtk.Align.CENTER)
        self.drop_target_indicator.set_margin_top(4)
        self.drop_target_indicator.set_margin_bottom(4)
        self.drop_target_indicator.add_css_class("drop-target-indicator")
        
        drop_icon = icon_utils.new_image_from_icon_name("list-add-symbolic")
        drop_icon.set_icon_size(Gtk.IconSize.NORMAL)
        self.drop_target_indicator.append(drop_icon)
        
        drop_label = Gtk.Label()
        drop_label.set_markup("<b>Add to Group</b>")
        drop_label.add_css_class("accent")
        self.drop_target_indicator.append(drop_label)
        
        self.drop_target_indicator.set_visible(False)

        # Add content to main_box
        main_box.append(content)
        main_box.append(self.drop_target_indicator)
        
        # Drop indicator (bottom)
        self.drop_indicator_bottom = DragIndicator()
        main_box.append(self.drop_indicator_bottom)

        self.set_child(main_box)
        self.set_selectable(True)
        self.set_can_focus(True)

        self._update_display()
        self._setup_drag_source()
        self._setup_double_click_gesture()

    # -- internal helpers -------------------------------------------------

    def _update_display(self):
        from sshpilot import icon_utils
        if self.group_info.get("expanded", True):
            icon_utils.set_button_icon(self.expand_button, "pan-down-symbolic")
        else:
            icon_utils.set_button_icon(self.expand_button, "pan-end-symbolic")

        actual_connections = [
            c
            for c in self.group_info.get("connections", [])
            if c in self.connections_dict
        ]
        count = len(actual_connections)
        group_name = self.group_info['name']
        self.name_label.set_markup(f"<b>{group_name}</b>")
        self.count_label.set_text(f"{count} connections")
        self._apply_group_color_style()


    def _on_expand_clicked(self, button):
        self._toggle_expand()

    def _setup_drag_source(self):
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        drag_source.connect("drag-end", self._on_drag_end)
        self.add_controller(drag_source)
        # Store reference for cleanup
        self._drag_source = drag_source

    def _on_drag_prepare(self, source, x, y):
        payload = {"type": "group", "group_id": self.group_id}
        return Gdk.ContentProvider.new_for_value(payload)

    def _on_drag_begin(self, source, drag):
        try:
            window = self.get_root()
            if window:
                # Track which group is being dragged
                window._dragged_group_id = self.group_id
                _show_ungrouped_area(window)
        except Exception as e:
            logger.error(f"Error in group drag begin: {e}")

    def _on_drag_end(self, source, drag, delete_data):
        try:
            window = self.get_root()
            if window:
                # Clear the dragged group tracking
                if hasattr(window, "_dragged_group_id"):
                    delattr(window, "_dragged_group_id")
                _hide_ungrouped_area(window)
        except Exception as e:
            logger.error(f"Error in group drag end: {e}")

    def _setup_double_click_gesture(self):
        gesture = Gtk.GestureClick()
        gesture.set_button(1)
        gesture.connect("pressed", self._on_double_click)
        self.add_controller(gesture)

    def _on_double_click(self, gesture, n_press, x, y):
        if n_press == 2:
            self._toggle_expand()

    def _toggle_expand(self):
        expanded = not self.group_info.get("expanded", True)
        self.group_info["expanded"] = expanded
        self.group_manager.set_group_expanded(self.group_id, expanded)
        self._update_display()
        self.emit("group-toggled", self.group_id, expanded)

    def _on_edit_clicked(self, button):
        """Handle edit button click"""
        try:
            window = self.get_root()
            if window and hasattr(window, 'on_edit_group_action'):
                # Set the context menu group row so the action knows which group to edit
                window._context_menu_group_row = self
                window.on_edit_group_action(None, None)
        except Exception as e:
            logger.error(f"Error editing group {self.group_id}: {e}")

    def _setup_edit_button_hover(self):
        """Set up hover events to show/hide edit button"""
        # Track hover state
        self._is_hovering_edit = False
        
        # Motion controller for the row
        motion_controller = Gtk.EventControllerMotion()
        motion_controller.connect("enter", self._on_row_enter_edit)
        motion_controller.connect("leave", self._on_row_leave_edit)
        self.add_controller(motion_controller)
        
        # Motion controller for the button itself (to keep it visible when hovering over button)
        if self.edit_button:
            button_motion_controller = Gtk.EventControllerMotion()
            button_motion_controller.connect("enter", self._on_button_enter_edit)
            button_motion_controller.connect("leave", self._on_button_leave_edit)
            self.edit_button.add_controller(button_motion_controller)

    def _on_row_enter_edit(self, controller, x, y):
        """Show edit button when mouse enters row"""
        self._is_hovering_edit = True
        if self.edit_button:
            self.edit_button.set_opacity(1.0)

    def _on_row_leave_edit(self, controller):
        """Hide edit button when mouse leaves row"""
        self._is_hovering_edit = False
        # Use a small delay to allow moving to the button
        GLib.timeout_add(100, self._maybe_hide_edit_button)

    def _on_button_enter_edit(self, controller, x, y):
        """Keep button visible when hovering over it"""
        self._is_hovering_edit = True
        if self.edit_button:
            self.edit_button.set_opacity(1.0)

    def _on_button_leave_edit(self, controller):
        """Handle mouse leaving the button"""
        self._is_hovering_edit = False
        GLib.timeout_add(100, self._maybe_hide_edit_button)

    def _maybe_hide_edit_button(self):
        """Hide button if not hovering"""
        if not self._is_hovering_edit and self.edit_button:
            self.edit_button.set_opacity(0.0)
        return False  # Don't repeat

    def _apply_group_color_style(self):
        config = getattr(self.group_manager, 'config', None)
        mode = _get_color_display_mode(config) if config else 'fill'
        rgba = _parse_color(self.group_info.get('color'))

        if mode == 'badge':
            self.remove_css_class("tinted")
            if hasattr(self, '_tint_provider') and self._tint_provider:
                try:
                    self.get_style_context().remove_provider(self._tint_provider)
                except Exception:
                    pass
                self._tint_provider = None

            if rgba:
                self._update_color_badge(rgba)
                self.color_badge.set_visible(True)
            else:
                self.color_badge.set_visible(False)
        else:
            self.color_badge.set_visible(False)
            if rgba:
                tint = _fill_rgba(rgba) or rgba
                self.add_css_class("tinted")
                _set_tint_card_color(self, tint)
            else:
                self.remove_css_class("tinted")
                if hasattr(self, '_tint_provider') and self._tint_provider:
                    try:
                        self.get_style_context().remove_provider(self._tint_provider)
                    except Exception:
                        pass
                    self._tint_provider = None

    def _update_color_badge(self, rgba: Gdk.RGBA):
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        color_hex = f"#{r:02x}{g:02x}{b:02x}"

        css_data = f"""
        image.sidebar-color-badge {{
          color: {color_hex};
        }}
        """

        if self._color_badge_provider:
            try:
                self.color_badge.get_style_context().remove_provider(self._color_badge_provider)
            except Exception:
                pass

        self._color_badge_provider = Gtk.CssProvider()
        self._color_badge_provider.load_from_data(css_data.encode('utf-8'))
        self.color_badge.get_style_context().add_provider(
            self._color_badge_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

        # Remove any accent classes that might add background color
        for cls in ("accent-red", "accent-blue", "accent-green", "accent-orange", "accent-purple", "accent-cyan", "accent-gray"):
            self.color_badge.remove_css_class(cls)

    def show_drop_indicator(self, top: bool):
        """Show drop indicator line"""
        self.hide_drop_indicators()
        
        if top:
            self.drop_indicator_top.set_visible(True)
        else:
            self.drop_indicator_bottom.set_visible(True)
    
    def hide_drop_indicators(self):
        """Hide all drop indicator lines"""
        self.drop_indicator_top.set_visible(False)
        self.drop_indicator_bottom.set_visible(False)
        self.show_group_highlight(False)

    def show_group_highlight(self, show: bool):
        """Show/hide group highlight for 'add to group' drop indication"""
        if show:
            self.add_css_class("drop-target-group")
            self.drop_target_indicator.set_visible(True)
        else:
            self.remove_css_class("drop-target-group")
            self.drop_target_indicator.set_visible(False)


class ConnectionRow(Gtk.ListBoxRow):
    """Row widget for connection list."""

    def __init__(self, connection: Connection, group_manager: GroupManager, config, file_manager_callback=None):
        super().__init__()
        _install_sidebar_color_css()
        #self.add_css_class("navigation-sidebar")
        self.add_css_class("card")
        self.connection = connection
        self.group_manager = group_manager
        self.config = config
        self._file_manager_callback = file_manager_callback
        self._tint_provider = None
        self._color_badge_provider = None
        self._indent_level = 0
        self._group_display_mode = None
        self._row_margin_base = None
        self._content_margin_base = None

        # Main container with drop indicators
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Drop indicator (top)
        self.drop_indicator_top = DragIndicator()
        main_box.append(self.drop_indicator_top)
        
        # Content container
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._content_box = content
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

        from sshpilot import icon_utils
        self.connection_icon = icon_utils.new_image_from_icon_name("computer-symbolic")
        self.connection_icon.set_icon_size(Gtk.IconSize.NORMAL)
        self.connection_icon.set_valign(Gtk.Align.CENTER)  # Center vertically relative to text
        # Set initial visibility based on preference
        show_connection_icon = self.config.get_setting('ui.sidebar_show_connection_icon', True)
        self.connection_icon.set_visible(show_connection_icon)
        content.append(self.connection_icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)  # Center vertically relative to icon

        self.nickname_label = Gtk.Label()
        self.nickname_label.set_markup(f"<b>{connection.nickname}</b>")
        self.nickname_label.set_halign(Gtk.Align.START)
        self.nickname_label.set_xalign(0.0)  # Left-align text within label (default is 0.5/center)
        self.nickname_label.set_valign(Gtk.Align.CENTER)  # Center vertically when host label is hidden
        # Ellipsize when text exceeds available width
        # Per GTK4 docs: For ellipsizing labels, width-chars sets minimum width,
        # max-width-chars limits natural width. Both help control size allocation.
        # Labels with ellipsize need hexpand to fill available space and ellipsize properly.
        self.nickname_label.set_hexpand(True)
        self.nickname_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.nickname_label.set_width_chars(10)  # Minimum width
        self.nickname_label.set_max_width_chars(25)  # Maximum natural width (prevents expansion)
        info_box.append(self.nickname_label)

        self.host_label = Gtk.Label()
        self.host_label.set_halign(Gtk.Align.START)
        self.host_label.set_xalign(0.0)  # Left-align text within label (default is 0.5/center)
        self.host_label.add_css_class("dim-label")
        # Ellipsize when text exceeds available width
        # Per GTK4 docs: For ellipsizing labels, width-chars sets minimum width,
        # max-width-chars limits natural width. Both help control size allocation.
        # Labels with ellipsize need hexpand to fill available space and ellipsize properly.
        self.host_label.set_hexpand(True)
        self.host_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.host_label.set_width_chars(10)  # Minimum width
        self.host_label.set_max_width_chars(25)  # Maximum natural width (prevents expansion)
        self._apply_host_label_text()
        # Set initial visibility based on preference
        show_user_hostname = self.config.get_setting('ui.sidebar_show_user_hostname', False)
        self.host_label.set_visible(show_user_hostname)
        info_box.append(self.host_label)

        content.append(info_box)

        self.indicator_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.indicator_box.set_halign(Gtk.Align.CENTER)
        self.indicator_box.set_valign(Gtk.Align.CENTER)
        content.append(self.indicator_box)

        self.color_badge = icon_utils.new_image_from_icon_name("tag-symbolic")
        self.color_badge.add_css_class("sidebar-color-badge")
        self.color_badge.set_icon_size(Gtk.IconSize.NORMAL)
        self.color_badge.set_valign(Gtk.Align.CENTER)
        self.color_badge.set_visible(False)
        content.append(self.color_badge)

        # File manager button (before status icon) - only visible on hover
        # Use opacity instead of visibility to reserve space and prevent row resizing
        from sshpilot import icon_utils
        self.file_manager_button = icon_utils.new_button_from_icon_name("folder-symbolic")
        self.file_manager_button.add_css_class("flat")
        self.file_manager_button.add_css_class("file-manager-button")
        self.file_manager_button.set_tooltip_text(_("Manage Files"))
        self.file_manager_button.set_valign(Gtk.Align.CENTER)
        self.file_manager_button.set_opacity(0.0)  # Hidden by default but reserves space
        if file_manager_callback:
            self.file_manager_button.connect("clicked", self._on_file_manager_clicked)
        content.append(self.file_manager_button)
        
        # Set up hover events to show/hide button
        self._setup_file_manager_button_hover()

        from sshpilot import icon_utils
        self.status_icon = icon_utils.new_image_from_icon_name("network-offline-symbolic")
        self.status_icon.set_pixel_size(16)
        # Set initial visibility based on preference
        show_status = self.config.get_setting('ui.sidebar_show_connection_status', True)
        self.status_icon.set_visible(show_status)
        content.append(self.status_icon)
        
        # Now add the content to main_box
        main_box.append(content)
        
        # Drop indicator (bottom)
        self.drop_indicator_bottom = DragIndicator()
        main_box.append(self.drop_indicator_bottom)

        # Set the main box as the child directly (no pulse overlay)
        self.set_child(main_box)

        self.set_selectable(True)

        self.update_status()
        self._update_forwarding_indicators()
        self._setup_drag_source()
        self._apply_group_color_style()

    def _on_file_manager_clicked(self, button):
        """Handle file manager button click"""
        if self._file_manager_callback:
            try:
                self._file_manager_callback(self.connection)
            except Exception as e:
                logger.error(f"Error opening file manager for {self.connection.nickname}: {e}")

    def _setup_file_manager_button_hover(self):
        """Set up hover events to show/hide file manager button"""
        # Track hover state
        self._is_hovering = False
        
        # Motion controller for the row
        motion_controller = Gtk.EventControllerMotion()
        motion_controller.connect("enter", self._on_row_enter)
        motion_controller.connect("leave", self._on_row_leave)
        self.add_controller(motion_controller)
        
        # Motion controller for the button itself (to keep it visible when hovering over button)
        if self.file_manager_button:
            button_motion_controller = Gtk.EventControllerMotion()
            button_motion_controller.connect("enter", self._on_button_enter)
            button_motion_controller.connect("leave", self._on_button_leave)
            self.file_manager_button.add_controller(button_motion_controller)

    def _on_row_enter(self, controller, x, y):
        """Show file manager button when mouse enters row"""
        self._is_hovering = True
        if self.file_manager_button and self._file_manager_callback:
            self.file_manager_button.set_opacity(1.0)

    def _on_row_leave(self, controller):
        """Hide file manager button when mouse leaves row"""
        self._is_hovering = False
        # Use a small delay to allow moving to the button
        GLib.timeout_add(100, self._maybe_hide_button)

    def _on_button_enter(self, controller, x, y):
        """Keep button visible when hovering over it"""
        self._is_hovering = True
        if self.file_manager_button:
            self.file_manager_button.set_opacity(1.0)

    def _on_button_leave(self, controller):
        """Handle mouse leaving the button"""
        self._is_hovering = False
        GLib.timeout_add(100, self._maybe_hide_button)

    def _maybe_hide_button(self):
        """Hide button if not hovering"""
        if not self._is_hovering and self.file_manager_button:
            self.file_manager_button.set_opacity(0.0)
        return False  # Don't repeat

    def show_drop_indicator(self, top: bool):
        """Show drop indicator line"""
        self.hide_drop_indicators()
        
        if top:
            self.drop_indicator_top.set_visible(True)
        else:
            self.drop_indicator_bottom.set_visible(True)
    
    def hide_drop_indicators(self):
        """Hide all drop indicator lines"""
        self.drop_indicator_top.set_visible(False)
        self.drop_indicator_bottom.set_visible(False)
    
    def set_indentation(self, level: int):
        """Set indentation level for grouped connections"""
        try:
            self._indent_level = max(0, int(level or 0))
        except Exception:
            self._indent_level = 0

        content = getattr(self, '_content_box', None)
        if not content:
            main_box = self.get_child()
            if not main_box:
                return

            top_indicator = main_box.get_first_child()
            content = top_indicator.get_next_sibling() if top_indicator else None
            if not content:
                return
            self._content_box = content

        global _DEFAULT_ROW_MARGIN_START, _DEFAULT_ROW_WIDGET_MARGIN_START

        if self._content_margin_base is None or _DEFAULT_ROW_MARGIN_START <= _MIN_VALID_MARGIN:
            _DEFAULT_ROW_MARGIN_START = content.get_margin_start()
            self._content_margin_base = _DEFAULT_ROW_MARGIN_START
        else:
            self._content_margin_base = _DEFAULT_ROW_MARGIN_START

        if self._row_margin_base is None or _DEFAULT_ROW_WIDGET_MARGIN_START <= _MIN_VALID_MARGIN:
            _DEFAULT_ROW_WIDGET_MARGIN_START = self.get_margin_start()
            if _DEFAULT_ROW_WIDGET_MARGIN_START < _MIN_VALID_MARGIN:
                _DEFAULT_ROW_WIDGET_MARGIN_START = 0
            self._row_margin_base = _DEFAULT_ROW_WIDGET_MARGIN_START
        else:
            self._row_margin_base = _DEFAULT_ROW_WIDGET_MARGIN_START

        self._apply_group_display_mode()

    def refresh_group_display_mode(self, new_mode: Optional[str] = None):
        """Refresh indentation styling when the preference changes."""
        if new_mode:
            normalized = str(new_mode).lower()
            if normalized in _GROUP_DISPLAY_OPTIONS:
                self._group_display_mode = normalized
        else:
            # Force new lookup from config
            self._group_display_mode = None

        self._apply_group_display_mode()

    def _get_group_display_mode(self) -> str:
        if self._group_display_mode in _GROUP_DISPLAY_OPTIONS:
            return self._group_display_mode

        mode = 'nested'
        config = getattr(self, 'config', None)
        if config:
            try:
                value = str(config.get_setting('ui.group_row_display', mode)).lower()
                if value in _GROUP_DISPLAY_OPTIONS:
                    mode = value
            except Exception:
                pass

        self._group_display_mode = mode
        return mode

    def _apply_group_display_mode(self):
        content = getattr(self, '_content_box', None)
        if not content:
            return

        if self._content_margin_base is None:
            self._content_margin_base = content.get_margin_start()

        if self._row_margin_base is None:
            self._row_margin_base = max(self.get_margin_start(), 0)

        indent_level = getattr(self, '_indent_level', 0)
        indent_px = max(0, indent_level) * _GROUP_ROW_INDENT_WIDTH

        mode = self._get_group_display_mode()

        if indent_px <= 0:
            self.set_margin_start(self._row_margin_base)
            content.set_margin_start(self._content_margin_base)
            return

        if mode == 'fullwidth':
            self.set_margin_start(self._row_margin_base)
            content.set_margin_start(self._content_margin_base + indent_px)
        else:  # nested mode
            self.set_margin_start(self._row_margin_base + indent_px)
            content.set_margin_start(self._content_margin_base)

    def _resolve_group_color(self) -> Optional[Gdk.RGBA]:
        manager = getattr(self, 'group_manager', None)
        if not manager:
            return None

        try:
            group_id = manager.get_connection_group(self.connection.nickname)
        except Exception:
            group_id = None

        visited = set()
        while group_id:
            if group_id in visited:
                break
            visited.add(group_id)

            group_info = None
            try:
                group_info = manager.groups.get(group_id)
            except Exception:
                group_info = None

            if not group_info:
                break

            color = _parse_color(group_info.get('color'))
            if color:
                return color

            group_id = group_info.get('parent_id')

        return None

    def _apply_group_color_style(self):
        mode = _get_color_display_mode(getattr(self, 'config', None))
        rgba = self._resolve_group_color()

        if mode == 'badge':
            self.remove_css_class("tinted")
            if hasattr(self, '_tint_provider') and self._tint_provider:
                try:
                    self.get_style_context().remove_provider(self._tint_provider)
                except Exception:
                    pass
                self._tint_provider = None

            if rgba:
                self._update_color_badge(rgba)
                self.color_badge.set_visible(True)
            else:
                self.color_badge.set_visible(False)
        else:
            self.color_badge.set_visible(False)
            if rgba:
                self.add_css_class("tinted")
                _set_tint_card_color(self, _fill_rgba(rgba) or rgba)
            else:
                self.remove_css_class("tinted")
                if hasattr(self, '_tint_provider') and self._tint_provider:
                    try:
                        self.get_style_context().remove_provider(self._tint_provider)
                    except Exception:
                        pass
                    self._tint_provider = None

    def _update_color_badge(self, rgba: Gdk.RGBA):
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        color_hex = f"#{r:02x}{g:02x}{b:02x}"

        css_data = f"""
        image.sidebar-color-badge {{
          color: {color_hex};
        }}
        """

        if self._color_badge_provider:
            try:
                self.color_badge.get_style_context().remove_provider(self._color_badge_provider)
            except Exception:
                pass

        self._color_badge_provider = Gtk.CssProvider()
        self._color_badge_provider.load_from_data(css_data.encode('utf-8'))
        self.color_badge.get_style_context().add_provider(
            self._color_badge_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

        # Remove any accent classes that might add background colors
        for cls in ("accent-red", "accent-blue", "accent-green", "accent-orange", "accent-purple", "accent-cyan", "accent-gray"):
            self.color_badge.remove_css_class(cls)

    # -- drag source ------------------------------------------------------

    def _setup_drag_source(self):
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        drag_source.connect("drag-end", self._on_drag_end)
        self.add_controller(drag_source)
        # Store reference for cleanup
        self._drag_source = drag_source

    def _on_drag_prepare(self, source, x, y):
        window = self.get_root()

        connections_payload: List[Dict[str, Optional[int | str]]] = []
        selection_order = 0

        if window and hasattr(window, "connection_list"):
            try:
                selected_rows = list(window.connection_list.get_selected_rows())
            except Exception:
                selected_rows = []

            if not selected_rows or self not in selected_rows:
                selected_rows.append(self)

            seen_nicknames = set()
            for row in selected_rows:
                connection_obj = getattr(row, "connection", None)
                nickname = getattr(connection_obj, "nickname", None)
                if not nickname or nickname in seen_nicknames:
                    continue

                seen_nicknames.add(nickname)

                row_index = None
                try:
                    idx = row.get_index()
                    if isinstance(idx, int) and idx >= 0:
                        row_index = idx
                except Exception:
                    row_index = None

                connections_payload.append(
                    {
                        "nickname": nickname,
                        "index": row_index,
                        "order": selection_order,
                    }
                )
                selection_order += 1

        if not connections_payload:
            row_index = None
            try:
                idx = self.get_index()
                if isinstance(idx, int) and idx >= 0:
                    row_index = idx
            except Exception:
                row_index = None

            connections_payload.append(
                {
                    "nickname": self.connection.nickname,
                    "index": row_index,
                    "order": 0,
                }
            )

        connections_payload.sort(
            key=lambda item: (
                item.get("index") is None,
                item.get("index") if isinstance(item.get("index"), int) else item.get("order", 0),
            )
        )

        ordered_nicknames: List[str] = []
        for item in connections_payload:
            nickname = item.get("nickname")
            if isinstance(nickname, str) and nickname not in ordered_nicknames:
                ordered_nicknames.append(nickname)
            item.pop("order", None)

        payload = {
            "type": "connection",
            "connection_nickname": ordered_nicknames[0] if ordered_nicknames else self.connection.nickname,
            "connection_nicknames": ordered_nicknames,
            "connections": connections_payload,
        }

        if window:
            window._dragged_connections = ordered_nicknames

        return Gdk.ContentProvider.new_for_value(payload)

    def _on_drag_begin(self, source, drag):
        try:
            window = self.get_root()
            if window:
                if not hasattr(window, "_dragged_connections"):
                    window._dragged_connections = [self.connection.nickname]
                window._drag_in_progress = True
                _show_ungrouped_area(window)
        except Exception as e:
            logger.error(f"Error in drag begin: {e}")

    def _on_drag_end(self, source, drag, delete_data):
        try:
            window = self.get_root()
            if window:
                if hasattr(window, "_dragged_connections"):
                    delattr(window, "_dragged_connections")
                window._drag_in_progress = False
                _hide_ungrouped_area(window)
        except Exception as e:
            logger.error(f"Error in drag end: {e}")

    # -- display updates --------------------------------------------------

    @staticmethod
    def _install_pf_css():
        try:
            display = Gdk.Display.get_default()
            if not display:
                return
            if getattr(display, "_pf_css_installed", False):
                return
            provider = Gtk.CssProvider()
            css = """
            .pf-indicator {}
            .pf-local { color: #E01B24; }
            .pf-remote { color: #2EC27E; }
            .pf-dynamic { color: #3584E4; }
            """
            provider.load_from_data(css.encode("utf-8"))
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            setattr(display, "_pf_css_installed", True)
        except Exception:
            pass

    def _update_forwarding_indicators(self):
        self._install_pf_css()
        try:
            while self.indicator_box.get_first_child():
                self.indicator_box.remove(self.indicator_box.get_first_child())
        except Exception:
            return

        # Check preference for showing port forwarding indicators
        show_port_forwarding = self.config.get_setting('ui.sidebar_show_port_forwarding', False)
        if not show_port_forwarding:
            return

        rules = getattr(self.connection, "forwarding_rules", []) or []
        has_local = any(r.get("enabled", True) and r.get("type") == "local" for r in rules)
        has_remote = any(r.get("enabled", True) and r.get("type") == "remote" for r in rules)
        has_dynamic = any(r.get("enabled", True) and r.get("type") == "dynamic" for r in rules)

        def make_badge(letter: str, cls: str):
            circled_map = {"L": "\u24C1", "R": "\u24C7", "D": "\u24B9"}
            glyph = circled_map.get(letter, letter)
            lbl = Gtk.Label(label=glyph)
            lbl.add_css_class(cls)
            lbl.set_halign(Gtk.Align.CENTER)
            lbl.set_valign(Gtk.Align.CENTER)
            try:
                lbl.set_xalign(0.5)
                lbl.set_yalign(0.5)
            except Exception:
                pass
            return lbl

        if has_local:
            self.indicator_box.append(make_badge("L", "pf-local"))
        if has_remote:
            self.indicator_box.append(make_badge("R", "pf-remote"))
        if has_dynamic:
            self.indicator_box.append(make_badge("D", "pf-dynamic"))

    def _apply_host_label_text(self, include_port: bool | None = None):
        try:
            window = self.get_root()
            hide = bool(getattr(window, "_hide_hosts", False)) if window else False
        except Exception:
            hide = False

        if hide:
            self.host_label.set_text("••••••••••")
            return

        format_kwargs = {}
        if include_port is not None:
            format_kwargs["include_port"] = include_port

        display = _format_connection_host_display(self.connection, **format_kwargs)
        self.host_label.set_text(display or '')

    def apply_hide_hosts(self, hide: bool):
        self._apply_host_label_text()

    def update_status(self):
        try:
            window = self.get_root()
            has_active_terminal = False

            if hasattr(window, "connection_to_terminals") and self.connection in getattr(window, "connection_to_terminals", {}):
                for t in window.connection_to_terminals.get(self.connection, []) or []:
                    if getattr(t, "is_connected", False):
                        has_active_terminal = True
                        break
            elif hasattr(window, "active_terminals") and self.connection in window.active_terminals:
                terminal = window.active_terminals[self.connection]
                if terminal and hasattr(terminal, "is_connected"):
                    has_active_terminal = terminal.is_connected

            self.connection.is_connected = has_active_terminal

            from sshpilot import icon_utils
            if has_active_terminal:
                icon_utils.set_icon_from_name(self.status_icon, "network-idle-symbolic")
                host_value = _get_connection_host(self.connection) or _get_connection_alias(self.connection)
                self.status_icon.set_tooltip_text(
                    f"Connected to {host_value}"
                )
            else:
                icon_utils.set_icon_from_name(self.status_icon, "network-offline-symbolic")
                self.status_icon.set_tooltip_text("Disconnected")

            self.status_icon.queue_draw()
        except Exception as e:
            logger.error(
                f"Error updating status for {getattr(self.connection, 'nickname', 'connection')}: {e}"
            )

        self._apply_group_color_style()

    def update_display(self):
        if hasattr(self.connection, "nickname") and hasattr(self, "nickname_label"):
            self.nickname_label.set_markup(f"<b>{self.connection.nickname}</b>")

        if hasattr(self.connection, "username") and hasattr(self, "host_label"):
            self._apply_host_label_text(include_port=True)
        self._update_forwarding_indicators()
        self.update_status()


# ---------------------------------------------------------------------------
# Drag-and-drop helpers
# ---------------------------------------------------------------------------


def _coerce_drag_value(value):
    """Extract the Python payload from drag values produced by ``DragSource``."""

    if isinstance(value, GObject.Value):
        for getter in ("get_boxed", "get_object", "get"):
            try:
                extracted = getattr(value, getter)()
            except Exception:
                extracted = None
            if extracted is not None:
                return extracted
        return None

    return value


def setup_connection_list_dnd(window):
    """Set up drag and drop for the window's connection list."""

    drop_target = Gtk.DropTarget.new(type=GObject.TYPE_PYOBJECT, actions=Gdk.DragAction.MOVE)
    drop_target.connect(
        "accept", lambda target, value: isinstance(_coerce_drag_value(value), dict)
    )
    drop_target.connect(
        "drop", lambda target, value, x, y: _on_connection_list_drop(window, value, x, y)
    )
    window.connection_list.add_controller(drop_target)

    motion_controller = Gtk.DropControllerMotion()
    motion_controller.connect(
        "enter", lambda controller, x, y: _on_connection_list_motion(window, x, y)
    )
    motion_controller.connect(
        "motion", lambda controller, x, y: _on_connection_list_motion(window, x, y)
    )
    motion_controller.connect(
        "leave", lambda controller: _on_connection_list_leave(window)
    )
    window.connection_list.add_controller(motion_controller)

    window._drop_indicator_row = None
    window._drop_indicator_position = None
    window._ungrouped_area_row = None
    window._ungrouped_area_visible = False
    window._connection_autoscroll_timeout_id = 0
    window._connection_autoscroll_velocity = 0.0
    if not hasattr(window, "_connection_autoscroll_margin"):
        window._connection_autoscroll_margin = 48.0
    if not hasattr(window, "_connection_autoscroll_max_velocity"):
        window._connection_autoscroll_max_velocity = 28.0
    if not hasattr(window, "_connection_autoscroll_interval_ms"):
        window._connection_autoscroll_interval_ms = 16


def _on_connection_list_motion(window, x, y):
    try:
        # Prevent row selection during drag by temporarily disabling selection
        if not hasattr(window, '_drag_in_progress'):
            window._drag_in_progress = True
            window.connection_list.set_selection_mode(Gtk.SelectionMode.NONE)

        # Throttle motion events to improve performance
        current_time = GLib.get_monotonic_time()
        if hasattr(window, '_last_motion_time'):
            if current_time - window._last_motion_time < 16000:  # ~16ms = 60fps
                return Gdk.DragAction.MOVE
        window._last_motion_time = current_time

        _show_ungrouped_area(window)
        _update_connection_autoscroll(window, y)

        row = window.connection_list.get_row_at_y(int(y))
        if not row:
            _clear_drop_indicator(window)
            return Gdk.DragAction.MOVE

        if getattr(row, "ungrouped_area", False):
            # For ungrouped area, we don't need special highlighting
            _clear_drop_indicator(window)
            window._drop_indicator_row = row
            window._drop_indicator_position = "ungrouped"
            return Gdk.DragAction.MOVE

        # Show indicators for valid drop targets
        if hasattr(row, "show_drop_indicator"):
            row_y = row.get_allocation().y
            row_height = row.get_allocation().height
            relative_y = y - row_y
            position = "above" if relative_y < row_height / 2 else "below"
            
            # Handle connection rows
            if hasattr(row, "connection"):
                dragged = set(getattr(window, "_dragged_connections", []) or [])
                nickname = getattr(getattr(row, "connection", None), "nickname", None)
                if dragged and nickname in dragged:
                    _clear_drop_indicator(window)
                    return Gdk.DragAction.MOVE

                _show_drop_indicator(window, row, position)
            
            # Handle group rows
            elif (hasattr(row, "group_id") and hasattr(window, "_dragged_group_id")):
                # Don't show indicators on the group being dragged
                if row.group_id == window._dragged_group_id:
                    _clear_drop_indicator(window)
                    return Gdk.DragAction.MOVE
                
                # Only show indicator if this is a different group
                _show_drop_indicator(window, row, position)
            
            # Handle mixed drag scenarios (dragging connection over group, etc.)
            elif (hasattr(row, "connection") and hasattr(window, "_dragged_group_id")):
                # Dragging group over connection - show indicator
                _show_drop_indicator(window, row, position)
            elif (hasattr(row, "group_id") and getattr(window, "_dragged_connections", None)):
                _show_drop_indicator_on_group(window, row)
            else:
                _clear_drop_indicator(window)
        else:
            # Clear indicators if we're over a non-valid target
            _clear_drop_indicator(window)
        return Gdk.DragAction.MOVE
    except Exception as e:
        logger.error(f"Error handling motion: {e}")
        return Gdk.DragAction.MOVE


def _on_connection_list_leave(window):
    _clear_drop_indicator(window)
    _hide_ungrouped_area(window)
    _stop_connection_autoscroll(window)

    # Restore selection mode after drag
    if hasattr(window, '_drag_in_progress'):
        window._drag_in_progress = False
        window.connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
    
    return True


def _show_drop_indicator(window, row, position):
    try:
        # Only update if the indicator has changed
        if (window._drop_indicator_row != row or
            window._drop_indicator_position != position):
            
            # Clear any existing indicators
            if window._drop_indicator_row and hasattr(window._drop_indicator_row, 'hide_drop_indicators'):
                window._drop_indicator_row.hide_drop_indicators()
            
            # Show indicator on the target row
            if hasattr(row, 'show_drop_indicator'):
                show_top = (position == "above")
                row.show_drop_indicator(show_top)

            window._drop_indicator_row = row
            window._drop_indicator_position = position
    except Exception as e:
        logger.error(f"Error showing drop indicator: {e}")


def _show_drop_indicator_on_group(window, row):
    """Show a special indicator when dropping a connection onto a group (adds to group)"""
    try:
        # Only update if the indicator has changed
        if (window._drop_indicator_row != row or
            window._drop_indicator_position != "on_group"):
            
            # Clear any existing indicators
            if window._drop_indicator_row and hasattr(window._drop_indicator_row, 'hide_drop_indicators'):
                window._drop_indicator_row.hide_drop_indicators()
            
            # Show group highlight indicator instead of line indicators
            if hasattr(row, 'show_group_highlight'):
                row.show_group_highlight(True)
            elif hasattr(row, 'show_drop_indicator'):
                # Fallback: show bottom indicator if group highlight not available
                row.show_drop_indicator(False)

            window._drop_indicator_row = row
            window._drop_indicator_position = "on_group"
    except Exception as e:
        logger.error(f"Error showing group drop indicator: {e}")


def _create_ungrouped_area(window):
    if window._ungrouped_area_row:
        return window._ungrouped_area_row

    ungrouped_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

    from sshpilot import icon_utils
    icon = icon_utils.new_image_from_icon_name("folder-open-symbolic")
    icon.set_pixel_size(24)
    icon.add_css_class("dim-label")

    label = Gtk.Label(label=_("Drop connections here to ungroup them"))
    label.add_css_class("dim-label")
    label.add_css_class("caption")

    ungrouped_area.append(icon)
    ungrouped_area.append(label)

    ungrouped_row = Gtk.ListBoxRow()
    ungrouped_row.set_child(ungrouped_area)
    ungrouped_row.set_selectable(False)
    ungrouped_row.set_activatable(False)
    ungrouped_row.ungrouped_area = True

    window._ungrouped_area_row = ungrouped_row
    return ungrouped_row


def _show_ungrouped_area(window):
    try:
        if window._ungrouped_area_visible:
            return

        hierarchy = window.group_manager.get_group_hierarchy()
        if not hierarchy:
            return

        ungrouped_row = _create_ungrouped_area(window)
        window.connection_list.append(ungrouped_row)
        window._ungrouped_area_visible = True
    except Exception as e:
        logger.error(f"Error showing ungrouped area: {e}")


def _hide_ungrouped_area(window):
    try:
        if not window._ungrouped_area_visible or not window._ungrouped_area_row:
            return

        window.connection_list.remove(window._ungrouped_area_row)
        window._ungrouped_area_visible = False
    except Exception as e:
        logger.error(f"Error hiding ungrouped area: {e}")


def _clear_drop_indicator(window):
    try:
        if window._drop_indicator_row and hasattr(window._drop_indicator_row, 'hide_drop_indicators'):
            window._drop_indicator_row.hide_drop_indicators()

        window._drop_indicator_row = None
        window._drop_indicator_position = None
    except Exception as e:
        logger.error(f"Error clearing drop indicator: {e}")
        # Ensure cleanup even if there's an error
        window._drop_indicator_row = None
        window._drop_indicator_position = None


def _on_connection_list_drop(window, value, x, y):
    try:
        _clear_drop_indicator(window)
        _hide_ungrouped_area(window)
        _stop_connection_autoscroll(window)

        # Restore selection mode after drag
        if hasattr(window, '_drag_in_progress'):
            window._drag_in_progress = False
            window.connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)

        payload = _coerce_drag_value(value)

        if not isinstance(payload, dict):
            return False

        drop_type = payload.get("type")
        changes_made = False

        if drop_type == "connection":
            connection_nicknames: List[str] = []

            connection_payload = payload.get("connections")
            if isinstance(connection_payload, list):
                for item in connection_payload:
                    if isinstance(item, dict):
                        nickname = item.get("nickname")
                        if isinstance(nickname, str) and nickname not in connection_nicknames:
                            connection_nicknames.append(nickname)

            if not connection_nicknames:
                raw_list = payload.get("connection_nicknames")
                if isinstance(raw_list, list):
                    for nickname in raw_list:
                        if isinstance(nickname, str) and nickname not in connection_nicknames:
                            connection_nicknames.append(nickname)

            if not connection_nicknames:
                nickname = payload.get("connection_nickname")
                if isinstance(nickname, str):
                    connection_nicknames.append(nickname)

            if connection_nicknames:
                target_row = window.connection_list.get_row_at_y(int(y))

                if not target_row:
                    for nickname in connection_nicknames:
                        window.group_manager.move_connection(nickname, None)
                        changes_made = True
                elif getattr(target_row, "ungrouped_area", False):
                    for nickname in connection_nicknames:
                        window.group_manager.move_connection(nickname, None)
                        changes_made = True
                else:
                    row_y = target_row.get_allocation().y
                    row_height = target_row.get_allocation().height
                    relative_y = y - row_y
                    position = "above" if relative_y < row_height / 2 else "below"

                    if hasattr(target_row, "group_id"):
                        target_group_id = target_row.group_id

                        if position == "above":
                            first_connection = None
                            child = window.connection_list.get_first_child()
                            while child:
                                if hasattr(child, 'connection'):
                                    connection_group = window.group_manager.get_connection_group(child.connection.nickname)
                                    if connection_group == target_group_id:
                                        first_connection = child.connection.nickname
                                        break
                                child = child.get_next_sibling()

                            if first_connection:
                                for nickname in connection_nicknames:
                                    current_group_id = window.group_manager.get_connection_group(nickname)
                                    if current_group_id != target_group_id:
                                        window.group_manager.move_connection(nickname, target_group_id)
                                        changes_made = True
                                    window.group_manager.reorder_connection_in_group(
                                        nickname, first_connection, "above"
                                    )
                                    first_connection = nickname
                                    changes_made = True
                            else:
                                for nickname in connection_nicknames:
                                    if window.group_manager.get_connection_group(nickname) != target_group_id:
                                        window.group_manager.move_connection(nickname, target_group_id)
                                        changes_made = True
                        else:
                            for nickname in connection_nicknames:
                                if window.group_manager.get_connection_group(nickname) != target_group_id:
                                    window.group_manager.move_connection(nickname, target_group_id)
                                    changes_made = True
                    else:
                        target_connection = getattr(target_row, "connection", None)
                        if target_connection:
                            reference_nickname = target_connection.nickname
                            target_group_id = window.group_manager.get_connection_group(reference_nickname)

                            for nickname in connection_nicknames:
                                current_group_id = window.group_manager.get_connection_group(nickname)
                                if current_group_id != target_group_id:
                                    window.group_manager.move_connection(nickname, target_group_id)
                                    changes_made = True

                            if position == "above":
                                reference = reference_nickname
                                for nickname in reversed(connection_nicknames):
                                    if nickname == reference:
                                        continue
                                    window.group_manager.reorder_connection_in_group(
                                        nickname, reference, "above"
                                    )
                                    reference = nickname
                                    changes_made = True
                            else:
                                reference = reference_nickname
                                for nickname in connection_nicknames:
                                    if nickname == reference:
                                        continue
                                    window.group_manager.reorder_connection_in_group(
                                        nickname, reference, "below"
                                    )
                                    reference = nickname
                                    changes_made = True

        elif drop_type == "group":
            group_id = value.get("group_id")
            if group_id:
                target_row = window.connection_list.get_row_at_y(int(y))
                if target_row and hasattr(target_row, "group_id"):
                    target_group_id = target_row.group_id
                    if target_group_id != group_id:
                        # Validate that the target group exists
                        if target_group_id in window.group_manager.groups:
                            # Calculate position for reordering
                            row_y = target_row.get_allocation().y
                            row_height = target_row.get_allocation().height
                            relative_y = y - row_y
                            position = "above" if relative_y < row_height / 2 else "below"
                            
                            # Check if both groups are at the same level (can be reordered)
                            source_group = window.group_manager.groups.get(group_id)
                            target_group = window.group_manager.groups.get(target_group_id)
                            
                            if (source_group and target_group and 
                                source_group.get('parent_id') == target_group.get('parent_id')):
                                # Same level - reorder
                                window.group_manager.reorder_group(group_id, target_group_id, position)
                                changes_made = True
                            else:
                                # Different levels - move to new parent (existing functionality)
                                if _move_group(window, group_id, target_group_id):
                                    changes_made = True
                        else:
                            logger.warning(f"Target group '{target_group_id}' does not exist")

        # Only rebuild the connection list once if changes were made
        if changes_made:
            window.rebuild_connection_list()
            return True

        return False
    except Exception as e:
        logger.error(f"Error handling drop: {e}")
        return False


def _get_target_group_at_position(window, x, y):
    try:
        row = window.connection_list.get_row_at_y(int(y))
        if row and hasattr(row, "group_id"):
            return row.group_id
        elif row and hasattr(row, "connection"):
            connection = row.connection
            return window.group_manager.get_connection_group(connection.nickname)
        return None
    except Exception:
        return None


def _move_group(window, group_id, target_parent_id):
    try:
        if group_id not in window.group_manager.groups:
            return False

        # Prevent circular references
        if target_parent_id == group_id:
            logger.warning(f"Cannot move group '{group_id}' to itself")
            return False
        
        # Check if target_parent_id is a descendant of group_id (would create circular reference)
        current_parent = target_parent_id
        while current_parent:
            if current_parent == group_id:
                logger.warning(f"Cannot move group '{group_id}' to its descendant '{target_parent_id}'")
                return False
            current_parent = window.group_manager.groups.get(current_parent, {}).get('parent_id')

        group = window.group_manager.groups[group_id]
        old_parent_id = group.get("parent_id")

        # Remove from old parent's children
        if old_parent_id and old_parent_id in window.group_manager.groups:
            if group_id in window.group_manager.groups[old_parent_id]["children"]:
                window.group_manager.groups[old_parent_id]["children"].remove(group_id)

        # Update parent reference
        group["parent_id"] = target_parent_id
        
        # Add to new parent's children
        if target_parent_id and target_parent_id in window.group_manager.groups:
            if group_id not in window.group_manager.groups[target_parent_id]["children"]:
                window.group_manager.groups[target_parent_id]["children"].append(group_id)

        window.group_manager._save_groups()
        return True
    except Exception as e:
        logger.error(f"Error moving group: {e}")
        return False


def _update_connection_autoscroll(window, y):
    """Update autoscroll velocity based on pointer position within the viewport."""
    scrolled = getattr(window, "connection_scrolled", None)
    if not scrolled:
        _stop_connection_autoscroll(window)
        return

    allocation = scrolled.get_allocation()
    height = allocation.height
    if height <= 0:
        _stop_connection_autoscroll(window)
        return

    margin = max(1.0, min(getattr(window, "_connection_autoscroll_margin", 48.0), height / 2))
    max_velocity = max(1.0, getattr(window, "_connection_autoscroll_max_velocity", 28.0))

    vadjustment = scrolled.get_vadjustment()
    adjustment_value = vadjustment.get_value() if vadjustment else 0.0
    viewport_y = max(0.0, min(height, y - adjustment_value))

    top_threshold = margin
    bottom_threshold = height - margin

    velocity = 0.0
    if viewport_y < top_threshold:
        distance = top_threshold - viewport_y
        velocity = -_calculate_autoscroll_velocity(distance, margin, max_velocity)
    elif viewport_y > bottom_threshold:
        distance = viewport_y - bottom_threshold
        velocity = _calculate_autoscroll_velocity(distance, margin, max_velocity)

    if velocity:
        _start_connection_autoscroll(window, velocity)
    else:
        _stop_connection_autoscroll(window)


def _calculate_autoscroll_velocity(distance, margin, max_velocity):
    """Scale the autoscroll velocity based on how deep the pointer is in the margin."""
    ratio = min(1.0, max(0.0, distance) / margin)
    return max_velocity * ratio


def _start_connection_autoscroll(window, velocity):
    """Ensure an autoscroll timeout is active with the requested velocity."""
    window._connection_autoscroll_velocity = float(velocity)

    timeout_id = getattr(window, "_connection_autoscroll_timeout_id", 0)
    if timeout_id:
        return

    interval = max(10, int(getattr(window, "_connection_autoscroll_interval_ms", 16)))

    def _step():
        return _connection_autoscroll_step(window)

    window._connection_autoscroll_timeout_id = GLib.timeout_add(interval, _step)


def _stop_connection_autoscroll(window):
    """Cancel any active autoscroll timeout and reset state."""
    timeout_id = getattr(window, "_connection_autoscroll_timeout_id", 0)
    if timeout_id:
        GLib.source_remove(timeout_id)
    window._connection_autoscroll_timeout_id = 0
    window._connection_autoscroll_velocity = 0.0


def _connection_autoscroll_step(window):
    scrolled = getattr(window, "connection_scrolled", None)
    if not scrolled:
        window._connection_autoscroll_timeout_id = 0
        window._connection_autoscroll_velocity = 0.0
        return False

    velocity = getattr(window, "_connection_autoscroll_velocity", 0.0)
    if not velocity:
        window._connection_autoscroll_timeout_id = 0
        return False

    adjustment = scrolled.get_vadjustment()
    if not adjustment:
        window._connection_autoscroll_timeout_id = 0
        window._connection_autoscroll_velocity = 0.0
        return False

    lower = adjustment.get_lower()
    upper = adjustment.get_upper() - adjustment.get_page_size()
    current = adjustment.get_value()

    if upper < lower:
        upper = lower

    new_value = max(lower, min(upper, current + velocity))

    if new_value != current:
        adjustment.set_value(new_value)

    # Keep the timeout running as long as velocity remains set
    if getattr(window, "_connection_autoscroll_velocity", 0.0):
        return True

    window._connection_autoscroll_timeout_id = 0
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_sidebar(window):
    """Set up sidebar behaviour for ``window``."""

    setup_connection_list_dnd(window)
    return window.connection_list


__all__ = ["GroupRow", "ConnectionRow", "build_sidebar"]
