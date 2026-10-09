"""Visual terminal color-scheme chooser shared by window chrome and Settings."""

from gettext import gettext as _
from typing import Callable, Mapping, Optional, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk, Pango


_css_installed = False


def _install_css() -> None:
    """Install the palette-card treatment once for the current display."""
    global _css_installed
    if _css_installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(b"""
button.terminal-palette-card {
  background: transparent;
  border: none;
  border-radius: 12px;
  box-shadow: 0 0 0 1px alpha(currentColor, 0.20);
  margin: 3px;
  padding: 0;
}
button.terminal-palette-card:hover {
  box-shadow: 0 0 0 1px alpha(currentColor, 0.38);
}
button.terminal-palette-card.terminal-palette-new {
  box-shadow: none;
  border: 1px dashed alpha(currentColor, 0.38);
}
button.terminal-palette-card.terminal-palette-new:hover {
  background: alpha(currentColor, 0.06);
}
button.terminal-palette-card.terminal-palette-selected {
  box-shadow: 0 0 0 2px @accent_bg_color;
}
button.terminal-palette-card image.terminal-palette-check {
  background: @accent_bg_color;
  border-radius: 999px;
  color: @accent_fg_color;
  padding: 3px;
}
flowbox.terminal-palette-grid flowboxchild,
flowbox.terminal-palette-grid flowboxchild:hover {
  background: none;
}
flowbox.terminal-palette-grid flowboxchild {
  border-radius: 12px;
  outline-offset: 2px;
  padding: 0;
}
""")
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


# Display order for built-in terminal schemes. Names and colors remain owned by
# Config.terminal_themes; this tuple only defines the user-facing picker order.
TERMINAL_SCHEME_KEYS = (
    "default", "black_on_white", "solarized_dark", "solarized_light",
    "monokai", "dracula", "nord", "gruvbox_dark", "one_dark",
    "tomorrow_night", "material_dark", "rose_pine", "rose_pine_moon",
    "rose_pine_dawn", "catppuccin_latte", "catppuccin_frappe",
    "catppuccin_macchiato", "catppuccin_mocha",
)


def selectable_terminal_theme_keys(themes: Mapping[str, object]) -> tuple[str, ...]:
    """Return picker keys that exist in the authoritative theme catalog."""
    return tuple(key for key in TERMINAL_SCHEME_KEYS if key in themes)


def _rgba(color: object, fallback: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    try:
        parsed = rgba.parse(str(color or fallback))
    except Exception:
        parsed = False
    if not parsed:
        rgba.parse(fallback)
    return rgba


def _set_source(cr, color: object, fallback: str) -> None:
    rgba = _rgba(color, fallback)
    cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha)


def _draw_background(
    _area, cr, width: int, height: int, theme: Mapping[str, object]
) -> None:
    _set_source(cr, theme.get("background"), "#000000")
    cr.rectangle(0, 0, width, height)
    cr.fill()


