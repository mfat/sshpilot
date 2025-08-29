"""Sidebar components and drag-and-drop helpers for sshPilot."""

from __future__ import annotations

import logging
from typing import Dict

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GObject, GLib, Adw

from gettext import gettext as _

from .connection_manager import Connection
from .groups import GroupManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Row widgets
# ---------------------------------------------------------------------------


class GroupRow(Gtk.ListBoxRow):
    """Row widget for group headers."""

    __gsignals__ = {
        "group-toggled": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
    }

    def __init__(self, group_info: Dict, group_manager: GroupManager, connections_dict: Dict | None = None):
        super().__init__()
        self.group_info = group_info
        self.group_manager = group_manager
        self.group_id = group_info["id"]
        self.connections_dict = connections_dict or {}

        # Main content
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(17)
        content.set_margin_bottom(17)

        icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        content.append(icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)

        self.name_label = Gtk.Label()
        self.name_label.set_halign(Gtk.Align.START)
        info_box.append(self.name_label)

        content.append(info_box)

        self.expand_button = Gtk.Button()
        self.expand_button.set_icon_name("pan-end-symbolic")
        self.expand_button.add_css_class("flat")
        self.expand_button.add_css_class("group-expand-button")
        self.expand_button.set_can_focus(False)
        self.expand_button.connect("clicked", self._on_expand_clicked)
        content.append(self.expand_button)

        self.set_child(content)
        self.set_selectable(True)
        self.set_can_focus(True)

        self._update_display()
        self._setup_drag_source()
        self._setup_double_click_gesture()

    # -- internal helpers -------------------------------------------------

    def _update_display(self):
        if self.group_info.get("expanded", True):
            self.expand_button.set_icon_name("pan-down-symbolic")
        else:
            self.expand_button.set_icon_name("pan-end-symbolic")

        actual_connections = [
            c
            for c in self.group_info.get("connections", [])
            if c in self.connections_dict
        ]
        count = len(actual_connections)
        group_name = self.group_info['name']
        self.name_label.set_markup(f"<b>{group_name} ({count})</b>")
        


    def _on_expand_clicked(self, button):
        self._toggle_expand()

    def _setup_drag_source(self):
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        self.add_controller(drag_source)

    def _on_drag_prepare(self, source, x, y):
        data = {"type": "group", "group_id": self.group_id}
        return Gdk.ContentProvider.new_for_value(
            GObject.Value(GObject.TYPE_PYOBJECT, data)
        )

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


class ConnectionRow(Gtk.ListBoxRow):
    """Row widget for connection list."""

    def __init__(self, connection: Connection):
        super().__init__()
        self.connection = connection

        overlay = Gtk.Overlay()
        self.set_child(overlay)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

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

        overlay.set_child(content)

        self._pulse = Gtk.Box()
        self._pulse.add_css_class("pulse-highlight")
        self._pulse.set_can_target(False)
        self._pulse.set_hexpand(True)
        self._pulse.set_vexpand(True)
        overlay.add_overlay(self._pulse)

        self.set_selectable(True)

        self.update_status()
        self._update_forwarding_indicators()
        self._setup_drag_source()

    # -- drag source ------------------------------------------------------

    def _setup_drag_source(self):
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        drag_source.connect("drag-end", self._on_drag_end)
        self.add_controller(drag_source)

    def _on_drag_prepare(self, source, x, y):
        data = {"type": "connection", "connection_nickname": self.connection.nickname}
        return Gdk.ContentProvider.new_for_value(
            GObject.Value(GObject.TYPE_PYOBJECT, data)
        )

    def _on_drag_begin(self, source, drag):
        try:
            window = self.get_root()
            if window:
                _show_ungrouped_area(window)
        except Exception as e:
            logger.error(f"Error in drag begin: {e}")

    def _on_drag_end(self, source, drag, delete_data):
        try:
            window = self.get_root()
            if window:
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

    def _apply_host_label_text(self):
        try:
            window = self.get_root()
            hide = bool(getattr(window, "_hide_hosts", False)) if window else False
        except Exception:
            hide = False
        if hide:
            self.host_label.set_text("••••••••••")
        else:
            self.host_label.set_text(f"{self.connection.username}@{self.connection.host}")

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

            if has_active_terminal:
                self.status_icon.set_from_icon_name("network-idle-symbolic")
                self.status_icon.set_tooltip_text(
                    f"Connected to {getattr(self.connection, 'hname', '') or self.connection.host}"
                )
            else:
                self.status_icon.set_from_icon_name("network-offline-symbolic")
                self.status_icon.set_tooltip_text("Disconnected")

            self.status_icon.queue_draw()
        except Exception as e:
            logger.error(
                f"Error updating status for {getattr(self.connection, 'nickname', 'connection')}: {e}"
            )

    def update_display(self):
        if hasattr(self.connection, "nickname") and hasattr(self, "nickname_label"):
            self.nickname_label.set_markup(f"<b>{self.connection.nickname}</b>")

        if hasattr(self.connection, "username") and hasattr(self.connection, "host") and hasattr(self, "host_label"):
            port_text = f":{self.connection.port}" if getattr(self.connection, "port", 22) != 22 else ""
            self.host_label.set_text(f"{self.connection.username}@{self.connection.host}{port_text}")
        self._update_forwarding_indicators()
        self.update_status()


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


