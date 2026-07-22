"""Detachable floating popup — used for search, reusable for more.

``SearchPopup`` hosts a widget (the live connection sidebar, ``sidebar_box``) in
an overlay panel that floats above the work area. On show it reparents that
widget out of its home container into the panel; on hide it puts it back.

Because it moves the *live* widget tree rather than copying it, the popup is
pixel-identical to the docked sidebar and every behaviour works with zero
duplication. The home container is left in place while detached, so the
surrounding content (the terminal) never resizes.

Presentation API
----------------
The panel's *placement* is configurable and composable; *content* stays the
owner's job (the ``on_shown`` callback can read ``popup.search_only`` to hide the
list for a spotlight look).

    popup.set_position(Position.LEFT | RIGHT | CENTER | TOP)
    popup.set_size(width=None, height=None)     # None -> derive (width_func / fill)
    popup.set_backdrop(Backdrop.NONE | DIM)     # scrim behind the panel
    popup.set_transparent(bool)                 # subtle panel transparency
    popup.apply_preset('sidebar' | 'center' | 'spotlight')
    popup.mode          # active preset name
    popup.search_only   # bool: this mode wants the list hidden

Lifecycle
---------
    popup.show() / popup.hide() / popup.visible / popup.dismiss()

Decoupling
----------
The popup knows nothing about *why* it's shown or what the content is. It only
touches structural pieces passed in, and delegates behaviour to callbacks:
``on_shown`` / ``on_hidden`` (content) and ``on_dismiss`` (Esc/click-outside).
``focus_func`` (optional) returns the widget to focus once shown. Callbacks are
deliberately *not* wrapped in try/except so a drifted contract fails loudly.

Extending
---------
Add a preset to ``_PRESETS`` and, if it needs a new placement, a ``Position`` or
``Backdrop`` value. Real backdrop blur is intentionally omitted — GTK4 has no
``backdrop-filter`` and a true blur needs per-frame custom snapshot rendering;
``DIM`` is the practical stand-in.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib

logger = logging.getLogger(__name__)

_TRANSITION_DURATION_MS = 280


class Position:
    LEFT = "left"
    RIGHT = "right"
    CENTER = "center"
    TOP = "top"


class Backdrop:
    NONE = "none"
    DIM = "dim"


# position -> (halign, valign, top-margin)
_ALIGN = {
    Position.LEFT: (Gtk.Align.START, Gtk.Align.FILL, 0),
    Position.RIGHT: (Gtk.Align.END, Gtk.Align.FILL, 0),
    Position.CENTER: (Gtk.Align.CENTER, Gtk.Align.CENTER, 0),
    Position.TOP: (Gtk.Align.CENTER, Gtk.Align.START, 48),
}

# name -> presentation config. width/height None means "derive".
_PRESETS = {
    "sidebar": dict(position=Position.LEFT, width=None, height=None,
                    backdrop=Backdrop.NONE, search_only=False, show_groups=True),
    "center": dict(position=Position.CENTER, width=520, height=560,
                   backdrop=Backdrop.DIM, search_only=False, show_groups=True),
    "spotlight": dict(position=Position.TOP, width=560, height=None,
                      backdrop=Backdrop.DIM, search_only=True, show_groups=False),
}


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
        focus_func: Optional[Callable[[], Optional[Gtk.Widget]]] = None,
    ):
        self._overlay = overlay
        self._home = home
        self._content = content
        self._width_func = width_func
        self._on_shown = on_shown or (lambda: None)
        self._on_hidden = on_hidden or (lambda: None)
        self._on_dismiss = on_dismiss
        self._focus_func = focus_func

        self._visible = False
        self._transparent = False
        self._scrim = None
        self._panel = None
        self._revealer = None
        self._hiding = False

        # Presentation state (defaults to the 'sidebar' preset — today's look).
        self._mode = "sidebar"
        self._position = Position.LEFT
        self._width = None
        self._height = None
        self._backdrop = Backdrop.NONE
        self._search_only = False
        self._show_groups = True

        self._build()

    # -- construction -----------------------------------------------------

    def _build(self):
        """Create the hidden scrim + panel overlay layers."""
        if self._overlay is None:
            return

        self._scrim = Gtk.Box()
        self._scrim.set_hexpand(True)
        self._scrim.set_vexpand(True)
        self._scrim.add_css_class("sidebar-popup-scrim")
        self._scrim.set_visible(False)
        scrim_click = Gtk.GestureClick()
        scrim_click.connect("pressed", lambda *_a: self.dismiss())
        self._scrim.add_controller(scrim_click)
        self._overlay.add_overlay(self._scrim)

        self._panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._panel.add_css_class("sidebar-popup")
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self._panel.add_controller(key)

        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_duration(_TRANSITION_DURATION_MS)
        self._revealer.set_reveal_child(False)
        self._revealer.set_child(self._panel)
        self._revealer.connect("notify::child-revealed", self._on_reveal_complete)
        self._overlay.add_overlay(self._revealer)

        self._apply_layout()
        self._apply_backdrop()

    # -- presentation -----------------------------------------------------

    @property
    def mode(self) -> str:
        """Active preset name."""
        return self._mode

    @property
    def search_only(self) -> bool:
        """True when the active mode wants the list hidden (search box only)."""
        return self._search_only

    @property
    def show_groups(self) -> bool:
        """Whether group headers appear in the list/results while detached.

        Owner-honoured: when False the results are a flat connection list.
        """
        return self._show_groups

    def set_show_groups(self, enabled: bool) -> None:
        self._show_groups = bool(enabled)

    def set_position(self, position: str) -> None:
        self._position = position
        self._apply_layout()

    def set_size(self, width: Optional[int] = None, height: Optional[int] = None) -> None:
        """Panel size. ``None`` derives: width from ``width_func``, height fills."""
        self._width = width
        self._height = height
        self._apply_layout()

    def set_backdrop(self, backdrop: str) -> None:
        self._backdrop = backdrop
        self._apply_backdrop()

    def apply_preset(self, name: str) -> None:
        """Apply a named placement preset (see ``_PRESETS``)."""
        cfg = _PRESETS.get(name)
        if cfg is None:
            return
        self._mode = name
        self._position = cfg["position"]
        self._width = cfg["width"]
        self._height = cfg["height"]
        self._backdrop = cfg["backdrop"]
        self._search_only = cfg["search_only"]
        self._show_groups = cfg["show_groups"]
        self._apply_layout()
        self._apply_backdrop()

    def set_transparent(self, enabled: bool) -> None:
        """Toggle a subtle background transparency on the panel (code-only)."""
        self._transparent = bool(enabled)
        if self._panel is None:
            return
        if self._transparent:
            self._panel.add_css_class("sidebar-popup-transparent")
        else:
            self._panel.remove_css_class("sidebar-popup-transparent")

    def _apply_layout(self):
        if self._panel is None:
            return
        halign, valign, top_margin = _ALIGN.get(
            self._position, (Gtk.Align.START, Gtk.Align.FILL, 0))
        target = self._revealer if self._revealer is not None else self._panel
        target.set_halign(halign)
        target.set_valign(valign)
        target.set_margin_top(top_margin)
        if self._revealer is not None:
            transitions = {
                Position.LEFT: Gtk.RevealerTransitionType.SLIDE_RIGHT,
                Position.RIGHT: Gtk.RevealerTransitionType.SLIDE_LEFT,
                Position.TOP: Gtk.RevealerTransitionType.SLIDE_DOWN,
                Position.CENTER: Gtk.RevealerTransitionType.CROSSFADE,
            }
            self._revealer.set_transition_type(
                transitions.get(self._position, Gtk.RevealerTransitionType.CROSSFADE))
        width = self._width if self._width is not None else self._width_func()
        height = self._height if self._height is not None else -1
        self._panel.set_size_request(width, height)

    def _apply_backdrop(self):
        if self._scrim is None:
            return
        if self._backdrop == Backdrop.DIM:
            self._scrim.add_css_class("sidebar-popup-scrim-dim")
        else:
            self._scrim.remove_css_class("sidebar-popup-scrim-dim")

    # -- lifecycle --------------------------------------------------------

    @property
    def visible(self) -> bool:
        """True while the content is detached into the popup."""
        return self._visible

    def show(self) -> None:
        """Detach ``content`` into the panel (floating). No-op if already shown
        or the popup wasn't built."""
        if self._visible or self._panel is None:
            return
        self._apply_layout()
        self._hiding = False

        # Reparent content: home -> panel.
        self._home.set_content(None)
        self._panel.append(self._content)
        self._on_shown()

        self._scrim.set_visible(True)
        if self._revealer is not None:
            self._revealer.set_reveal_child(True)
        else:
            self._panel.set_visible(True)
        self._visible = True

        if self._focus_func is not None:
            widget = self._focus_func()
            if widget is not None:
                GLib.idle_add(widget.grab_focus)

    def hide(self) -> None:
        """Re-attach ``content`` to its home, hiding the panel. No-op if not
        shown."""
        if not self._visible:
            return
        self._scrim.set_visible(False)

        if self._revealer is not None:
            self._hiding = True
            self._revealer.set_reveal_child(False)
            return

        self._finish_hide()

    def _finish_hide(self) -> None:
        """Reattach content after the closing transition has finished."""
        if not self._visible:
            return
        if self._revealer is None:
            self._panel.set_visible(False)

        # Reparent content: panel -> home.
        if self._content.get_parent() is self._panel:
            self._panel.remove(self._content)
        self._home.set_content(self._content)
        self._on_hidden()

        self._visible = False
        self._hiding = False

    def _on_reveal_complete(self, revealer, _param) -> None:
        if not self._hiding or revealer.get_child_revealed():
            return
        self._finish_hide()

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
