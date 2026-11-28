"""The window's two views: Connections, and the sections of the SSH configuration.

A rail at the window's leading edge switches the whole window between them.
It is outside the sidebar, so it stays when the sidebar is hidden: choosing
choosing either view shows the sidebar again. It never hides it: that is
F9's, and the header button's.

Settings ▸ Sidebar ▸ View Rail (``ui.view_rail``) can hide it with the
sidebar or for good. Whenever the rail is not on screen, Advanced
Configuration opens in its own window, not as the Configuration view: the
view needs the rail to lead back to the connections. So the rail stays while
the Configuration view is shown, and leaving the rail for good leaves that
view.

* **Connections**: the connection list, and the tabs and terminals beside it.
* **Configuration**: the sidebar lists every block of the configuration in
  file order (the options above all blocks, each ``Host`` and ``Match``
  block), each said to be a connection, a host pattern, a Match rule or
  options for all hosts; the content area shows the blocks and what OpenSSH
  resolves, in place of the tabs. It is Advanced Configuration
  (``AdvancedConfigView``): the same editor as the window, which is still
  what opens over a dialog (the connection editor) that the main window
  cannot be used behind.

The tabs are hidden while the sections are shown, not closed: terminals keep
running. Selecting or opening a tab brings the Connections view back. The
draft stays when the view is left: the rail marks unsaved changes, and
quitting asks about them (``confirm_quit``).
"""

from __future__ import annotations

import logging
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, Gtk

logger = logging.getLogger(__name__)

CONNECTIONS = "connections"
SECTIONS = "sections"

# ui.view_rail: when the rail is on screen.
RAIL_ALWAYS = "always"
RAIL_WITH_SIDEBAR = "with_sidebar"
RAIL_NEVER = "never"
RAIL_MODES = (RAIL_ALWAYS, RAIL_WITH_SIDEBAR, RAIL_NEVER)
RAIL_SETTING = "ui.view_rail"


def rail_mode(window) -> str:
    config = getattr(window, "config", None)
    try:
        mode = config.get_setting(RAIL_SETTING, RAIL_ALWAYS) if config else RAIL_ALWAYS
    except Exception:
        mode = RAIL_ALWAYS
    return mode if mode in RAIL_MODES else RAIL_ALWAYS


def rail_wanted(window) -> bool:
    """Whether the rail belongs on screen now (terminal fullscreen aside)."""
    mode = rail_mode(window)
    if mode == RAIL_NEVER:
        return False
    if mode == RAIL_ALWAYS or current_view(window) == SECTIONS:
        return True
    hidden = getattr(window, "_sidebar_hidden_by_toggle", None)
    if callable(hidden):
        try:
            return not hidden()
        except Exception:
            return True
    return bool(getattr(window, "_sidebar_visible", True))


def sync_rail_visibility(window) -> None:
    """Show or hide the rail as ``ui.view_rail`` and the sidebar say."""
    rail = getattr(window, "view_rail", None)
    if rail is None:
        return
    fullscreen = getattr(window, "fullscreen_controller", None)
    if fullscreen is not None and getattr(fullscreen, "terminal_presentation_active", False):
        return                          # it hides the rail, and restores it
    wanted = rail_wanted(window)
    if rail.get_visible() != wanted:
        rail.set_visible(wanted)


def on_rail_setting_changed(window) -> None:
    """``ui.view_rail`` changed: never leaves no way back from the
    Configuration view, so that view is left (a draft in it is kept, and
    quitting still asks about it)."""
    if rail_mode(window) == RAIL_NEVER:
        leave_sections(window)
    sync_rail_visibility(window)


def opens_in_window(window) -> bool:
    """Whether Advanced Configuration opens in its own window: when the
    rail, the way back to the connections, is not on screen."""
    rail = getattr(window, "view_rail", None)
    if rail_mode(window) == RAIL_NEVER:
        return True
    return rail is not None and not rail.get_visible()


def current_view(window) -> str:
    stack = getattr(window, "_sidebar_view_stack", None)
    return (stack.get_visible_child_name() or CONNECTIONS) if stack else CONNECTIONS


