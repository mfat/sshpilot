"""The window's two views: Connections, and the sections of the SSH configuration.

A rail at the window's leading edge switches the whole window between them.
It is outside the sidebar, so it stays when the sidebar is hidden: choosing
the other view shows the sidebar again, and choosing the view already shown
hides or shows the sidebar, as F9 does.

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

    view._confirm_discard(discard)
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
    # The sidebar's title says which view it is.
    title = getattr(window, "_sidebar_title_label", None)
    if title is not None:
        title.set_label(_("Configuration") if sections else "SSH Pilot")
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
.view-rail button { min-width: 36px; min-height: 36px; padding: 0; border-radius: 8px; }
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
    return (
        (CONNECTIONS, _("Connections"), "network-server-symbolic"),
        (SECTIONS, _("Configuration"), "document-properties-symbolic"),
    )


def _sync_rail(window) -> None:
    """Mark the view shown, and the configuration's unsaved changes."""
    buttons = getattr(window, "_view_rail_buttons", None)
    if not buttons:
        return
    shown = current_view(window)
    unsaved = bool(getattr(window, "_config_unsaved", False))
    for name, title, _icon in _rail_items():
        button = buttons[name]
        if name == shown:
            button.add_css_class("current")
        else:
            button.remove_css_class("current")
        if name == SECTIONS:
            # The dot Advanced Configuration's own title carries.
            window._view_rail_unsaved_dot.set_visible(unsaved)
            button.set_tooltip_text(
                _("{view} (unsaved changes)").format(view=title) if unsaved else title
            )


def choose_view(window, name: str) -> None:
    """A click on the rail: *name* is shown, with the sidebar; the view
    already shown hides or shows the sidebar instead."""
    if current_view(window) == name:
        toggle = getattr(window, "on_sidebar_toggle", None)
        if callable(toggle):
            toggle(None)
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
    for name, title, icon in _rail_items():
        button = Gtk.Button()
        button.add_css_class("flat")
        button.set_can_focus(False)
        icon_utils.set_button_icon(button, icon)
        button.set_tooltip_text(title)
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
    rail = Gtk.WindowHandle()
    rail.add_css_class("view-rail")
    rail.set_child(box)
    window._view_rail_buttons = buttons
    window.view_rail = rail
    _sync_rail(window)
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
