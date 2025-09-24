"""Sidebar components and drag-and-drop helpers for sshPilot."""

from __future__ import annotations

import logging
from typing import Dict

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gettext import gettext as _

from gi.repository import Gdk, GLib, GObject, Graphene, Gtk

from .connection_manager import Connection
from .connection_display import (
    get_connection_alias as _get_connection_alias,
    get_connection_host as _get_connection_host,
    format_connection_host_display as _format_connection_host_display,
)
from .groups import GroupManager

logger = logging.getLogger(__name__)


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
        self.add_css_class("navigation-sidebar")
        self.group_info = group_info
        self.group_manager = group_manager
        self.group_id = group_info["id"]
        self.connections_dict = connections_dict or {}

        # Color styling is now handled by the group container

        # Main container with drop indicators
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Drop indicator (top)
        self.drop_indicator_top = DragIndicator()
        main_box.append(self.drop_indicator_top)

        # Main content
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        content.set_margin_start(8)
        content.set_margin_end(8)
        content.set_margin_top(4)
        content.set_margin_bottom(4)

        icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        content.append(icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)

        self.name_label = Gtk.Label()
        self.name_label.set_halign(Gtk.Align.START)
        info_box.append(self.name_label)

        self.count_label = Gtk.Label()
        self.count_label.set_halign(Gtk.Align.START)
        self.count_label.add_css_class("dim-label")
        info_box.append(self.count_label)

        content.append(info_box)

        self.expand_button = Gtk.Button()
        self.expand_button.set_icon_name("pan-end-symbolic")
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

        drop_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
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

    def _lighten_color(self, hex_color, factor=0.3):
        """Lighten a hex color by mixing it with white"""
        try:
            if hex_color.startswith("#"):
                hex_color = hex_color[1:]
            
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            
            # Mix with white (255, 255, 255) using the factor
            # factor=0.0 means original color, factor=1.0 means pure white
            r = int(r + (255 - r) * factor)
            g = int(g + (255 - g) * factor)
            b = int(b + (255 - b) * factor)
            
            return f"#{r:02x}{g:02x}{b:02x}"
        except (ValueError, IndexError):
            return hex_color

    def _get_effective_color(self):
        """Get the effective color for this group (own color or inherited from parent)"""
        # First check if this group has its own color
        own_color = self.group_info.get("color")
        if own_color:
            return own_color
        
        # If no own color, check parent's color
        parent_id = self.group_info.get("parent_id")
        if parent_id and parent_id in self.group_manager.groups:
            parent_group = self.group_manager.groups[parent_id]
            parent_color = parent_group.get("color")
            if parent_color:
                # Return a lighter version of parent's color
                return self._lighten_color(parent_color, factor=0.4)
        
        return None

    def _apply_group_color(self):
        """Apply group color as background for the group container"""
        color = self._get_effective_color()
        if color:
            try:
                # Parse hex color and apply as CSS
                if color.startswith("#"):
                    # Create a CSS provider for this specific group
                    provider = Gtk.CssProvider()
                    css = f"""
                    .group-container-{self.group_id} {{
                        background-color: alpha({color}, 0.12);
                        border: 2px solid {color};
                        border-radius: 8px;
                        margin: 4px 8px;
                    }}
                    .group-container-{self.group_id} .group-header {{
                        background-color: alpha({color}, 0.20);
                        border-radius: 6px 6px 0 0;
                        border-bottom: 1px solid {color};
                    }}
                    .group-container-{self.group_id} .group-header:hover {{
                        background-color: alpha({color}, 0.30);
                    }}
                    .group-container-{self.group_id} .group-header:selected {{
                        background-color: alpha({color}, 0.40);
                    }}
                    .group-container-{self.group_id} .group-connections {{
                        background-color: transparent;
                    }}
                    .group-container-{self.group_id} .group-connections > * {{
                        background-color: transparent;
                        border: none;
                    }}
                    .group-container-{self.group_id} .group-connections > *:hover {{
                        background-color: alpha({color}, 0.15);
                    }}
                    .group-container-{self.group_id} .group-connections > *:selected {{
                        background-color: alpha({color}, 0.25);
                    }}
                    """
                    provider.load_from_data(css.encode("utf-8"))

                    # Add CSS class and provider
                    self.add_css_class(f"group-container-{self.group_id}")
                    
                    # Try adding provider to the widget's style context first
                    style_context = self.get_style_context()
                    style_context.add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                    
                    # Also add to display for broader coverage
                    Gtk.StyleContext.add_provider_for_display(
                        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
            except (ValueError, IndexError) as e:
                logger.error(f"Invalid color format for group {self.group_info.get('name', 'unknown')}: {color}, error: {e}")

    def _update_display(self):
        if self.group_info.get("expanded", True):
            self.expand_button.set_icon_name("pan-down-symbolic")
        else:
            self.expand_button.set_icon_name("pan-end-symbolic")

        actual_connections = [c for c in self.group_info.get("connections", []) if c in self.connections_dict]
        count = len(actual_connections)
        group_name = self.group_info["name"]
        self.name_label.set_markup(f"<b>{group_name}</b>")
        self.count_label.set_text(f"{count} connections")

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
        data = {"type": "group", "group_id": self.group_id}
        return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_PYOBJECT, data))

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

    def update_group_info(self, group_info: Dict):
        """Update group information and refresh display"""
        self.group_info = group_info
        self._apply_group_color()
        self._update_display()

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

    def __init__(self, connection: Connection, group_manager=None, group_info=None):
        super().__init__()
        self.add_css_class("navigation-sidebar")
        self.connection = connection
        self.group_manager = group_manager
        self.group_info = group_info

        # Main container with drop indicators
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Drop indicator (top)
        self.drop_indicator_top = DragIndicator()
        main_box.append(self.drop_indicator_top)

        # Content container
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        content.set_margin_start(8)
        content.set_margin_end(8)
        content.set_margin_top(4)
        content.set_margin_bottom(4)

        icon = Gtk.Image.new_from_icon_name("computer-symbolic")
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        content.append(icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)

        self.nickname_label = Gtk.Label()
        self.nickname_label.set_markup(f"<b>{connection.nickname}</b>")
        self.nickname_label.set_halign(Gtk.Align.START)
        info_box.append(self.nickname_label)

        self.host_label = Gtk.Label()
        self.host_label.set_halign(Gtk.Align.START)
        self.host_label.add_css_class("dim-label")
        self._apply_host_label_text()
        info_box.append(self.host_label)

        content.append(info_box)

        self.indicator_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.indicator_box.set_halign(Gtk.Align.CENTER)
        self.indicator_box.set_valign(Gtk.Align.CENTER)
        content.append(self.indicator_box)

        self.status_icon = Gtk.Image.new_from_icon_name("network-offline-symbolic")
        self.status_icon.set_pixel_size(16)
        content.append(self.status_icon)

        # Now add the content to main_box
        main_box.append(content)

        # Drop indicator (bottom)
        self.drop_indicator_bottom = DragIndicator()
        main_box.append(self.drop_indicator_bottom)

        # Add pulse overlay to the main box
        self._pulse = Gtk.Box()
        self._pulse.add_css_class("pulse-highlight")
        self._pulse.set_can_target(False)
        self._pulse.set_hexpand(True)
        self._pulse.set_vexpand(True)

        # Create overlay for pulse effect
        overlay = Gtk.Overlay()
        overlay.set_child(main_box)
        overlay.add_overlay(self._pulse)
        self.set_child(overlay)

        self.set_selectable(True)

        # Apply group color if available
        self._apply_group_color()

        self.update_status()
        self._update_forwarding_indicators()
        self._setup_drag_source()

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
        if level > 0:
            # Find the content box and set its margin
            overlay = self.get_child()
            if overlay and hasattr(overlay, "get_child"):
                main_box = overlay.get_child()
                if main_box and hasattr(main_box, "get_first_child"):
                    # Skip the first child (top drop indicator) and get the content box
                    top_indicator = main_box.get_first_child()
                    if top_indicator and hasattr(top_indicator, "get_next_sibling"):
                        content = top_indicator.get_next_sibling()
                        if content:
                            content.set_margin_start(12 + (level * 20))

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
        data = {"type": "connection", "connection_nickname": self.connection.nickname}
        return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_PYOBJECT, data))

    def _on_drag_begin(self, source, drag):
        try:
            window = self.get_root()
            if window:
                # Track which connection is being dragged
                window._dragged_connection = self.connection
                _show_ungrouped_area(window)
        except Exception as e:
            logger.error(f"Error in drag begin: {e}")

    def _on_drag_end(self, source, drag, delete_data):
        try:
            window = self.get_root()
            if window:
                # Clear the dragged connection tracking
                if hasattr(window, "_dragged_connection"):
                    delattr(window, "_dragged_connection")
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
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
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

        rules = getattr(self.connection, "forwarding_rules", []) or []
        has_local = any(r.get("enabled", True) and r.get("type") == "local" for r in rules)
        has_remote = any(r.get("enabled", True) and r.get("type") == "remote" for r in rules)
        has_dynamic = any(r.get("enabled", True) and r.get("type") == "dynamic" for r in rules)

        def make_badge(letter: str, cls: str):
            circled_map = {"L": "\u24c1", "R": "\u24c7", "D": "\u24b9"}
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

    def _apply_host_label_text(self):
        try:
            window = self.get_root()
            hide = bool(getattr(window, "_hide_hosts", False)) if window else False
        except Exception:
            hide = False
        if hide:
            self.host_label.set_text("••••••••••")
        else:
            host_value = getattr(
                self.connection, "hostname", getattr(self.connection, "host", getattr(self.connection, "nickname", ""))
            )
            self.host_label.set_text(f"{self.connection.username}@{host_value}")


    def apply_hide_hosts(self, hide: bool):
        self._apply_host_label_text()

    def update_status(self):
        try:
            window = self.get_root()
            has_active_terminal = False

            if hasattr(window, "connection_to_terminals") and self.connection in getattr(
                window, "connection_to_terminals", {}
            ):
                for t in window.connection_to_terminals.get(self.connection, []) or []:
                    if getattr(t, "is_connected", False):
                        has_active_terminal = True
                        break
            elif hasattr(window, "active_terminals") and self.connection in window.active_terminals:
                terminal = window.active_terminals[self.connection]
                if terminal and hasattr(terminal, "is_connected"):
                    has_active_terminal = terminal.is_connected

            self.connection.is_connected = has_active_terminal

            if has_active_terminal:
                self.status_icon.set_from_icon_name("network-idle-symbolic")
                host_value = getattr(
                    self.connection,
                    "hostname",
                    getattr(self.connection, "host", getattr(self.connection, "nickname", "")),

                )
                self.status_icon.set_tooltip_text(f"Connected to {host_value}")
            else:
                self.status_icon.set_from_icon_name("network-offline-symbolic")
                self.status_icon.set_tooltip_text("Disconnected")

            self.status_icon.queue_draw()
        except Exception as e:
            logger.error(f"Error updating status for {getattr(self.connection, 'nickname', 'connection')}: {e}")

    def update_display(self):
        if hasattr(self.connection, "nickname") and hasattr(self, "nickname_label"):
            self.nickname_label.set_markup(f"<b>{self.connection.nickname}</b>")

        if hasattr(self.connection, "username") and hasattr(self, "host_label"):
            host_value = getattr(
                self.connection, "hostname", getattr(self.connection, "host", getattr(self.connection, "nickname", ""))
            )
            port_text = f":{self.connection.port}" if getattr(self.connection, "port", 22) != 22 else ""
            self.host_label.set_text(f"{self.connection.username}@{host_value}{port_text}")

        self._update_forwarding_indicators()
        self.update_status()

    def _lighten_color(self, hex_color, factor=0.3):
        """Lighten a hex color by mixing it with white"""
        try:
            if hex_color.startswith("#"):
                hex_color = hex_color[1:]
            
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            
            # Mix with white (255, 255, 255) using the factor
            # factor=0.0 means original color, factor=1.0 means pure white
            r = int(r + (255 - r) * factor)
            g = int(g + (255 - g) * factor)
            b = int(b + (255 - b) * factor)
            
            return f"#{r:02x}{g:02x}{b:02x}"
        except (ValueError, IndexError):
            return hex_color

    def _get_effective_group_color(self):
        """Get the effective color for this connection's group"""
        if not self.group_info or not self.group_manager:
            return None
        
        # First check if this group has its own color
        own_color = self.group_info.get("color")
        if own_color:
            return own_color
        
        # If no own color, check parent's color
        parent_id = self.group_info.get("parent_id")
        if parent_id and parent_id in self.group_manager.groups:
            parent_group = self.group_manager.groups[parent_id]
            parent_color = parent_group.get("color")
            if parent_color:
                # Return a lighter version of parent's color
                return self._lighten_color(parent_color, factor=0.4)
        
        return None

    def _apply_group_color(self):
        """Apply group color styling for connection rows (now handled by group container)"""
        # Individual connection styling is now handled by the group container
        # This method is kept for compatibility but does nothing
        pass


