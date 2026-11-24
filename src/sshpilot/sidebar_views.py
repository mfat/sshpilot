"""The window's two views: Connections, and the sections of the SSH configuration.

A dropdown at the top of the sidebar switches the whole window between them.

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
draft stays when the view is left: the dropdown marks unsaved changes, and
quitting asks about them (``confirm_quit``).
"""

from __future__ import annotations

import logging
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

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


def _labels(unsaved: bool = False):
    sections = _("Configuration")
    # The dot Advanced Configuration's own title carries for unsaved changes.
    return [_("Connections"), sections + " •" if unsaved else sections]


def _mark_unsaved(window, unsaved: bool) -> None:
    """Say on the dropdown that the configuration has unsaved changes."""
    switcher = getattr(window, "_sidebar_view_switcher", None)
    if switcher is None:
        return
    model = switcher.get_model()
    label = _labels(unsaved)[1]
    if model.get_string(1) == label:
        return
    selected = switcher.get_selected()
    # Renaming the entry is not choosing a view.
    window._sidebar_switcher_syncing = True
    try:
        model.splice(1, 1, [label])
        switcher.set_selected(selected)
    finally:
        window._sidebar_switcher_syncing = False


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
    switcher = getattr(window, "_sidebar_view_switcher", None)
    if switcher is not None and switcher.get_selected() != _VIEWS.index(name):
        switcher.set_selected(_VIEWS.index(name))
    # The search and the buttons under the list are the connections'.
    for attr in ("_sidebar_toolbar_clip", "search_button"):
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

#: The dropdown's height until a connection row has been measured.
_SWITCHER_HEIGHT = 46

_SWITCHER_CSS = """
dropdown.sidebar-view-switcher > button {
  background-color: @accent_bg_color;
  color: @accent_fg_color;
  font-weight: bold;
  border-radius: 8px;
  box-shadow: none;
}
dropdown.sidebar-view-switcher > button:hover {
  background-image: image(alpha(currentColor, 0.1));
}
dropdown.sidebar-view-switcher > button:active,
dropdown.sidebar-view-switcher > button:checked {
  background-image: image(alpha(black, 0.15));
}
"""


def _install_switcher_css() -> None:
    from gi.repository import Gdk

    display = Gdk.Display.get_default()
    if display is None or getattr(display, "_sidebar_switcher_css", False):
        return
    provider = Gtk.CssProvider()
    try:
        provider.load_from_string(_SWITCHER_CSS)
    except (AttributeError, TypeError):         # GTK before 4.12
        provider.load_from_data(_SWITCHER_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    display._sidebar_switcher_css = True


def sync_switcher_height(window) -> None:
    """Make the dropdown as tall as a connection row.

    The rows' height follows the sidebar mode and the font, so it is
    measured from the first connection row there is.
    """
    switcher = getattr(window, "_sidebar_view_switcher", None)
    listbox = getattr(window, "connection_list", None)
    if switcher is None or listbox is None:
        return
    row = listbox.get_first_child()
    while row is not None:
        if getattr(row, "connection", None) is not None and row.get_visible():
            # As drawn, once it has a size; what it asks for before that.
            height = row.get_height()
            if height <= 0:
                try:
                    height = row.measure(Gtk.Orientation.VERTICAL, -1)[1]
                except Exception:
                    return
            if height > 0:
                switcher.set_size_request(-1, height)
            return
        row = row.get_next_sibling()


def build_sidebar_switcher(window, sidebar_box) -> None:
    """The dropdown at the top of the sidebar that picks the view."""
    _install_switcher_css()
    dropdown = Gtk.DropDown.new_from_strings(_labels())
    # Filled with the accent colour, as a suggested action is.
    dropdown.add_css_class("sidebar-view-switcher")
    dropdown.set_size_request(-1, _SWITCHER_HEIGHT)
    dropdown.set_hexpand(True)
    dropdown.set_margin_start(6)
    dropdown.set_margin_end(6)
    dropdown.set_margin_bottom(6)
    dropdown.set_tooltip_text(_("What the window shows"))

    def chosen(d, _pspec):
        index = d.get_selected()
        if getattr(window, "_sidebar_switcher_syncing", False) or index >= len(_VIEWS):
            return
        show_view(window, _VIEWS[index])

    dropdown.connect("notify::selected", chosen)
    window._sidebar_view_switcher = dropdown
    sidebar_box.append(dropdown)


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
    # Once the first rows are on screen; later rebuilds keep it in step.
    GLib.timeout_add(1500, lambda: (sync_switcher_height(window), False)[1])
    if window.lookup_action("new-host-rule") is None:
        action = Gio.SimpleAction.new("new-host-rule", None)
        action.connect("activate", lambda *_a: new_host_rule(window))
        window.add_action(action)


def new_host_rule(window) -> None:
    """Begin a new, unsaved Host block in Advanced Configuration."""
    opener = getattr(window, "open_advanced_config", None)
    if callable(opener):
        opener(new_block=("", ()))