def _draw_swatch(_area, cr, width: int, height: int, color: object) -> None:
    _set_source(cr, color, "#ffffff")
    radius = min(5.0, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(width - radius, radius, radius, -1.5708, 0)
    cr.arc(width - radius, height - radius, radius, 0, 1.5708)
    cr.arc(radius, height - radius, radius, 1.5708, 3.14159)
    cr.arc(radius, radius, radius, 3.14159, 4.71239)
    cr.close_path()
    cr.fill()


def _set_label_color(
    label: Gtk.Label, color: object, *, weight: Pango.Weight | None = None
) -> None:
    rgba = _rgba(color, "#ffffff")
    attrs = Pango.AttrList()
    attrs.insert(
        Pango.attr_foreground_new(
            round(rgba.red * 65535),
            round(rgba.green * 65535),
            round(rgba.blue * 65535),
        )
    )
    if weight is not None:
        attrs.insert(Pango.attr_weight_new(weight))
    label.set_attributes(attrs)


_CARD_WIDTH = 168
_CARD_HEIGHT = 150


def _configure_grid(grid: Gtk.FlowBox) -> None:
    grid.add_css_class("terminal-palette-grid")
    grid.set_selection_mode(Gtk.SelectionMode.NONE)
    grid.set_homogeneous(True)
    grid.set_min_children_per_line(3)
    grid.set_max_children_per_line(3)
    grid.set_column_spacing(9)
    grid.set_row_spacing(9)
    grid.set_margin_top(9)
    grid.set_margin_bottom(9)
    grid.set_margin_start(9)
    grid.set_margin_end(9)


def _palette_colors(theme: Mapping[str, object]) -> tuple[object, ...]:
    palette = theme.get("palette") or ()
    if isinstance(palette, (list, tuple)):
        # Match Ptyxis: show ANSI red through cyan, not background/black.
        colors = tuple(palette[1:7])
        if colors:
            return colors
    return (theme.get("foreground", "#ffffff"),) * 6


class TerminalThemeChooser:
    """Scrollable, preview-based selector suitable for a ``Gtk.Popover``.

    Built-in schemes come first. When *on_new* is given, a "Custom" section
    follows with the user's themes (each with Edit and Delete) and a "New
    Theme" card. Call :meth:`set_themes` after the catalog changes.
    """

    def __init__(
        self,
        themes: Mapping[str, Mapping[str, object]],
        selected_key: str,
        on_selected: Callable[[str], None],
        *,
        custom_keys: Sequence[str] = (),
        on_new: Optional[Callable[[], None]] = None,
        on_edit: Optional[Callable[[str], None]] = None,
        on_delete: Optional[Callable[[str], None]] = None,
    ) -> None:
        _install_css()
        self._on_selected = on_selected
        self._on_new = on_new
        self._on_edit = on_edit
        self._on_delete = on_delete
        self._buttons: dict[str, Gtk.Button] = {}
        self._checks: dict[str, Gtk.Image] = {}

        self.flow_box = Gtk.FlowBox()
        _configure_grid(self.flow_box)
        self.flow_box.set_max_children_per_line(3)

        self.custom_heading = Gtk.Label(label=_("Custom"))
        self.custom_heading.set_xalign(0)
        self.custom_heading.add_css_class("heading")
        self.custom_heading.set_margin_top(6)
        self.custom_heading.set_margin_start(12)
        self.custom_heading.set_margin_end(12)
        self.custom_flow_box = Gtk.FlowBox()
        _configure_grid(self.custom_flow_box)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.flow_box)
        content.append(self.custom_heading)
        content.append(self.custom_flow_box)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_min_content_width(570)
        self.scroller.set_min_content_height(470)
        self.scroller.set_child(content)

        self.container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        heading = Gtk.Label(label=_("Palette"))
        heading.set_xalign(0)
        heading.add_css_class("heading")
        heading.set_margin_top(12)
        heading.set_margin_start(12)
        heading.set_margin_end(12)
        self.container.append(heading)
        self.container.append(self.scroller)
        self.set_themes(themes, selected_key, custom_keys)

    @property
    def widget(self) -> Gtk.Widget:
        return self.container

    def set_themes(
        self,
        themes: Mapping[str, Mapping[str, object]],
        selected_key: str,
        custom_keys: Sequence[str] = (),
    ) -> None:
        """Rebuild the cards from *themes*; *custom_keys* go in the Custom section."""
        for grid in (self.flow_box, self.custom_flow_box):
            child = grid.get_child_at_index(0)
            while child is not None:
                grid.remove(child)
                child = grid.get_child_at_index(0)
        self._buttons.clear()
        self._checks.clear()

        for key in selectable_terminal_theme_keys(themes):
            self.flow_box.append(self._build_card(key, themes[key]))

        custom = [key for key in custom_keys if key in themes]
        for key in custom:
            self.custom_flow_box.append(self._build_custom_card(key, themes[key]))
        if self._on_new is not None:
            self.custom_flow_box.append(self._build_new_card())
        has_custom_section = bool(custom) or self._on_new is not None
        self.custom_heading.set_visible(has_custom_section)
        self.custom_flow_box.set_visible(has_custom_section)
        self.set_selected(selected_key)

    def _build_card(self, key: str, theme: Mapping[str, object]) -> Gtk.Button:
        name = str(theme.get("name") or key)
        button = Gtk.Button()
        button.add_css_class("terminal-palette-card")
        button.set_focus_on_click(False)
        button.set_overflow(Gtk.Overflow.HIDDEN)
        button.set_tooltip_text(
            _("Use {theme} terminal colors").format(theme=name)
        )
        button.connect("clicked", self._on_button_clicked, key)

        overlay = Gtk.Overlay()
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        background = Gtk.DrawingArea()
        background.set_content_width(_CARD_WIDTH)
        background.set_content_height(_CARD_HEIGHT)
        background.set_draw_func(_draw_background, theme)
        overlay.set_child(background)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title = Gtk.Label(label=name)
        title.set_xalign(0)
        title.set_hexpand(True)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.add_css_class("heading")
        _set_label_color(title, theme.get("foreground"))
        title_box.append(title)
        check = Gtk.Image.new_from_icon_name("object-select-symbolic")
        check.add_css_class("terminal-palette-check")
        check.set_valign(Gtk.Align.START)
        check.set_visible(False)
        title_box.append(check)
        content.append(title_box)

        sample = Gtk.Label(
            label=_("The quick brown fox jumps over the lazy dog")
        )
        sample.set_xalign(0)
        sample.set_hexpand(True)
        sample.set_wrap(True)
        sample.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        sample.set_lines(3)
        sample.set_ellipsize(Pango.EllipsizeMode.END)
        sample.add_css_class("monospace")
        _set_label_color(
            sample,
            theme.get("foreground"),
            weight=Pango.Weight.NORMAL,
        )
        content.append(sample)

        swatches = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        swatches.set_hexpand(True)
        for color in _palette_colors(theme):
            swatch = Gtk.DrawingArea()
            swatch.set_content_width(20)
            swatch.set_content_height(16)
            swatch.set_hexpand(True)
            swatch.set_tooltip_text(str(color))
            swatch.set_draw_func(_draw_swatch, color)
            swatches.append(swatch)
        content.append(swatches)

        overlay.add_overlay(content)
        button.set_child(overlay)
        self._buttons[key] = button
        self._checks[key] = check
        return button

    def _build_custom_card(
        self, key: str, theme: Mapping[str, object]
    ) -> Gtk.Widget:
        """A theme card with Edit and Delete beneath it (not inside the button)."""
        name = str(theme.get("name") or key)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self._build_card(key, theme))
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        actions.set_halign(Gtk.Align.END)
        for icon, tooltip, callback in (
            ("document-edit-symbolic", _("Edit {theme}"), self._on_edit),
            ("user-trash-symbolic", _("Delete {theme}"), self._on_delete),
        ):
            if callback is None:
                continue
            action = Gtk.Button.new_from_icon_name(icon)
            action.add_css_class("flat")
            action.add_css_class("circular")
            action.set_tooltip_text(tooltip.format(theme=name))
            action.update_property(
                [Gtk.AccessibleProperty.LABEL], [tooltip.format(theme=name)]
            )
            action.connect("clicked", lambda _b, cb=callback: cb(key))
            actions.append(action)
        box.append(actions)
        return box

    def _build_new_card(self) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("terminal-palette-card")
        button.add_css_class("terminal-palette-new")
        button.set_focus_on_click(False)
        button.set_valign(Gtk.Align.START)
        button.set_tooltip_text(_("Create a terminal color scheme"))
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_size_request(_CARD_WIDTH, _CARD_HEIGHT)
        content.set_valign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        icon.set_pixel_size(24)
        icon.set_vexpand(True)
        icon.set_valign(Gtk.Align.END)
        content.append(icon)
        label = Gtk.Label(label=_("New Theme"))
        label.set_vexpand(True)
        label.set_valign(Gtk.Align.START)
        content.append(label)
        button.set_child(content)
        button.connect("clicked", lambda _b: self._on_new())
        return button

    def set_selected(self, key: str) -> None:
        selected = self._buttons.get(key) or self._buttons.get("default")
        for theme_key, button in self._buttons.items():
            active = button is selected
            if active:
                button.add_css_class("terminal-palette-selected")
            else:
                button.remove_css_class("terminal-palette-selected")
            self._checks[theme_key].set_visible(active)

    def _on_button_clicked(self, _button: Gtk.Button, key: str) -> None:
        self.set_selected(key)
        self._on_selected(key)
