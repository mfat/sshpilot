"""Native macOS application menubar built from SSH Pilot's existing actions.

On macOS GTK renders a ``Gio.Menu`` set via ``Gtk.Application.set_menubar``
as the system menu bar. This module builds that model by reusing the same
``app.*`` / ``win.*`` actions the in-window hamburger menu already registers
— the handlers live in exactly one place.

The application menu itself (the bold first menu with About / Settings /
Quit) is deliberately *not* built here: GTK's macOS backend already renders
it natively from the ``app.*`` actions. Only the trailing menus (File … Help)
live in the menubar model.

The builder takes injectable constructors so tests can capture the menu
structure without instantiating real GIO objects.
"""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Gio, GLib

from .platform_utils import is_macos


def build_macos_menubar(
    *,
    menu_cls=Gio.Menu,
    menu_item_cls=Gio.MenuItem,
    variant_factory=GLib.Variant,
) -> Gio.Menu:
    """Build SSH Pilot's native macOS application menubar model.

    The macOS application menu (About / Settings… / Quit) is rendered
    natively by GTK from the ``app.*`` actions and must not be duplicated
    here — that would produce a redundant "SSH Pilot" menu in the bar.
    """
    menubar = menu_cls()

    file_menu = menu_cls()
    file_menu.append(_("New Connection"), "app.new-connection")
    file_menu.append(_("Local Terminal"), "app.local-terminal")

    file_sessions = menu_cls()
    file_sessions.append(_("Save Session…"), "win.save-session")
    file_sessions.append(_("Open Session…"), "win.open-session")
    file_menu.append_section(None, file_sessions)

    file_transfer = menu_cls()
    file_transfer.append(_("Import Configuration…"), "win.import-config")
    file_transfer.append(_("Export Configuration…"), "win.export-config")
    file_menu.append_section(None, file_transfer)
    menubar.append_submenu(_("File"), file_menu)

    edit_menu = menu_cls()
    edit_menu.append(_("Undo"), "text.undo")
    edit_menu.append(_("Redo"), "text.redo")

    clipboard_section = menu_cls()
    clipboard_section.append(_("Cut"), "clipboard.cut")
    clipboard_section.append(_("Copy"), "clipboard.copy")
    clipboard_section.append(_("Paste"), "clipboard.paste")
    clipboard_section.append(_("Select All"), "selection.select-all")
    edit_menu.append_section(None, clipboard_section)
    menubar.append_submenu(_("Edit"), edit_menu)

    view_menu = menu_cls()
    view_menu.append(_("Toggle Sidebar"), "win.toggle_sidebar")
    view_menu.append(_("Toggle Full Screen"), "win.toggle-fullscreen")
    view_menu.append(_("Tab Overview"), "app.tab-overview")
    menubar.append_submenu(_("View"), view_menu)

    window_menu = menu_cls()
    window_menu.append(_("Next Tab"), "app.tab-next")
    window_menu.append(_("Previous Tab"), "app.tab-prev")

    window_item = menu_item_cls.new_submenu(_("Window"), window_menu)
    window_item.set_attribute_value(
        "gtk-macos-special",
        variant_factory("s", "window-submenu"),
    )
    menubar.append_item(window_item)

    help_menu = menu_cls()
    help_menu.append(_("Keyboard Shortcuts"), "app.shortcuts")
    help_menu.append(_("Documentation"), "app.help")
    help_menu.append(_("Check for Updates"), "win.check-for-updates")
    help_menu.append(_("Report a Problem…"), "win.report-problem")
    menubar.append_submenu(_("Help"), help_menu)

    return menubar


def install_menubar(app) -> None:
    """Set the native menubar on *app* when running on macOS.

    No-op elsewhere: the in-window hamburger menu keeps serving Linux and
    Windows.
    """
    if not is_macos():
        return
    app.set_menubar(build_macos_menubar())