# ---------------------------------------------------------------------------
# Drag-and-drop helpers
# ---------------------------------------------------------------------------


def setup_connection_list_dnd(window):
    """Set up drag and drop for the window's connection list."""

    drop_target = Gtk.DropTarget.new(type=GObject.TYPE_PYOBJECT, actions=Gdk.DragAction.MOVE)
    drop_target.connect("drop", lambda t, v, x, y: _on_connection_list_drop(window, t, v, x, y))
    drop_target.connect("motion", lambda t, x, y: _on_connection_list_motion(window, t, x, y))
    drop_target.connect("leave", lambda t: _on_connection_list_leave(window, t))
    window.connection_list.add_controller(drop_target)

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


def _on_connection_list_motion(window, target, x, y):
    try:
        # Prevent row selection during drag by temporarily disabling selection
        if not hasattr(window, "_drag_in_progress"):
            window._drag_in_progress = True
            window.connection_list.set_selection_mode(Gtk.SelectionMode.NONE)

        # Throttle motion events to improve performance
        current_time = GLib.get_monotonic_time()
        if hasattr(window, "_last_motion_time"):
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
            if hasattr(row, "connection") and hasattr(window, "_dragged_connection"):
                # Don't show indicators on the row being dragged
                if row.connection == window._dragged_connection:
                    _clear_drop_indicator(window)
                    return Gdk.DragAction.MOVE

                # Only show indicator if this is a different connection
                _show_drop_indicator(window, row, position)

            # Handle group rows
            elif hasattr(row, "group_id") and hasattr(window, "_dragged_group_id"):
                # Don't show indicators on the group being dragged
                if row.group_id == window._dragged_group_id:
                    _clear_drop_indicator(window)
                    return Gdk.DragAction.MOVE

                # Only show indicator if this is a different group
                _show_drop_indicator(window, row, position)

            # Handle mixed drag scenarios (dragging connection over group, etc.)
            elif hasattr(row, "connection") and hasattr(window, "_dragged_group_id"):
                # Dragging group over connection - show indicator
                _show_drop_indicator(window, row, position)
            elif hasattr(row, "group_id") and hasattr(window, "_dragged_connection"):
                # Dragging connection over group - only show indicator on the group itself (not above/below)
                # This indicates the connection will be added to the group
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


