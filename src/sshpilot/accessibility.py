"""Small helpers over GTK 4's native accessibility API.

GTK already derives correct roles and names for most of the UI: a ``Gtk.Button``
with a label, an ``Adw.EntryRow`` with a title, a ``Gtk.ListBox`` of rows. This
module only covers the cases GTK cannot infer:

* icon-only buttons, whose only text is a tooltip — and GTK's tooltip fallback
  drags the keyboard shortcut into the accessible name, which then changes
  whenever the user rebinds that shortcut;
* composite rows (``ConnectionRow``, ``GroupRow``) whose text lives in child
  labels, so the row itself is exposed with an empty name;
* entries whose only text is placeholder text, which GTK does not expose.

Everything here funnels into ``Gtk.Accessible.update_property`` /
``update_state`` — no parallel widget hierarchy, no test-only attributes.

Every helper is a no-op when the widget does not implement the API (the unit
suite runs against a stubbed ``gi``), so callers never need their own guards.

There is deliberately no ``labelled-by`` helper: in GTK 4.22 / PyGObject 3.56
``Gtk.Accessible.update_relation([LABELLED_BY], [[label]])`` trips a
``g_value_get_pointer`` assertion and publishes no relation at all, so the
accessible name has to be set directly instead.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _gtk():
    """Return the real ``Gtk`` module, or ``None`` under the test stubs."""
    try:
        from gi.repository import Gtk
    except Exception:  # pragma: no cover - environment dependent
        return None
    if not getattr(Gtk.Accessible, "__module__", "").startswith("gi.repository"):
        return None  # conftest's dummy gi
    return Gtk


def set_accessible_name(widget, name: Optional[str]) -> bool:
    """Set (or clear) the accessible name exposed to AT-SPI as the node name.

    Overrides GTK's own derivation, which for an icon-only button is the
    tooltip and for a composite row is empty.

    One trap: GTK does **not** publish a name for a widget whose accessible
    role is the default generic one, so labelling a bare ``Gtk.Box`` is
    silently ignored. A container that deserves a name needs a role that
    carries one — ``TOOLBAR``, ``GROUP``, … — and ``accessible-role`` is
    construct-only, so it has to be passed to the constructor.
    """

    Gtk = _gtk()
    if Gtk is None or widget is None:
        return False
    try:
        if name:
            widget.update_property([Gtk.AccessibleProperty.LABEL], [str(name)])
        else:
            widget.reset_property(Gtk.AccessibleProperty.LABEL)
        return True
    except Exception:
        logger.debug("Could not set accessible name on %r", widget, exc_info=True)
        return False


def set_accessible_description(widget, description: Optional[str]) -> bool:
    """Set (or clear) the accessible description (AT-SPI node description)."""

    Gtk = _gtk()
    if Gtk is None or widget is None:
        return False
    try:
        if description:
            widget.update_property(
                [Gtk.AccessibleProperty.DESCRIPTION], [str(description)]
            )
        else:
            widget.reset_property(Gtk.AccessibleProperty.DESCRIPTION)
        return True
    except Exception:
        logger.debug(
            "Could not set accessible description on %r", widget, exc_info=True
        )
        return False


def set_accessible_expanded(widget, expanded: Optional[bool]) -> bool:
    """Expose an expanded/collapsed state on a widget GTK cannot infer it for.

    ``GTK_ACCESSIBLE_STATE_EXPANDED`` is a *tristate*. From PyGObject the value
    must be passed as a plain ``int`` (1/0): handing it ``True`` trips a
    ``g_value_get_int`` assertion, and handing it ``Gtk.AccessibleTristate.TRUE``
    silently degrades to ``EXPANDABLE`` without ``EXPANDED`` (verified against
    GTK 4.22 / at-spi 2.58).
    """

    Gtk = _gtk()
    if Gtk is None or widget is None:
        return False
    try:
        if expanded is None:
            widget.reset_state(Gtk.AccessibleState.EXPANDED)
        else:
            widget.update_state(
                [Gtk.AccessibleState.EXPANDED], [1 if expanded else 0]
            )
        return True
    except Exception:
        logger.debug("Could not set expanded state on %r", widget, exc_info=True)
        return False


def set_accessible_selected(widget, selected: Optional[bool]) -> bool:
    """Expose a selected/unselected state GTK cannot infer.

    Same plain-``int`` tristate rule as :func:`set_accessible_expanded`.
    """

    Gtk = _gtk()
    if Gtk is None or widget is None:
        return False
    try:
        if selected is None:
            widget.reset_state(Gtk.AccessibleState.SELECTED)
        else:
            widget.update_state(
                [Gtk.AccessibleState.SELECTED], [1 if selected else 0]
            )
        return True
    except Exception:
        logger.debug("Could not set selected state on %r", widget, exc_info=True)
        return False


def label_icon_button(
    button,
    name: str,
    *,
    tooltip: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    """Give an icon-only control a stable accessible name and a tooltip.

    ``name`` is the bare action ("New Connection"); ``tooltip`` is the visible
    hint, which may also mention the keyboard shortcut. Keeping them apart is
    the point: the tooltip changes when the user rebinds the shortcut, the
    accessible name does not, so automation and screen readers keep working.
    GTK still publishes the shortcut separately in the ``keyshortcuts``
    AT-SPI attribute.

    ``tooltip`` defaults to ``name``, because an icon-only control needs one
    either way — this is a drop-in replacement for ``set_tooltip_text(name)``,
    not a way to drop the tooltip. Where a control is deliberately without one,
    call :func:`set_accessible_name` instead.
    """

    if button is None:
        return
    try:
        button.set_tooltip_text(tooltip if tooltip is not None else name)
    except Exception:
        logger.debug("Could not set tooltip on %r", button, exc_info=True)
    set_accessible_name(button, name)
    if description is not None:
        set_accessible_description(button, description)