def _supported(window) -> bool:
    from .api.capabilities import Capability

    client = getattr(window, "client", None)
    try:
        return bool(
            client is not None
            and client.get_capabilities().supports(Capability.CONNECTIONS_CONFIG_READ)
            and callable(getattr(client, "evaluate_ssh_config_draft", None))
        )
    except Exception:
        return False


def _content_slot(window):
    """Where the tabs are shown: the box the sections view is put in beside
    them, or None when the window is laid out without one."""
    overview = getattr(window, "tab_overview", None)
    parent = overview.get_parent() if overview is not None else None
    return parent if isinstance(parent, Gtk.Box) else None


def _ensure_sections_view(window):
    """The read-only Advanced Configuration of *window*, made on first use."""
    view = getattr(window, "_config_sections_view", None)
    if view is not None:
        return view
    slot = _content_slot(window)
    if slot is None or not _supported(window):
        return None
    from .advanced_config_window import AdvancedConfigView

    def saved():
        schedule = getattr(window, "_schedule_config_syntax_check", None)
        if callable(schedule):
            schedule()

    view = AdvancedConfigView(window, window.client, on_saved=saved)
    view.set_vexpand(True)
    view.set_hexpand(True)
    view.set_visible(False)
    view.host_present = lambda: show_view(window, SECTIONS)
    view.host_close = lambda: show_view(window, CONNECTIONS)
    view.host_dirty = lambda dirty: _mark_unsaved(window, dirty)
    slot.append(view)
    # Its controls (undo, the view switcher, Save) go in the window's own
    # title row while the view is shown: one bar, not two stacked.
    title = getattr(window, "_content_title_stack", None)
    header = getattr(view, "header_widget", None)
    if title is not None and header is not None:
        title.add_named(header, "config")
    column = view.sidebar_column
    column.set_vexpand(True)
    window._sidebar_sections_holder.append(column)
    window._config_sections_view = view
    return view


def _mark_unsaved(window, unsaved: bool) -> None:
    """Say on the rail that the configuration has unsaved changes."""
    window._config_unsaved = bool(unsaved)
    _sync_rail(window)


def open_sections(window, *, host="", line=0, option="", file="", pin=False,
                  new_block=None):
    """Show the Configuration view at a place of the configuration, as
    ``MainWindow.open_advanced_config`` takes it. Returns the view, or None
    when it cannot be shown here (the caller then opens the window)."""
    view = _ensure_sections_view(window)
    if view is None:
        return None
    show_view(window, SECTIONS)
    if host or line or file:
        view.go_to(host=host, line=line, option=option, file=file, pin=pin)
    if new_block:
        view.start_block(new_block[0], new_block[1])
    return view


def confirm_quit(window, proceed) -> bool:
    """Ask about unsaved configuration changes before the window closes.

    True when the question is up: closing waits, and *proceed* runs if the
    changes are discarded. False when there is nothing to ask.
    """
    view = getattr(window, "_config_sections_view", None)
    # Discarding closes again: that one request is not asked a second time.
    # Only that one: the close may still be called off (the question about
    # running sessions), and the draft is then as unsaved as before. The
    # view goes on working meanwhile, so nothing of it is switched off here.
    answered = bool(getattr(window, "_config_discard_answered", False))
    window._config_discard_answered = False
    if view is None or not view.dirty or answered:
        return False
    show_view(window, SECTIONS)

    def discard():
        window._config_discard_answered = True
        proceed()

    def stay():
        # Shown only to ask: without the rail there is no way back.
        if rail_mode(window) == RAIL_NEVER:
            show_view(window, CONNECTIONS)

    view._confirm_discard(discard, stay)
    return True


