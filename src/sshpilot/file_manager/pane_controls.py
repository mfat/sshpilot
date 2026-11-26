"""Pane-level UI controls for the file manager (path entry, nav buttons, toolbar)."""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from .icon_levels import _DEFAULT_LIST_LEVEL, _MAX_LIST_LEVEL, _MIN_ICON_LEVEL


class PathEntry(Gtk.Entry):
    """Simple entry used for the editable pathbar."""

    def __init__(self) -> None:
        super().__init__()
        # Don't set hexpand here - we'll set it explicitly in the toolbar
        # self.set_hexpand(True)
        self.set_placeholder_text("/remote/path")
        # Remove minimum width constraint to allow full expansion
        # self.set_size_request(200, -1)  # Commented out to allow full width


class PaneControls(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_valign(Gtk.Align.CENTER)
        from sshpilot import icon_utils
        self.back_button = icon_utils.new_button_from_icon_name("go-previous-symbolic")
        self.back_button.set_tooltip_text(_("Back"))
        self.forward_button = icon_utils.new_button_from_icon_name("go-next-symbolic")
        self.forward_button.set_tooltip_text(_("Forward"))
        self.up_button = icon_utils.new_button_from_icon_name("go-up-symbolic")
        self.refresh_button = icon_utils.new_button_from_icon_name("view-refresh-symbolic")
        self.new_folder_button = icon_utils.new_button_from_icon_name("folder-new-symbolic")
        for widget in (
            self.back_button,
            self.forward_button,
            self.up_button,
            self.refresh_button,
            self.new_folder_button,
        ):
            widget.set_valign(Gtk.Align.CENTER)
        for widget in (
            self.back_button,
            self.forward_button,
            self.up_button,
            self.refresh_button,
            self.new_folder_button,
        ):
            widget.add_css_class("flat")
        # Only back/forward/up live here (left of the address bar); refresh
        # and new_folder are packed right of the entry by PaneToolbar.
        self.append(self.back_button)
        self.append(self.forward_button)
        self.append(self.up_button)


class PaneToolbar(Gtk.Box):
    __gsignals__ = {
        "view-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "zoom-changed": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        # Build a custom top bar: WindowHandle -> Box [ left | ENTRY (expands) | right ]
        handle = Gtk.WindowHandle()                    # gives draggable area like a headerbar
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        handle.set_child(bar)

        # Left side (compact)
        self._pane_label = Gtk.Label()
        self._pane_label.set_css_classes(["title"])
        self.controls = PaneControls()
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        left.set_margin_start(12)  # Add margin before Remote/Local labels
        left.append(self._pane_label)
        left.append(self.controls)
        bar.append(left)

        # Entry (fills all remaining space)
        self.path_entry = PathEntry()
        self.path_entry.set_hexpand(True)
        self.path_entry.set_halign(Gtk.Align.FILL)
        self.path_entry.set_width_chars(0)
        self.path_entry.set_max_width_chars(0)
        bar.append(self.path_entry)

        # Right side (compact, flush-right): reload immediately after the
        # address bar, then new folder, then the view controls.
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        right.append(self.controls.refresh_button)
        right.append(self.controls.new_folder_button)
        self._current_view = "list"
        # Show Hidden Files lives in the view menu below (pane.show-hidden).

        self.sort_split_button = self._create_sort_split_button()
        right.append(self.sort_split_button)
        bar.append(right)

        # Don't wrap in ToolbarView to avoid nested ToolbarView theme issues
        # Instead, add toolbar styling via CSS class and append handle directly.
        # The toolbar is the top row of the pane's card (FilePane frames the
        # toolbar and the file list together), so it draws no card of its own.
        handle.add_css_class('toolbar')
        self.append(handle)

    # Keep your factory
    def _create_sort_split_button(self) -> Adw.SplitButton:
        menu_model = Gio.Menu()

        # Custom widget slot: the icon-size slider lives at the top of the
        # dropdown. The "custom" attribute names a slot that PopoverMenu
        # fills with whatever widget we add_child() under the same id.
        size_section = Gio.Menu()
        zoom_item = Gio.MenuItem.new(_("Icon Size"), None)
        zoom_item.set_attribute_value("custom", GLib.Variant.new_string("zoom_slider"))
        size_section.append_item(zoom_item)
        menu_model.append_section(None, size_section)

        # Ctrl+H (Shift+Cmd+. on macOS) toggles it too; the accel attribute
        # only labels the item.
        from ..platform_utils import is_macos
        hidden_section = Gio.Menu()
        hidden_item = Gio.MenuItem.new(_("Show Hidden Files"), "pane.show-hidden")
        hidden_item.set_attribute_value(
            "accel",
            GLib.Variant.new_string("<Shift><Primary>period" if is_macos() else "<Primary>h"),
        )
        hidden_section.append_item(hidden_item)
        menu_model.append_section(None, hidden_section)

        sort_section = Gio.Menu()
        sort_section.append(_("Name"), "pane.sort-by-name")
        sort_section.append(_("Size"), "pane.sort-by-size")
        sort_section.append(_("Modified"), "pane.sort-by-modified")
        sort_section.append(_("Type"), "pane.sort-by-type")
        menu_model.append_section(_("Sort by"), sort_section)
        direction_section = Gio.Menu()
        direction_section.append(_("Ascending"), "pane.sort-direction-asc")
        direction_section.append(_("Descending"), "pane.sort-direction-desc")
        menu_model.append_section(_("Order"), direction_section)

        # Build a PopoverMenu from the model so we can inject the slider widget
        # into the named custom slot.
        popover = Gtk.PopoverMenu.new_from_model(menu_model)
        popover.add_child(self._build_zoom_slider_widget(), "zoom_slider")

        split_button = Adw.SplitButton()
        split_button.set_popover(popover)
        # Tooltip text is parsed as Pango markup; escape the ampersand or
        # use a plain word to avoid "entity did not end with a semicolon".
        split_button.set_dropdown_tooltip(_("View options"))
        # Icon/tooltip show the destination layout (Nautilus), not the current one.
        icon_name, tooltip = self.view_toggle_appearance(self._current_view)
        split_button.set_icon_name(icon_name)
        split_button.set_tooltip_text(tooltip)
        split_button.connect("clicked", self._on_view_toggle_clicked)
        return split_button

    def _build_zoom_slider_widget(self) -> Gtk.Widget:
        """Build the icon-size slider that sits inside the view-toggle dropdown."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(2)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_size_request(220, -1)

        header = Gtk.Label(label=_("Icon Size"), xalign=0)
        header.add_css_class("caption-heading")
        header.add_css_class("dim-label")
        box.append(header)

        # Starts on the list view's range; set_zoom_level() re-ranges it
        # when the pane switches views, since each view zooms separately.
        scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            float(_MIN_ICON_LEVEL),
            float(_MAX_LIST_LEVEL),
            1.0,
        )
        scale.set_digits(0)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_round_digits(0)
        self._zoom_max_level = _MAX_LIST_LEVEL
        self._add_zoom_marks(scale, _MAX_LIST_LEVEL)
        scale.set_value(float(_DEFAULT_LIST_LEVEL))
        self._zoom_scale = scale
        self._zoom_scale_handler_id = scale.connect(
            "value-changed", self._on_zoom_scale_changed
        )
        box.append(scale)
        return box

    @staticmethod
    def _add_zoom_marks(scale: Gtk.Scale, max_level: int) -> None:
        scale.clear_marks()
        for lvl in range(_MIN_ICON_LEVEL, max_level + 1):
            scale.add_mark(float(lvl), Gtk.PositionType.BOTTOM, None)

    def _on_zoom_scale_changed(self, scale: Gtk.Scale) -> None:
        level = int(round(scale.get_value()))
        level = max(_MIN_ICON_LEVEL, min(self._zoom_max_level, level))
        self.emit("zoom-changed", level)

    def set_zoom_level(self, level: int, max_level: int) -> None:
        """Show *level* out of *max_level* on the slider without firing zoom-changed."""
        scale = getattr(self, "_zoom_scale", None)
        if scale is None:
            return
        clamped = float(max(_MIN_ICON_LEVEL, min(max_level, level)))
        handler_id = getattr(self, "_zoom_scale_handler_id", None)
        if handler_id is not None:
            scale.handler_block(handler_id)
        try:
            if max_level != self._zoom_max_level:
                self._zoom_max_level = max_level
                scale.set_range(float(_MIN_ICON_LEVEL), float(max_level))
                self._add_zoom_marks(scale, max_level)
            if abs(scale.get_value() - clamped) >= 0.5:
                scale.set_value(clamped)
        finally:
            if handler_id is not None:
                scale.handler_unblock(handler_id)

    @staticmethod
    def view_toggle_appearance(current_view: str) -> tuple[str, str]:
        """Icon and tooltip for switching away from *current_view*.

        Matches Nautilus: the control advertises the other layout, not the
        one currently shown.
        """
        if current_view == "list":
            return "view-grid-symbolic", _("Grid View")
        return "view-list-symbolic", _("List View")

    def _sync_view_toggle_appearance(self) -> None:
        icon_name, tooltip = self.view_toggle_appearance(self._current_view)
        self.sort_split_button.set_icon_name(icon_name)
        self.sort_split_button.set_tooltip_text(tooltip)

    def _on_view_toggle_clicked(self, *_):
        self._current_view = "grid" if self._current_view == "list" else "list"
        self._sync_view_toggle_appearance()
        self.emit("view-changed", self._current_view)

    def get_header_bar(self):
        """Get the actual header bar for toolbar view."""
        return None  # No longer using Adw.HeaderBar