def _on_connection_list_leave(window, target):
    _clear_drop_indicator(window)
    _hide_ungrouped_area(window)

    _stop_connection_autoscroll(window)

    # Restore selection mode after drag
    if hasattr(window, "_drag_in_progress"):
        window._drag_in_progress = False
        window.connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)

    return True


def _show_drop_indicator(window, row, position):
    try:
        # Only update if the indicator has changed
        if window._drop_indicator_row != row or window._drop_indicator_position != position:
            # Clear any existing indicators
            if window._drop_indicator_row and hasattr(window._drop_indicator_row, "hide_drop_indicators"):
                window._drop_indicator_row.hide_drop_indicators()

            # Show indicator on the target row
            if hasattr(row, "show_drop_indicator"):
                show_top = position == "above"
                row.show_drop_indicator(show_top)

            window._drop_indicator_row = row
            window._drop_indicator_position = position
    except Exception as e:
        logger.error(f"Error showing drop indicator: {e}")


def _show_drop_indicator_on_group(window, row):
    """Show a special indicator when dropping a connection onto a group (adds to group)"""
    try:
        # Only update if the indicator has changed
        if window._drop_indicator_row != row or window._drop_indicator_position != "on_group":
            # Clear any existing indicators
            if window._drop_indicator_row and hasattr(window._drop_indicator_row, "hide_drop_indicators"):
                window._drop_indicator_row.hide_drop_indicators()

            # Show group highlight indicator instead of line indicators
            if hasattr(row, "show_group_highlight"):
                row.show_group_highlight(True)
            elif hasattr(row, "show_drop_indicator"):
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

    icon = Gtk.Image.new_from_icon_name("folder-open-symbolic")
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
        if window._drop_indicator_row and hasattr(window._drop_indicator_row, "hide_drop_indicators"):
            window._drop_indicator_row.hide_drop_indicators()

        window._drop_indicator_row = None
        window._drop_indicator_position = None
    except Exception as e:
        logger.error(f"Error clearing drop indicator: {e}")
        # Ensure cleanup even if there's an error
        window._drop_indicator_row = None
        window._drop_indicator_position = None