def show_view(window, name: str) -> None:
    """Show the Connections or the Configuration view of the window."""
    stack = getattr(window, "_sidebar_view_stack", None)
    if stack is None or name not in _VIEWS:
        return
    sections = name == SECTIONS
    view = _ensure_sections_view(window) if sections else getattr(
        window, "_config_sections_view", None
    )
    if sections and view is None:
        # No background service to ask, or nowhere to show it: say so and
        # stay on the connections.
        toast = getattr(window, "show_toast", None)
        if callable(toast):
            toast(_(
                "SSH config editing requires the background service. "
                "Start it and try again."
            ))
        sections, name = False, CONNECTIONS
    was = current_view(window)
    stack.set_visible_child_name(name)
    _sync_rail(window)
    sync_rail_visibility(window)
    # The search, the New menu, the view options (sort, tag filter,
    # hostnames) and the buttons under the list are the connections': the
    # list of blocks has its own search and New Block button.
    for attr in (
        "_sidebar_toolbar_clip", "search_button",
        "_sidebar_add_button", "_sidebar_view_button",
    ):
        widget = getattr(window, attr, None)
        if widget is not None:
            widget.set_visible(not sections)
    # The search field goes too, and comes back as it was: a query left in
    # it still filters the connections, and must not do so out of sight.
    container = getattr(window, "search_container", None)
    if container is not None:
        if sections and was != SECTIONS:
            window._search_shown_before_sections = container.get_visible()
            container.set_visible(False)
        elif not sections and was == SECTIONS:
            container.set_visible(
                bool(getattr(window, "_search_shown_before_sections", False))
            )
    # The tabs stay as they are, out of sight: terminals keep running.
    overview = getattr(window, "tab_overview", None)
    if view is not None:
        view.set_visible(sections)
    if overview is not None and view is not None:
        overview.set_visible(not sections)
    # The tab bar in the header bar goes with them.
    update_tabs = getattr(window, "_update_tab_button_visibility", None)
    if callable(update_tabs):
        update_tabs()
    if sections and was != SECTIONS:
        view.reload()                   # does nothing over unsaved changes


def focus_sections_search(window) -> bool:
    """Ctrl+F while the Configuration view is shown: its search of the
    blocks, not the connections'. False when that view is not the one shown."""
    view = getattr(window, "_config_sections_view", None)
    if view is None or current_view(window) != SECTIONS:
        return False
    entry = view.target_entry
    entry.grab_focus()
    text = entry.get_text()
    if text:
        entry.select_region(0, len(text))
    return True


def refresh_sections(window) -> None:
    """Read the configuration again if its sections are what is shown."""
    view = getattr(window, "_config_sections_view", None)
    if view is not None and current_view(window) == SECTIONS:
        view.reload()


def leave_sections(window) -> None:
    """Back to the connections: a tab was selected or opened."""
    if current_view(window) == SECTIONS:
        show_view(window, CONNECTIONS)


_VIEWS = (CONNECTIONS, SECTIONS)

_RAIL_CSS = """
.view-rail {
  background-color: @sidebar_bg_color;
  border-right: 1px solid @sidebar_border_color;
  padding: 6px;
}
.view-rail:backdrop { background-color: @sidebar_backdrop_color; }
.view-rail:dir(rtl) {
  border-right: none;
  border-left: 1px solid @sidebar_border_color;
}
.view-rail button { min-width: 44px; min-height: 44px; padding: 0; border-radius: 10px; }
/* 20px icons, drawn with thinner lines (the *-rail-symbolic ones) so they
   look as light as the 16px icons elsewhere. */
.view-rail button image { -gtk-icon-size: 20px; }
.view-rail button.current {
  background-color: alpha(@accent_bg_color, 0.18);
  color: @accent_color;
}
.view-rail .unsaved-dot {
  min-width: 8px;
  min-height: 8px;
  margin: 4px;
  border-radius: 999px;
  background-color: @accent_bg_color;
}
"""


