"""Visual terminal color-scheme chooser shared by window chrome and Settings."""

from gettext import gettext as _
from typing import Callable, Mapping

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
button.terminal-palette-card.terminal-palette-selected {
  box-shadow: 0 0 0 2px @accent_bg_color;
}
button.terminal-palette-card image.terminal-palette-check {
  background: @accent_bg_color;
  border-radius: 999px;
  color: @accent_fg_color;
  padding: 3px;
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


def _palette_colors(theme: Mapping[str, object]) -> tuple[object, ...]:
    palette = theme.get("palette") or ()
    if isinstance(palette, (list, tuple)):
        # Match Ptyxis: show ANSI red through cyan, not background/black.
        colors = tuple(palette[1:7])
        if colors:
            return colors
    return (theme.get("foreground", "#ffffff"),) * 6


class TerminalThemeChooser:
    """Scrollable, preview-based selector suitable for a ``Gtk.Popover``."""

    def __init__(
        self,
        themes: Mapping[str, Mapping[str, object]],
        selected_key: str,
        on_selected: Callable[[str], None],
    ) -> None:
        _install_css()
        self._on_selected = on_selected
        self._buttons: dict[str, Gtk.Button] = {}
        self._checks: dict[str, Gtk.Image] = {}

        self.flow_box = Gtk.FlowBox()
        self.flow_box.add_css_class("terminal-palette-grid")
        self.flow_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow_box.set_homogeneous(True)
        self.flow_box.set_min_children_per_line(3)
        self.flow_box.set_max_children_per_line(3)
        self.flow_box.set_column_spacing(9)
        self.flow_box.set_row_spacing(9)
        self.flow_box.set_margin_top(9)
        self.flow_box.set_margin_bottom(9)
        self.flow_box.set_margin_start(9)
        self.flow_box.set_margin_end(9)

        for key in selectable_terminal_theme_keys(themes):
            theme = themes[key]
            button = Gtk.Button()
            button.add_css_class("terminal-palette-card")
            button.set_focus_on_click(False)
            button.set_overflow(Gtk.Overflow.HIDDEN)
            button.set_tooltip_text(
                _("Use {theme} terminal colors").format(
                    theme=str(theme.get("name") or key)
                )
            )
            button.connect("clicked", self._on_button_clicked, key)

            overlay = Gtk.Overlay()
            overlay.set_overflow(Gtk.Overflow.HIDDEN)
            background = Gtk.DrawingArea()
            background.set_content_width(168)
            background.set_content_height(150)
            background.set_draw_func(_draw_background, theme)
            overlay.set_child(background)

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            content.set_margin_top(12)
            content.set_margin_bottom(12)
            content.set_margin_start(12)
            content.set_margin_end(12)

            title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            title = Gtk.Label(label=str(theme.get("name") or key))
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
            self.flow_box.append(button)
            self._buttons[key] = button
            self._checks[key] = check

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_min_content_width(570)
        self.scroller.set_min_content_height(470)
        self.scroller.set_child(self.flow_box)

        self.container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        heading = Gtk.Label(label=_("Palette"))
        heading.set_xalign(0)
        heading.add_css_class("heading")
        heading.set_margin_top(12)
        heading.set_margin_start(12)
        heading.set_margin_end(12)
        self.container.append(heading)
        self.container.append(self.scroller)
        self.set_selected(selected_key)

    @property
    def widget(self) -> Gtk.Widget:
        return self.container

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
