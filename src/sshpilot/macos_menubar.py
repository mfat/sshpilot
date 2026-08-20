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

from .file_manager_integration import should_hide_file_manager_options
from .platform_utils import is_macos


def build_macos_menubar(
    *,
    menu_cls=Gio.Menu,
    menu_item_cls=Gio.MenuItem,
    variant_factory=GLib.Variant,
    plugins_section=None,
) -> Gio.Menu:
    """Build SSH Pilot's native macOS application menubar model.

    The macOS application menu (About / Settings… / Quit) is rendered
    natively by GTK from the ``app.*`` actions and must not be duplicated
    here — that would produce a redundant "SSH Pilot" menu in the bar.

    ``plugins_section`` is the shared mutable menu section that plugin pages
    are appended to (see ``UiHost._install_menu_item``); pass the same object
    ``install_menubar`` stores on the app. ``None`` (tests / off-macOS) just
    omits the section.
    """
    menubar = menu_cls()

    file_menu = menu_cls()
    file_menu.append(_("New Connection"), "app.new-connection")
    file_menu.append(_("Create Group"), "win.create-group")
    file_menu.append(_("Local Terminal"), "app.local-terminal")

    file_sessions = menu_cls()
    file_sessions.append(_("Save Session…"), "win.save-session")
    file_sessions.append(_("Open Session…"), "win.open-session")
    file_sessions.append(_("Manage Sessions…"), "win.manage-sessions")
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

    tools_menu = menu_cls()
    tools_menu.append(_("Copy Key to Server"), "app.new-key")
    tools_menu.append(_("Broadcast Command"), "app.broadcast-command")
    if not should_hide_file_manager_options():
        tools_menu.append(_("Manage Files"), "win.open-file-manager")

    tools_ssh_section = menu_cls()
    tools_ssh_section.append(_("SSH Config Editor"), "app.edit-ssh-config")
    tools_ssh_section.append(_("Known Hosts Editor"), "win.edit-known-hosts")
    tools_ssh_section.append(
        _("Manage Local authorized_keys…"), "win.manage-local-authorized-keys"
    )
    tools_menu.append_section(None, tools_ssh_section)

    # Plugin-contributed pages land in the native Tools menu through the same
    # shared/mutable section the hamburger menu uses, so entries the plugin
    # host appends after this model is built still appear.
    if plugins_section is not None:
        tools_menu.append_section(None, plugins_section)
    menubar.append_submenu(_("Tools"), tools_menu)

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
    help_menu.append(_("View Logs…"), "win.view-logs")
    help_menu.append(_("Report a Problem…"), "win.report-problem")
    help_menu.append(_("Export Diagnostics…"), "win.export-diagnostics")
    menubar.append_submenu(_("Help"), help_menu)

    return menubar


def install_menubar(app) -> None:
    """Set the native menubar on *app* when running on macOS.

    The plugin-contributed Tools entries share one mutable section between
    the in-window hamburger menu and this menubar: the object is created here
    (before any window exists), stored on *app*, passed into the builder, and
    later appended to by ``UiHost._install_menu_item``.

    No-op elsewhere: the in-window hamburger menu keeps serving Linux and
    Windows.
    """
    if not is_macos():
        return
    plugins_section = getattr(app, "_macos_plugins_menu_section", None)
    if plugins_section is None:
        plugins_section = Gio.Menu()
        app._macos_plugins_menu_section = plugins_section
    app.set_menubar(build_macos_menubar(plugins_section=plugins_section))