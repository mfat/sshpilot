"""Start-up hint: the Colored Grips sidebar style, for existing users.

New installs start with Colored Grips; upgraders keep the style they had
(core/settings/migration.py), since a stored 'dot' cannot tell a choice
from the old default. So when the main window opens with a coloured row's
drag grip on screen and any other style in use, a popover points at that
grip and offers to switch. It comes back on later starts until the user
picks "Switch" or "Don't Show Again", or picks Colored Grips in Settings.
"""

import logging
from gettext import gettext as _

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gdk, GLib, Gtk

logger = logging.getLogger(__name__)

HINT_DISMISSED_KEY = 'ui.colored_grips_hint_dismissed'
# After the window maps, so the hint lands once the start-up layout settles;
# then a few retries, since the connection list may still be filling in.
SHOW_DELAY_MS = 1200
RETRY_MS = 1000
MAX_ATTEMPTS = 5


def _row_on_screen(row, listbox) -> bool:
    """True when *row* lies inside the visible part of the list."""
    viewport = listbox.get_ancestor(Gtk.ScrolledWindow) or listbox
    ok, bounds = row.compute_bounds(viewport)
    if not ok:
        return False
    top = bounds.get_y()
    bottom = top + bounds.get_height()
    return top >= 0 and bottom <= viewport.get_height()


def find_anchor(window):
    """The first coloured row's drag grip on screen, or None."""
    listbox = getattr(window, 'connection_list', None)
    if listbox is None or not listbox.get_mapped():
        return None
    row = listbox.get_first_child()
    while row is not None:
        grip = getattr(row, 'drag_grip', None)
        if (grip is not None and getattr(row, 'group_rgba', None) is not None
                and grip.get_visible() and grip.get_mapped()
                and _row_on_screen(row, listbox)):
            return grip
        row = row.get_next_sibling()
    return None


def _current_mode(config) -> str:
    return str(config.get_setting('ui.group_color_display', 'dot')).lower()


def should_show(window) -> bool:
    config = window.config
    if config.get_setting(HINT_DISMISSED_KEY, False):
        return False
    if _current_mode(config) == 'grip':
        return False
    if not getattr(window, '_sidebar_visible', True):
        return False
    return find_anchor(window) is not None


def _switch(window) -> None:
    config = window.config
    config.set_setting('ui.group_color_display', 'grip')
    # Colored Grips colours the drag handles (as Settings does on choosing it).
    if not bool(config.get_setting('ui.sidebar_show_drag_handles', True)):
        config.set_setting('ui.sidebar_show_drag_handles', True)
    config.set_setting(HINT_DISMISSED_KEY, True)
    rebuild = getattr(window, 'rebuild_connection_list', None)
    if rebuild is not None:
        rebuild()


def _build_popover(window, grip):
    listbox = window.connection_list
    popover = Gtk.Popover()
    popover.add_css_class('colored-grips-hint')
    popover.set_position(Gtk.PositionType.RIGHT)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    box.set_margin_start(6)
    box.set_margin_end(6)

    title = Gtk.Label(label=_("New: Colored Grips"), xalign=0)
    title.add_css_class('heading')
    box.append(title)
    body = Gtk.Label(
        label=_("Show each group's color on its drag handle. You can change "
                "this later in Settings › Appearance."),
        xalign=0, wrap=True, max_width_chars=34,
    )
    box.append(body)

    def dismiss(*_args):
        window.config.set_setting(HINT_DISMISSED_KEY, True)
        popover.popdown()

    def switch(*_args):
        popover.popdown()
        try:
            _switch(window)
        except Exception:
            logger.error("Could not switch to Colored Grips", exc_info=True)

    dont_show = Gtk.Button(label=_("Don't Show Again"))
    dont_show.add_css_class('flat')
    dont_show.connect('clicked', dismiss)
    switch_button = Gtk.Button(label=_("Switch"))
    switch_button.add_css_class('suggested-action')
    switch_button.connect('clicked', switch)
    buttons = Gtk.Box(spacing=6)
    buttons.set_halign(Gtk.Align.END)
    buttons.append(dont_show)
    buttons.append(switch_button)
    box.append(buttons)

    popover.dont_show_button = dont_show
    popover.switch_button = switch_button
    popover.set_child(box)
    # On the list, not the row: a rebuild replaces the rows.
    popover.set_parent(listbox)
    ok, bounds = grip.compute_bounds(listbox)
    if ok:
        rect = Gdk.Rectangle()
        rect.x = int(bounds.get_x())
        rect.y = int(bounds.get_y())
        rect.width = max(1, int(bounds.get_width()))
        rect.height = max(1, int(bounds.get_height()))
        popover.set_pointing_to(rect)
    popover.connect('closed', lambda p: GLib.idle_add(_unparent, p))
    return popover, switch_button


def _unparent(popover):
    if popover.get_parent() is not None:
        popover.unparent()
    return GLib.SOURCE_REMOVE


def show_if_needed(window):
    """Show the hint now if it is due; returns the popover or None."""
    try:
        if not should_show(window):
            return None
        popover, switch_button = _build_popover(window, find_anchor(window))
        popover.popup()
        switch_button.grab_focus()
        return popover
    except Exception:
        logger.debug("colored grips hint failed", exc_info=True)
        return None


def _due_later(window) -> bool:
    """Worth another try: everything but the rows is in place."""
    try:
        config = window.config
        return (not config.get_setting(HINT_DISMISSED_KEY, False)
                and _current_mode(config) != 'grip'
                and getattr(window, '_sidebar_visible', True))
    except Exception:
        return False


def schedule(window):
    """Offer the hint once the window has mapped (first map only)."""
    attempts = [0]

    def attempt():
        attempts[0] += 1
        if show_if_needed(window) is None and attempts[0] < MAX_ATTEMPTS and _due_later(window):
            GLib.timeout_add(RETRY_MS, attempt)
        return GLib.SOURCE_REMOVE

    def on_map(*_args):
        window.disconnect(handler)
        GLib.timeout_add(SHOW_DELAY_MS, attempt)

    handler = window.connect('map', on_map)
