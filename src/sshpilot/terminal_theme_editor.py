"""Create, edit and delete custom terminal color schemes.

Shared by the headerbar theme picker and Settings. Themes are stored through
``Config.save_custom_theme`` / ``Config.remove_custom_theme``; the config's
``setting-changed`` signal then rebuilds the pickers and repaints terminals.
"""

from __future__ import annotations

import logging
from gettext import gettext as _
from typing import Any, Dict, List, Mapping, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo

from .custom_terminal_themes import PALETTE_SIZE, normalize_color

logger = logging.getLogger(__name__)

_PALETTE_NAMES = (
    _("Black"), _("Red"), _("Green"), _("Yellow"),
    _("Blue"), _("Magenta"), _("Cyan"), _("White"),
)

# (text, palette index or None for the foreground) per preview line segment.
_PREVIEW_LINES = (
    (("user@host", 2), (":", None), ("~/project", 4), ("$ ls", None)),
    (("build", 4), ("  ", None), ("run.sh", 2), ("  ", None), ("notes.txt", None)),
    (("error:", 9), (" file not found", None)),
    (("warning:", 11), (" disk ", None), ("85%", 3), (" full", None)),
)


def _rgba(color: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    if not rgba.parse(color):
        rgba.parse("#000000")
    return rgba


def _hex(rgba: Gdk.RGBA) -> str:
    return "#{:02X}{:02X}{:02X}".format(
        round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)
    )


def _palette_tooltip(index: int) -> str:
    name = _PALETTE_NAMES[index % 8]
    if index < 8:
        return name
    return _("Bright {color}").format(color=name)


