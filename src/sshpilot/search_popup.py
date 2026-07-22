"""Detachable floating sidebar popup — used for search, reusable for more.

``SearchPopup`` hosts the *live* connection sidebar (``window._sidebar_box`` —
header actions, search, connection list, toolbar) in an overlay panel that
floats above the work area. On show it reparents ``sidebar_box`` out of the
split view's ``Adw.ToolbarView`` into the panel; on hide it puts it back.

Because it is the *same* widget tree, the popup is pixel-identical to the
expanded sidebar and every behaviour (selection, drag-and-drop, context menus,
search, tags) works with zero duplication. The split view's sidebar column is
left in place while detached (its ToolbarView just loses its content), so the
terminal never resizes — the whole reason this exists instead of collapsing the
split view to an overlay, which unavoidably reflows the content by the sidebar
width.

Public API
----------
    popup = SearchPopup(window)     # builds hidden scrim + panel on the overlay
    popup.show()                    # detach sidebar_box into the panel
    popup.hide()                    # re-attach it to the split view
    popup.visible                   # -> bool
    popup.set_transparent(enabled)  # subtle background transparency (code-only)
    popup.dismiss()                 # Esc / click-outside routing

Dismissal is automatic on Esc and on a click outside the panel (a transparent
scrim captures those); ``dismiss()`` routes through the window's search teardown
when search is active so the filter/entry are cleaned up too.

Extending with modes
--------------------
Presentation is deliberately localised: ``_target_width()`` decides the panel
width and ``_build()`` owns the panel/scrim widgets and their alignment. Add a
``mode`` attribute and branch in those two places (plus new CSS classes on the
panel) to introduce e.g. right-docked, wide, or full-height variants without
touching the window.
"""

from __future__ import annotations

import logging

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, Adw

logger = logging.getLogger(__name__)

_HAS_OVERLAY_SPLIT = hasattr(Adw, "OverlaySplitView")
_DEFAULT_MAX_SIDEBAR_WIDTH = 400


class SearchPopup:
    """A floating panel that detaches the sidebar over the work area."""

    def __init__(self, window):
        self._window = window
        self._overlay = getattr(window, "_content_overlay", None)
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
        # panel to dismiss. Transparent so the terminal stays fully visible;
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

        # Panel: left-aligned, full height, hosts the reparented sidebar_box.
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
        """True while the sidebar is detached into the popup."""
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
        """Detach the sidebar into the panel (full content, floating).

        No-op if already shown or the required widgets aren't available.
        """
        if self._visible:
            return
        win = self._window
        box = getattr(win, "_sidebar_box", None)
        tv = getattr(win, "_sidebar_toolbar_view", None)
        if box is None or tv is None or self._panel is None:
            return

        self._panel.set_size_request(self._target_width(), -1)

        # Reparent sidebar_box: split view ToolbarView -> popup panel.
        try:
            tv.set_content(None)
        except Exception:
            pass
        self._panel.append(box)

        # The popup always shows the full sidebar, even when the strip is minimal.
        try:
            win._set_sidebar_clipping(False)
            win._apply_sidebar_minimal_chrome(False)
            win._apply_sidebar_minimal_rows(False)
        except Exception:
            logger.debug("search popup content restore failed", exc_info=True)

        self._scrim.set_visible(True)
        self._panel.set_visible(True)
        self._visible = True

    def hide(self) -> None:
        """Re-attach the sidebar to the split view, hiding the panel.

        No-op if not shown. Re-collapses the strip if minimal mode is active.
        """
        if not self._visible:
            return
        win = self._window
        box = getattr(win, "_sidebar_box", None)
        tv = getattr(win, "_sidebar_toolbar_view", None)

        if self._panel is not None:
            self._panel.set_visible(False)
        if self._scrim is not None:
            self._scrim.set_visible(False)

        # Reparent sidebar_box back: popup panel -> split view ToolbarView.
        if box is not None and self._panel is not None and box.get_parent() is self._panel:
            self._panel.remove(box)
        if box is not None and tv is not None:
            try:
                tv.set_content(box)
            except Exception:
                pass

        # Restore the strip if the resting sidebar is minimal.
        if getattr(win, "_sidebar_minimal", False):
            try:
                win._apply_sidebar_minimal_chrome(True)
                win._apply_sidebar_minimal_rows(True)
            except Exception:
                logger.debug("search popup re-collapse failed", exc_info=True)

        self._visible = False

    def dismiss(self) -> None:
        """Dismiss (Esc / click-outside). Routes through the window's search
        teardown when search is active so the filter/entry are cleaned too."""
        win = self._window
        if getattr(win, "search_container", None) and win.search_container.get_visible():
            win._close_search_if_open()
        else:
            self.hide()

    # -- helpers ----------------------------------------------------------

    def _on_key(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.dismiss()
            return True
        return False

    def _target_width(self) -> int:
        """The panel width: the *actual* expanded-sidebar width (fraction-based,
        clamped to [base_min, max]), not the raw max — otherwise the panel is
        wider than the sidebar ever is side-by-side."""
        saved_max = self._effective_max_width()
        try:
            sv = getattr(self._window, "split_view", None)
            if sv is not None and hasattr(sv, "get_sidebar_width_fraction"):
                base_min = 180 if _HAS_OVERLAY_SPLIT else 200
                win_w = sv.get_width() or 0
                if win_w > 0:
                    frac = sv.get_sidebar_width_fraction()
                    return max(base_min, min(saved_max, int(frac * win_w)))
        except Exception:
            pass
        return saved_max

    def _effective_max_width(self) -> int:
        value = self._window.config.get_setting("ui.max-sidebar-width", None)
        if value is None:
            return _DEFAULT_MAX_SIDEBAR_WIDTH
        try:
            return int(value)
        except (TypeError, ValueError):
            return _DEFAULT_MAX_SIDEBAR_WIDTH