def _on_connection_list_drop(window, target, value, x, y):
    try:
        _clear_drop_indicator(window)
        _hide_ungrouped_area(window)


        # Restore selection mode after drag
        if hasattr(window, "_drag_in_progress"):
            window._drag_in_progress = False
            window.connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)

        # Extract Python object from GObject.Value drops
        if isinstance(value, GObject.Value):
            extracted = None
            for getter in ("get_boxed", "get_object", "get"):
                try:
                    extracted = getattr(value, getter)()
                    if extracted is not None:
                        break
                except Exception:
                    continue
            value = extracted

        if not isinstance(value, dict):
            return False

        drop_type = value.get("type")
        changes_made = False

        if drop_type == "connection":
            connection_nickname = value.get("connection_nickname")
            if connection_nickname:
                current_group_id = window.group_manager.get_connection_group(connection_nickname)

                target_row = window.connection_list.get_row_at_y(int(y))
                if not target_row:
                    # Drop on empty space - ungroup the connection
                    window.group_manager.move_connection(connection_nickname, None)
                    changes_made = True
                elif getattr(target_row, "ungrouped_area", False):
                    # Drop on ungrouped area
                    window.group_manager.move_connection(connection_nickname, None)
                    changes_made = True
                else:
                    row_y = target_row.get_allocation().y
                    row_height = target_row.get_allocation().height
                    relative_y = y - row_y
                    position = "above" if relative_y < row_height / 2 else "below"

                    if hasattr(target_row, "group_id"):
                        # Drop on group row - add to the group
                        target_group_id = target_row.group_id
                        if target_group_id != current_group_id:
                            window.group_manager.move_connection(connection_nickname, target_group_id)
                            changes_made = True
                    else:
                        # Drop on connection row
                        target_connection = getattr(target_row, "connection", None)
                        if target_connection:
                            target_group_id = window.group_manager.get_connection_group(target_connection.nickname)
                            if target_group_id != current_group_id:
                                # Moving to a different group - move first, then reorder
                                window.group_manager.move_connection(connection_nickname, target_group_id)
                                # Now reorder within the new group
                                window.group_manager.reorder_connection_in_group(
                                    connection_nickname, target_connection.nickname, position
                                )
                                changes_made = True
                            else:
                                # Same group - just reorder
                                window.group_manager.reorder_connection_in_group(
                                    connection_nickname, target_connection.nickname, position
                                )
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

                            if (
                                source_group
                                and target_group
                                and source_group.get("parent_id") == target_group.get("parent_id")
                            ):
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
            current_parent = window.group_manager.groups.get(current_parent, {}).get("parent_id")

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

    top_threshold = margin
    bottom_threshold = height - margin

    velocity = 0.0
    if y < top_threshold:
        distance = top_threshold - y
        velocity = -_calculate_autoscroll_velocity(distance, margin, max_velocity)
    elif y > bottom_threshold:
        distance = y - bottom_threshold
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
