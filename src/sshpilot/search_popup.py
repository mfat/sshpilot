"""Detachable floating sidebar popup — used for search, reusable for more.

``SearchPopup`` hosts a widget (the live connection sidebar, ``sidebar_box``) in
an overlay panel that floats above the work area. On show it reparents that
widget out of its home container into the panel; on hide it puts it back.

Because it moves the *live* widget tree rather than copying it, the popup is
pixel-identical to the docked sidebar and every behaviour (selection,
drag-and-drop, context menus, search, tags) works with zero duplication — there
is nothing to keep in sync. The home container is left in place while detached,
so the surrounding content (the terminal) never resizes.

Decoupling
----------
The popup knows nothing about *why* it is shown or what the content is. It only
touches structural pieces passed in, and delegates all behaviour to callbacks:

    SearchPopup(
        overlay,          # Gtk.Overlay to host the scrim + panel
        home,             # the container the content lives in; must support
                          #   set_content(widget|None) and get_content()
        content,          # the widget to reparent (e.g. the sidebar box)
        width_func,       # () -> int, the panel width
        on_shown=None,    # () -> None, after reparenting into the panel
        on_hidden=None,   # () -> None, after reparenting back home
        on_dismiss=None,  # () -> None on Esc/click-outside (default: hide())
    )

Callbacks are intentionally *not* wrapped in try/except: if the owner's contract
drifts, the failure should surface loudly rather than leave the popup half-set.

Public API
----------
    popup.show() / popup.hide() / popup.visible / popup.set_transparent(bool)
    popup.dismiss()

Extending with modes
--------------------
Presentation is localised: ``width_func`` decides the width and ``_build`` owns
the panel/scrim widgets and their alignment. Add a ``mode`` attribute and branch
in those two places (plus new CSS classes on the panel) for right-docked, wide,
or full-height variants — without touching the owner.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk

logger = logging.getLogger(__name__)


class SearchPopup:
    """A floating panel that detaches ``content`` over the work area."""

    def __init__(
        self,
        overlay: Gtk.Overlay,
        home,
        content: Gtk.Widget,
        width_func: Callable[[], int],
        *,
        on_shown: Optional[Callable[[], None]] = None,
        on_hidden: Optional[Callable[[], None]] = None,
        on_dismiss: Optional[Callable[[], None]] = None,
    ):
        self._overlay = overlay
        self._home = home
        self._content = content
        self._width_func = width_func
        self._on_shown = on_shown or (lambda: None)
        self._on_hidden = on_hidden or (lambda: None)
        self._on_dismiss = on_dismiss
        self._visible = False
        self._transparent = False
        self._scrim = None
        self._panel = None
        self._build()

    # -- construction -----------------------------------------------------

    def _build(self):
        """Create the hidden scrim + panel overlay layers."""
        if self._overlay is None:
            return

        # Transparent scrim behind the panel: captures a click *outside* the
        # panel to dismiss. Transparent so the content stays fully visible;
        # the panel's own shadow lifts it off the content.
        self._scrim = Gtk.Box()
        self._scrim.set_hexpand(True)
        self._scrim.set_vexpand(True)
        self._scrim.add_css_class("sidebar-popup-scrim")
        self._scrim.set_visible(False)
        scrim_click = Gtk.GestureClick()
        scrim_click.connect("pressed", lambda *_a: self.dismiss())
        self._scrim.add_controller(scrim_click)
        self._overlay.add_overlay(self._scrim)

        # Panel: left-aligned, full height, hosts the reparented content.
        self._panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._panel.set_halign(Gtk.Align.START)
        self._panel.set_valign(Gtk.Align.FILL)
        self._panel.add_css_class("sidebar-popup")
        self._panel.set_visible(False)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self._panel.add_controller(key)
        self._overlay.add_overlay(self._panel)

    # -- state ------------------------------------------------------------

    @property
    def visible(self) -> bool:
        """True while the content is detached into the popup."""
        return self._visible

    def set_transparent(self, enabled: bool) -> None:
        """Toggle a subtle background transparency on the panel.

        Programmatic only — intentionally not exposed in Preferences. Persists
        across show/hide (the class stays on the persistent panel widget).
        """
        self._transparent = bool(enabled)
        if self._panel is None:
            return
        if self._transparent:
            self._panel.add_css_class("sidebar-popup-transparent")
        else:
            self._panel.remove_css_class("sidebar-popup-transparent")

    # -- show / hide ------------------------------------------------------

    def show(self) -> None:
        """Detach ``content`` into the panel (floating). No-op if already shown
        or the popup wasn't built."""
        if self._visible or self._panel is None:
            return
        self._panel.set_size_request(self._width_func(), -1)

        # Reparent content: home -> panel.
        self._home.set_content(None)
        self._panel.append(self._content)
        self._on_shown()

        self._scrim.set_visible(True)
        self._panel.set_visible(True)
        self._visible = True

    def hide(self) -> None:
        """Re-attach ``content`` to its home, hiding the panel. No-op if not
        shown."""
        if not self._visible:
            return
        self._panel.set_visible(False)
        self._scrim.set_visible(False)

        # Reparent content: panel -> home.
        if self._content.get_parent() is self._panel:
            self._panel.remove(self._content)
        self._home.set_content(self._content)
        self._on_hidden()

        self._visible = False

    def dismiss(self) -> None:
        """Dismiss (Esc / click-outside): the owner's ``on_dismiss`` if given
        (e.g. to also tear down search state), otherwise a plain hide."""
        if self._on_dismiss is not None:
            self._on_dismiss()
        else:
            self.hide()

    # -- helpers ----------------------------------------------------------

    def _on_key(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.dismiss()
            return True
        return False
