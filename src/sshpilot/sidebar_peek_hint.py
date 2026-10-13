"""Start-up hint: how to peek at a sidebar hidden on startup.

Each time the main window opens with the sidebar hidden, a popover on the
header bar's sidebar button plays the hover-reveal sketch (the same one
Settings › Sidebar › Behavior shows) and explains it, until the user picks
"Don't Show Again". It is skipped while that button is turned off, since the
hover zone is the button (sidebar_hover_reveal.py).
"""

import logging
from gettext import gettext as _

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import GLib, Gtk

from .sidebar_peek_preview import SidebarPeekPreview

logger = logging.getLogger(__name__)

HINT_DISMISSED_KEY = 'ui.sidebar_peek_hint_dismissed'
# After the window maps, so the hint lands once the start-up layout settles.
SHOW_DELAY_MS = 800


def should_show(window) -> bool:
    config = window.config
    if config.get_setting(HINT_DISMISSED_KEY, False):
        return False
    if getattr(window, '_sidebar_visible', True):
        return False
    button = getattr(window, 'sidebar_toggle_button', None)
    return button is not None and button.get_mapped()


def _build_popover(button, config):
    popover = Gtk.Popover()
    popover.add_css_class('sidebar-peek-hint')
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    box.set_margin_start(6)
    box.set_margin_end(6)

    preview = SidebarPeekPreview()
    preview.set_halign(Gtk.Align.CENTER)
    box.append(preview)

    title = Gtk.Label(label=_("The Sidebar Is Hidden"), xalign=0)
    title.add_css_class('heading')
    box.append(title)
    body = Gtk.Label(
        label=_("Rest the pointer on this button to float the sidebar over "
                "the terminal, or click it to show the sidebar again."),
        xalign=0, wrap=True, max_width_chars=34,
    )
    box.append(body)

    def dismiss(*_args):
        config.set_setting(HINT_DISMISSED_KEY, True)
        popover.popdown()

    dont_show = Gtk.Button(label=_("Don't Show Again"))
    dont_show.add_css_class('flat')
    dont_show.connect('clicked', dismiss)
    got_it = Gtk.Button(label=_("Got It"))
    got_it.add_css_class('suggested-action')
    got_it.connect('clicked', lambda *_a: popover.popdown())
    buttons = Gtk.Box(spacing=6)
    buttons.set_halign(Gtk.Align.END)
    buttons.append(dont_show)
    buttons.append(got_it)
    box.append(buttons)

    popover.dont_show_button = dont_show
    popover.set_child(box)
    popover.set_parent(button)
    popover.connect('closed', lambda p: GLib.idle_add(_unparent, p))
    return popover, got_it


def _unparent(popover):
    if popover.get_parent() is not None:
        popover.unparent()
    return GLib.SOURCE_REMOVE


def show_if_needed(window):
    """Show the hint now if it is due; returns the popover or None."""
    try:
        if not should_show(window):
            return None
        popover, got_it = _build_popover(window.sidebar_toggle_button, window.config)
        popover.popup()
        got_it.grab_focus()
        return popover
    except Exception:
        logger.debug("sidebar peek hint failed", exc_info=True)
        return None


def schedule(window):
    """Offer the hint once the window has mapped (first map only)."""
    def on_map(*_args):
        window.disconnect(handler)
        GLib.timeout_add(SHOW_DELAY_MS, lambda: (show_if_needed(window), GLib.SOURCE_REMOVE)[1])

    handler = window.connect('map', on_map)