def _on_connection_list_motion(window, target, x, y):
    try:
        _clear_drop_indicator(window)
        _show_ungrouped_area(window)

        row = window.connection_list.get_row_at_y(int(y))
        if not row:
            return Gdk.DragAction.MOVE

        if getattr(row, "ungrouped_area", False):
            row.add_css_class("drag-over")
            window._drop_indicator_row = row
            window._drop_indicator_position = "ungrouped"
            return Gdk.DragAction.MOVE

        row_y = row.get_allocation().y
        row_height = row.get_allocation().height
        relative_y = y - row_y
        position = "above" if relative_y < row_height / 2 else "below"

        _show_drop_indicator(window, row, position)
        return Gdk.DragAction.MOVE
    except Exception as e:
        logger.error(f"Error handling motion: {e}")
        return Gdk.DragAction.MOVE


def _on_connection_list_leave(window, target):
    _clear_drop_indicator(window)
    _hide_ungrouped_area(window)
    return True


def _show_drop_indicator(window, row, position):
    try:
        if position == "above":
            row.add_css_class("drop-above")
        else:
            row.add_css_class("drop-below")
        window._drop_indicator_row = row
        window._drop_indicator_position = position
    except Exception as e:
        logger.error(f"Error showing drop indicator: {e}")


def _create_ungrouped_area(window):
    if window._ungrouped_area_row:
        return window._ungrouped_area_row

    ungrouped_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    ungrouped_area.add_css_class("ungrouped-area")

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
        if window._drop_indicator_row:
            window._drop_indicator_row.remove_css_class("drop-above")
            window._drop_indicator_row.remove_css_class("drop-below")
            window._drop_indicator_row.remove_css_class("drag-over")
            window._drop_indicator_row = None
            window._drop_indicator_position = None
    except Exception as e:
        logger.error(f"Error clearing drop indicator: {e}")


def _on_connection_list_drop(window, target, value, x, y):
    try:
        _clear_drop_indicator(window)
        _hide_ungrouped_area(window)

        if not isinstance(value, dict):
            return False

        drop_type = value.get("type")
        if drop_type == "connection":
            connection_nickname = value.get("connection_nickname")
            if connection_nickname:
                current_group_id = window.group_manager.get_connection_group(connection_nickname)

                target_row = window.connection_list.get_row_at_y(int(y))
                if not target_row:
                    window.group_manager.move_connection(connection_nickname, None)
                    window.rebuild_connection_list()
                    return True

                if getattr(target_row, "ungrouped_area", False):
                    window.group_manager.move_connection(connection_nickname, None)
                    window.rebuild_connection_list()
                    return True

                row_y = target_row.get_allocation().y
                row_height = target_row.get_allocation().height
                relative_y = y - row_y
                position = "above" if relative_y < row_height / 2 else "below"

                if hasattr(target_row, "group_id"):
                    target_group_id = target_row.group_id
                    if target_group_id != current_group_id:
                        window.group_manager.move_connection(connection_nickname, target_group_id)
                        window.rebuild_connection_list()
                else:
                    target_connection = getattr(target_row, "connection", None)
                    if target_connection:
                        target_group_id = window.group_manager.get_connection_group(
                            target_connection.nickname
                        )
                        if target_group_id != current_group_id:
                            window.group_manager.move_connection(connection_nickname, target_group_id)
                            window.rebuild_connection_list()
                        else:
                            window.group_manager.reorder_connection_in_group(
                                connection_nickname, target_connection.nickname, position
                            )
                            window.rebuild_connection_list()

                return True

        elif drop_type == "group":
            group_id = value.get("group_id")
            if group_id:
                target_row = window.connection_list.get_row_at_y(int(y))
                if target_row and hasattr(target_row, "group_id"):
                    target_group_id = target_row.group_id
                    if target_group_id != group_id:
                        _move_group(window, group_id, target_group_id)
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
            return

        group = window.group_manager.groups[group_id]
        old_parent_id = group.get("parent_id")

        if old_parent_id and old_parent_id in window.group_manager.groups:
            if group_id in window.group_manager.groups[old_parent_id]["children"]:
                window.group_manager.groups[old_parent_id]["children"].remove(group_id)

        group["parent_id"] = target_parent_id
        if target_parent_id and target_parent_id in window.group_manager.groups:
            if group_id not in window.group_manager.groups[target_parent_id]["children"]:
                window.group_manager.groups[target_parent_id]["children"].append(group_id)

        window.group_manager._save_groups()
    except Exception as e:
        logger.error(f"Error moving group: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_sidebar(window):
    """Set up sidebar behaviour for ``window``."""

    setup_connection_list_dnd(window)
    return window.connection_list


__all__ = ["GroupRow", "ConnectionRow", "build_sidebar"]

