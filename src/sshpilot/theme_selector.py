"""Ptyxis-style application theme selector for the main menu."""

from gettext import gettext as _

from gi.repository import Gdk, Gtk, GLib


_css_installed = False


def _install_css() -> None:
    global _css_installed
    if _css_installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(b"""
box.main-menu-theme-selector { padding: 12px; }
box.main-menu-theme-selector checkbutton.theme-selector-choice {
  border-radius: 999px;
  box-shadow: 0 0 0 1px alpha(currentColor, 0.25);
  min-width: 44px;
  min-height: 44px;
  padding: 0;
}
box.main-menu-theme-selector checkbutton.follow {
  background-image: linear-gradient(to bottom right, #fff 49.99%, #202020 50.01%);
}
box.main-menu-theme-selector checkbutton.light { background: #fff; }
box.main-menu-theme-selector checkbutton.dark { background: #202020; }
box.main-menu-theme-selector checkbutton.theme-selector-choice radio {
  -gtk-icon-source: none;
  border: none;
  background: none;
  box-shadow: none;
  min-width: 12px;
  min-height: 12px;
  transform: translate(27px, 14px);
  padding: 2px;
}
box.main-menu-theme-selector checkbutton.theme-selector-choice radio:checked {
  -gtk-icon-source: -gtk-icontheme("object-select-symbolic");
  background: @accent_bg_color;
  color: @accent_fg_color;
}
""")
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


def create_theme_selector() -> Gtk.Widget:
    """Return the three-choice visual theme selector used by the main menu."""
    _install_css()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_hexpand(True)
    box.add_css_class('main-menu-theme-selector')

    first = None
    for theme, css_class, tooltip in (
        ('default', 'follow', _('Follow System Style')),
        ('light', 'light', _('Light Style')),
        ('dark', 'dark', _('Dark Style')),
    ):
        button = Gtk.CheckButton()
        button.set_hexpand(True)
        button.set_halign(Gtk.Align.CENTER)
        button.set_focus_on_click(False)
        button.add_css_class('theme-selector-choice')
        button.add_css_class(css_class)
        button.set_tooltip_text(tooltip)
        button.set_action_name('win.set-app-theme')
        button.set_action_target_value(GLib.Variant('s', theme))
        if first is None:
            first = button
        else:
            button.set_group(first)
        box.append(button)
    return box