class TerminalThemeEditor:
    """``Adw.Dialog`` that edits one theme and saves it as a custom theme."""

    def __init__(self, config, *, key: Optional[str] = None, base_key: Optional[str] = None):
        self.config = config
        self.key = key
        themes = getattr(config, "terminal_themes", {}) or {}
        if key is not None:
            initial = themes[key]
            title = _("Edit Color Scheme")
            name = str(initial.get("name") or "")
        else:
            initial = themes.get(base_key or "") or themes["default"]
            title = _("New Color Scheme")
            name = _("Custom Theme")
        self._initial: Mapping[str, Any] = initial

        self.dialog = Adw.Dialog()
        self.dialog.set_title(title)
        self.dialog.set_content_width(560)
        self.dialog.set_content_height(720)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_a: self.dialog.close())
        header.pack_start(cancel)
        self.save_button = Gtk.Button(label=_("Save"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        header.pack_end(self.save_button)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)
        self.dialog.set_child(toolbar)

        preview_group = Adw.PreferencesGroup()
        self.preview = Gtk.DrawingArea()
        self.preview.set_content_height(132)
        self.preview.set_hexpand(True)
        self.preview.set_draw_func(self._draw_preview)
        frame = Gtk.Frame()
        frame.set_child(self.preview)
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.add_css_class("card")
        preview_group.add(frame)
        page.add(preview_group)

        general = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow()
        self.name_row.set_title(_("Name"))
        self.name_row.set_text(name)
        self.name_row.connect("changed", lambda *_a: self._update_save_sensitivity())
        general.add(self.name_row)
        page.add(general)

        colors = Adw.PreferencesGroup(title=_("Colors"))
        self.background_button = self._color_row(
            colors, _("Background"), initial.get("background", "#000000")
        )
        self.foreground_button = self._color_row(
            colors, _("Foreground"), initial.get("foreground", "#FFFFFF")
        )
        self.cursor_button = self._color_row(
            colors,
            _("Cursor"),
            initial.get("cursor_color") or initial.get("foreground", "#FFFFFF"),
        )
        page.add(colors)

        palette_group = Adw.PreferencesGroup(title=_("Palette"))
        grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        grid.set_halign(Gtk.Align.CENTER)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        palette = list(initial.get("palette") or [])
        palette += ["#000000"] * (PALETTE_SIZE - len(palette))
        self.palette_buttons: List[Gtk.ColorDialogButton] = []
        for row, row_label in enumerate((_("Normal"), _("Bright"))):
            label = Gtk.Label(label=row_label)
            label.set_xalign(0)
            label.add_css_class("dim-label")
            label.set_margin_end(6)
            grid.attach(label, 0, row, 1, 1)
            for column in range(8):
                index = row * 8 + column
                button = self._color_button(palette[index])
                button.set_tooltip_text(_palette_tooltip(index))
                button.update_property(
                    [Gtk.AccessibleProperty.LABEL], [_palette_tooltip(index)]
                )
                grid.attach(button, column + 1, row, 1, 1)
                self.palette_buttons.append(button)
        palette_box = Gtk.Box()
        palette_box.add_css_class("card")
        palette_box.append(grid)
        grid.set_hexpand(True)
        palette_group.add(palette_box)
        page.add(palette_group)

        self._update_save_sensitivity()

    def _color_button(self, color: str) -> Gtk.ColorDialogButton:
        dialog = Gtk.ColorDialog()
        dialog.set_with_alpha(False)
        button = Gtk.ColorDialogButton.new(dialog)
        button.set_rgba(_rgba(str(color)))
        button.set_valign(Gtk.Align.CENTER)
        button.connect("notify::rgba", lambda *_a: self.preview.queue_draw())
        return button

    def _color_row(
        self, group: Adw.PreferencesGroup, title: str, color: str
    ) -> Gtk.ColorDialogButton:
        row = Adw.ActionRow(title=title)
        button = self._color_button(color)
        button.update_property([Gtk.AccessibleProperty.LABEL], [title])
        row.add_suffix(button)
        row.set_activatable_widget(button)
        group.add(row)
        return button

    def theme_data(self) -> Dict[str, Any]:
        """The theme as currently edited, in the stored shape."""
        return {
            "name": self.name_row.get_text().strip(),
            "foreground": _hex(self.foreground_button.get_rgba()),
            "background": _hex(self.background_button.get_rgba()),
            "cursor_color": _hex(self.cursor_button.get_rgba()),
            "highlight_background": normalize_color(
                self._initial.get("highlight_background")
            ) or "#4A90E2",
            "highlight_foreground": normalize_color(
                self._initial.get("highlight_foreground")
            ) or "#FFFFFF",
            "palette": [_hex(button.get_rgba()) for button in self.palette_buttons],
        }

    def _update_save_sensitivity(self) -> None:
        self.save_button.set_sensitive(bool(self.name_row.get_text().strip()))

    def _draw_preview(self, _area, cr, width: int, height: int) -> None:
        def set_color(rgba: Gdk.RGBA) -> None:
            cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1.0)

        set_color(self.background_button.get_rgba())
        cr.rectangle(0, 0, width, height)
        cr.fill()

        foreground = self.foreground_button.get_rgba()
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string("Monospace 10"))
        y = 12.0
        for segments in _PREVIEW_LINES:
            x = 14.0
            for text, index in segments:
                rgba = (
                    foreground if index is None
                    else self.palette_buttons[index].get_rgba()
                )
                set_color(rgba)
                layout.set_text(text, -1)
                cr.move_to(x, y)
                PangoCairo.show_layout(cr, layout)
                x += layout.get_pixel_size()[0]
            # Draw the cursor after the prompt line.
            if segments is _PREVIEW_LINES[0]:
                set_color(self.cursor_button.get_rgba())
                cr.rectangle(x + 6, y + 1, 8, layout.get_pixel_size()[1] - 2)
                cr.fill()
            y += layout.get_pixel_size()[1] + 4

        swatch_width = max(1.0, (width - 28 - 15 * 4) / 16)
        for index, button in enumerate(self.palette_buttons):
            set_color(button.get_rgba())
            cr.rectangle(14 + index * (swatch_width + 4), height - 24, swatch_width, 12)
            cr.fill()

    def _on_save(self, _button) -> None:
        key = self.config.save_custom_theme(self.theme_data(), key=self.key)
        if key is None:
            logger.error("Custom terminal theme could not be saved")
            return
        self.config.set_setting("terminal.theme", key)
        self.dialog.close()

    def present(self, parent: Gtk.Widget) -> None:
        self.dialog.present(parent)
        self.name_row.grab_focus()


def edit_custom_theme(
    parent: Gtk.Widget, config, key: Optional[str] = None
) -> TerminalThemeEditor:
    """Open the editor for *key*, or for a new theme based on the active one."""
    base_key = None
    if key is None:
        base_key = str(config.get_setting("terminal.theme", "default"))
    editor = TerminalThemeEditor(config, key=key, base_key=base_key)
    editor.present(parent)
    return editor


def confirm_delete_custom_theme(parent: Gtk.Widget, config, key: str) -> None:
    """Ask, then delete the custom theme *key*."""
    themes = getattr(config, "terminal_themes", {}) or {}
    name = str((themes.get(key) or {}).get("name") or key)
    fallback = str((themes.get("default") or {}).get("name") or "default")
    dialog = Adw.AlertDialog(
        heading=_("Delete “{theme}”?").format(theme=name),
        body=_("Terminals using this color scheme switch to {fallback}.").format(
            fallback=fallback
        ),
    )
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("delete", _("Delete"))
    dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def _on_response(_dialog, response: str) -> None:
        if response == "delete":
            config.remove_custom_theme(key)

    dialog.connect("response", _on_response)
    dialog.present(parent)