def _install_rail_css() -> None:
    from gi.repository import Gdk

    display = Gdk.Display.get_default()
    if display is None or getattr(display, "_view_rail_css", False):
        return
    provider = Gtk.CssProvider()
    try:
        provider.load_from_string(_RAIL_CSS)
    except (AttributeError, TypeError):         # GTK before 4.12
        provider.load_from_data(_RAIL_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    display._view_rail_css = True


def _rail_items():
    """(view, accessible name, tooltip, icon) of each rail button."""
    return (
        (CONNECTIONS, _("Connections"), _("Connections and groups"),
         "connections-rail-symbolic"),
        (SECTIONS, _("Configuration"), _("Advanced SSH configuration"),
         "document-config-rail-symbolic"),
    )


def _sync_rail(window) -> None:
    """Mark the view shown, and the configuration's unsaved changes."""
    buttons = getattr(window, "_view_rail_buttons", None)
    if not buttons:
        return
    shown = current_view(window)
    unsaved = bool(getattr(window, "_config_unsaved", False))
    for name, _title, tooltip, _icon in _rail_items():
        button = buttons[name]
        if name == shown:
            button.add_css_class("current")
        else:
            button.remove_css_class("current")
        if name == SECTIONS:
            # The dot Advanced Configuration's own title carries.
            window._view_rail_unsaved_dot.set_visible(unsaved)
            button.set_tooltip_text(
                _("{view} (unsaved changes)").format(view=tooltip) if unsaved else tooltip
            )


def choose_view(window, name: str) -> None:
    """A click on the rail: *name* is shown, with the sidebar. The view
    already shown brings a hidden sidebar back and otherwise does nothing:
    a click on the button of the view one is in must not take its list
    away."""
    if current_view(window) == name:
        reveal = getattr(window, "_reveal_sidebar", None)
        if callable(reveal):
            reveal()                    # docked, or floated when too narrow
        elif not getattr(window, "_sidebar_visible", True):
            show = getattr(window, "_apply_sidebar_visible", None)
            if callable(show):
                show(True)
        return
    show_view(window, name)
    if current_view(window) == name and not getattr(window, "_sidebar_visible", True):
        show = getattr(window, "_apply_sidebar_visible", None)
        if callable(show):
            show(True)


def build_view_rail(window) -> Gtk.Widget:
    """The rail that picks the view, at the window's leading edge. Its
    empty part moves the window, as a header bar's does."""
    from . import icon_utils

    _install_rail_css()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    buttons = {}
    for name, title, tooltip, icon in _rail_items():
        button = Gtk.Button()
        button.add_css_class("flat")
        button.set_can_focus(False)
        icon_utils.set_button_icon(button, icon)
        button.set_tooltip_text(tooltip)
        button.update_property([Gtk.AccessibleProperty.LABEL], [title])
        button.connect("clicked", lambda _b, n=name: choose_view(window, n))
        buttons[name] = button
        if name != SECTIONS:
            box.append(button)
            continue
        overlay = Gtk.Overlay()
        overlay.set_child(button)
        dot = Gtk.Box()
        dot.add_css_class("unsaved-dot")
        dot.set_halign(Gtk.Align.END)
        dot.set_valign(Gtk.Align.START)
        dot.set_can_target(False)
        dot.set_visible(False)
        overlay.add_overlay(dot)
        window._view_rail_unsaved_dot = dot
        box.append(overlay)
    # Settings at the foot of the rail: it opens the Settings window, it is
    # not a view, so it sits apart from the views and never shows as current.
    # The gap above it is still empty rail, so it still moves the window.
    spacer = Gtk.Box()
    spacer.set_vexpand(True)
    box.append(spacer)
    settings = Gtk.Button()
    settings.add_css_class("flat")
    settings.set_can_focus(False)
    icon_utils.set_button_icon(settings, "settings-rail-symbolic")
    settings.set_tooltip_text(_("Settings"))
    settings.update_property([Gtk.AccessibleProperty.LABEL], [_("Settings")])
    settings.set_action_name("app.preferences")
    box.append(settings)
    window._view_rail_settings_button = settings
    rail = Gtk.WindowHandle()
    rail.add_css_class("view-rail")
    rail.set_child(box)
    window._view_rail_buttons = buttons
    window.view_rail = rail
    _sync_rail(window)
    sync_rail_visibility(window)
    return rail


def build_sidebar_view_stack(window, sidebar_box) -> None:
    """Put the connection list and the list of sections where the list was."""
    stack = Gtk.Stack()
    stack.set_vexpand(True)
    stack.set_hexpand(True)
    stack.add_named(window.connection_scrolled, CONNECTIONS)
    # Filled with Advanced Configuration's list of blocks on first use.
    holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    window._sidebar_sections_holder = holder
    stack.add_named(holder, SECTIONS)
    window._sidebar_view_stack = stack
    window._config_sections_view = None
    sidebar_box.append(stack)
    _sync_rail(window)
    if window.lookup_action("new-host-rule") is None:
        action = Gio.SimpleAction.new("new-host-rule", None)
        action.connect("activate", lambda *_a: new_host_rule(window))
        window.add_action(action)


def new_host_rule(window) -> None:
    """Begin a new, unsaved Host block in Advanced Configuration."""
    opener = getattr(window, "open_advanced_config", None)
    if callable(opener):
        opener(new_block=("", ()))
