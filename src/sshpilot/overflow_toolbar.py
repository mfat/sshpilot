"""Horizontal toolbar that moves trailing controls into a “…” popover.

Used by the connection sidebar's top and bottom chrome so the pane can shrink
below the full button-row width without clipping mid-icon: excess actions stay
reachable from a ``view-more-symbolic`` menu.

GTK4 layout is owned by ``Gtk.LayoutManager``, so a plain ``Gtk.Box`` cannot
override measure/allocate with ``do_measure``. This widget uses a small custom
layout manager that reports a low horizontal minimum (primary controls + the
overflow button) and, on allocate, hides trailing items into the popover.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import GObject, Gtk

from gettext import gettext as _

from .accessibility import label_icon_button, set_accessible_name

logger = logging.getLogger(__name__)

#: Typical flat icon button width used before the first real measure.
_FALLBACK_ITEM_WIDTH = 36


def choose_overflow(
    widths: Sequence[int],
    available: int,
    *,
    spacing: int = 6,
    overflow_width: int = _FALLBACK_ITEM_WIDTH,
) -> tuple[int, bool]:
    """Pick how many leading items stay visible for ``available`` pixels.

    ``widths`` are in priority order (index 0 kept longest). Returns
    ``(visible_count, show_overflow_button)``. When the overflow button is
    shown, at least one trailing item is considered overflowed even if
    ``visible_count`` is 0.
    """
    n = len(widths)
    if n == 0:
        return 0, False
    if available < 0:
        available = 0

    def _row_width(parts: Sequence[int]) -> int:
        if not parts:
            return 0
        return int(sum(parts) + spacing * (len(parts) - 1))

    full = _row_width(widths)
    if full <= available:
        return n, False

    for k in range(n - 1, -1, -1):
        parts = list(widths[:k]) + [overflow_width]
        if _row_width(parts) <= available:
            return k, True
    return 0, True


def _widget_label(widget: Gtk.Widget) -> str:
    tooltip = ''
    try:
        tooltip = widget.get_tooltip_text() or ''
    except Exception:
        pass
    if tooltip:
        base = tooltip.split(' (', 1)[0].strip()
        if base:
            return base
    try:
        name = widget.get_accessible_name() or ''
        if name:
            return name
    except Exception:
        pass
    return _('Action')


def _activate_widget(widget: Gtk.Widget) -> None:
    """Fire the same path a direct click on ``widget`` would."""
    try:
        if isinstance(widget, Gtk.MenuButton):
            widget.popup()
            return
    except Exception:
        logger.debug('MenuButton popup failed', exc_info=True)
    try:
        action = widget.get_action_name()
        if action:
            widget.activate()
            return
    except Exception:
        pass
    try:
        widget.activate()
        return
    except Exception:
        pass
    try:
        widget.emit('clicked')
    except Exception:
        logger.debug('Failed to activate overflowed toolbar widget', exc_info=True)


def _guess_icon_name(widget: Gtk.Widget) -> Optional[str]:
    try:
        if hasattr(widget, 'get_icon_name'):
            name = widget.get_icon_name()
            if name:
                return name
    except Exception:
        pass
    try:
        child = widget.get_child()
        if isinstance(child, Gtk.Image):
            return child.get_icon_name()
    except Exception:
        pass
    return None


class _OverflowLayout(Gtk.LayoutManager):
    """Measure/allocate for :class:`OverflowToolbar`."""

    def do_measure(self, widget, orientation, for_size):  # noqa: N802
        return widget._layout_measure(orientation, for_size)

    def do_allocate(self, widget, width, height, baseline):  # noqa: N802
        widget._layout_allocate(width, height, baseline)


class OverflowToolbar(Gtk.Widget):
    """Prioritized controls with a trailing overflow ``MenuButton``."""

    __gtype_name__ = 'SshPilotOverflowToolbar'

    def __init__(
        self,
        *,
        spacing: int = 6,
        primary_count: int = 2,
        accessible_name: Optional[str] = None,
    ) -> None:
        super().__init__(accessible_role=Gtk.AccessibleRole.TOOLBAR)
        self.set_layout_manager(_OverflowLayout())
        self.set_hexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        if accessible_name:
            set_accessible_name(self, accessible_name)

        self._spacing = spacing
        self._primary_count = max(1, int(primary_count))
        self._items: list[Gtk.Widget] = []
        self._last_visible = -1
        self._last_overflow = False
        self._last_available = -1
        self._last_overflowed_ids: tuple = ()
        self._applying = False

        self._box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=spacing,
        )
        self._box.set_hexpand(True)
        self._box.set_homogeneous(False)
        self._box.set_parent(self)

        self._overflow_btn = Gtk.MenuButton()
        self._overflow_btn.add_css_class('flat')
        self._overflow_btn.set_icon_name('view-more-symbolic')
        label_icon_button(
            self._overflow_btn,
            _('More actions'),
            tooltip=_('More actions'),
        )
        try:
            self._overflow_btn.set_can_focus(False)
        except Exception:
            pass

        self._overflow_list = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        self._overflow_list.set_margin_start(6)
        self._overflow_list.set_margin_end(6)
        self._overflow_list.set_margin_top(6)
        self._overflow_list.set_margin_bottom(6)
        popover = Gtk.Popover()
        popover.set_child(self._overflow_list)
        self._overflow_btn.set_popover(popover)
        self._overflow_btn.set_visible(False)
        self._box.append(self._overflow_btn)

    @property
    def overflow_button(self) -> Gtk.MenuButton:
        return self._overflow_btn

    def add_item(self, widget: Gtk.Widget) -> None:
        """Append a control in priority order (first = kept visible longest)."""
        widget.set_hexpand(False)
        widget.set_halign(Gtk.Align.CENTER)
        prev = self._items[-1] if self._items else None
        self._items.append(widget)
        self._box.append(widget)
        if prev is None:
            self._box.reorder_child_after(widget, None)
        else:
            self._box.reorder_child_after(widget, prev)
        self._box.reorder_child_after(self._overflow_btn, widget)
        self.queue_resize()

    def do_dispose(self):  # noqa: N802 - GTK vfunc
        box = getattr(self, '_box', None)
        if box is not None:
            box.unparent()
            self._box = None
        Gtk.Widget.do_dispose(self)

    def _item_width(self, widget: Gtk.Widget) -> int:
        """Natural width for packing.

        Invisible widgets often measure as 0 in GTK4, which would make the next
        allocate think every control fits and un-hide them (e.g. when the
        overflow ``MenuButton`` opens). Cache a real width while visible and
        reuse it for hidden items.
        """
        cached = getattr(widget, '_overflow_nat_width', None)
        try:
            minimum, natural, _, _ = widget.measure(
                Gtk.Orientation.HORIZONTAL, -1
            )
            measured = max(int(minimum), int(natural), 0)
        except Exception:
            measured = 0
        if measured > 1:
            widget._overflow_nat_width = measured  # type: ignore[attr-defined]
            return measured
        if cached:
            return int(cached)
        return _FALLBACK_ITEM_WIDTH

    def _candidates(self) -> list[Gtk.Widget]:
        return [w for w in self._items if self._item_wants_visible(w)]

    def _row_width(self, parts: Sequence[int]) -> int:
        if not parts:
            return 0
        return int(sum(parts) + self._spacing * (len(parts) - 1))

    def _ensure_width_cache(self) -> None:
        """Measure every candidate once while still visible (first layout)."""
        for widget in self._candidates():
            if getattr(widget, '_overflow_nat_width', None):
                continue
            was_visible = True
            try:
                was_visible = bool(widget.get_visible())
            except Exception:
                pass
            if not was_visible:
                try:
                    widget.set_visible(True)
                except Exception:
                    pass
            self._item_width(widget)
            if not was_visible:
                try:
                    widget.set_visible(False)
                except Exception:
                    pass

    def _primary_floor(self) -> int:
        self._ensure_width_cache()
        candidates = self._candidates()
        if not candidates:
            return self._item_width(self._overflow_btn)
        primary = candidates[: self._primary_count]
        widths = [self._item_width(w) for w in primary]
        widths.append(self._item_width(self._overflow_btn))
        return self._row_width(widths)

    def _preferred_width(self) -> int:
        self._ensure_width_cache()
        candidates = self._candidates()
        if not candidates:
            return 0
        return self._row_width([self._item_width(w) for w in candidates])

    def _layout_measure(self, orientation, for_size):
        box = getattr(self, '_box', None)
        if orientation == Gtk.Orientation.VERTICAL:
            if box is None:
                return 0, 0, -1, -1
            return box.measure(orientation, for_size)

        floor = self._primary_floor()
        natural = max(floor, self._preferred_width())
        # Margins are applied by GTK outside measure; do not add them here.
        return floor, natural, -1, -1

    def _layout_allocate(self, width, height, baseline):
        self._apply_overflow(max(0, int(width)))
        box = getattr(self, '_box', None)
        if box is not None:
            box.allocate(width, height, baseline, None)

    def _apply_overflow(self, available: int) -> None:
        if self._applying or available < 0:
            return
        self._ensure_width_cache()
        candidates = self._candidates()
        widths = [self._item_width(w) for w in candidates]
        overflow_w = self._item_width(self._overflow_btn)
        visible_count, show_overflow = choose_overflow(
            widths,
            available,
            spacing=self._spacing,
            overflow_width=overflow_w,
        )
        if (
            visible_count == self._last_visible
            and show_overflow == self._last_overflow
            and available == self._last_available
        ):
            return

        self._applying = True
        try:
            overflowed: list[Gtk.Widget] = []
            for index, widget in enumerate(candidates):
                show = index < visible_count
                try:
                    widget.set_visible(show)
                except Exception:
                    pass
                if not show:
                    overflowed.append(widget)
            for widget in self._items:
                if widget not in candidates:
                    try:
                        widget.set_visible(False)
                    except Exception:
                        pass
            self._overflow_btn.set_visible(show_overflow)
            if show_overflow:
                overflowed_ids = tuple(id(w) for w in overflowed)
                if overflowed_ids != self._last_overflowed_ids:
                    self._rebuild_overflow_menu(overflowed)
                    self._last_overflowed_ids = overflowed_ids
            else:
                self._last_overflowed_ids = ()
            self._last_visible = visible_count
            self._last_overflow = show_overflow
            self._last_available = available
        finally:
            self._applying = False

    def _item_wants_visible(self, widget: Gtk.Widget) -> bool:
        if getattr(widget, '_overflow_force_hidden', False):
            return False
        return True

    def _rebuild_overflow_menu(self, overflowed: Sequence[Gtk.Widget]) -> None:
        child = self._overflow_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._overflow_list.remove(child)
            child = nxt

        from sshpilot import icon_utils

        for widget in overflowed:
            label = _widget_label(widget)
            row = Gtk.Button()
            row.add_css_class('flat')
            row.set_sensitive(widget.get_sensitive())
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            icon_name = _guess_icon_name(widget)
            if icon_name:
                try:
                    box.append(icon_utils.new_image_from_icon_name(icon_name))
                except Exception:
                    pass
            text = Gtk.Label(label=label, xalign=0.0)
            text.set_hexpand(True)
            box.append(text)
            row.set_child(box)
            row.connect('clicked', self._on_overflow_item_clicked, widget)
            self._overflow_list.append(row)

    def _on_overflow_item_clicked(
        self, _button: Gtk.Button, target: Gtk.Widget
    ) -> None:
        try:
            popover = self._overflow_btn.get_popover()
            if popover is not None:
                popover.popdown()
        except Exception:
            pass
        _activate_widget(target)

    def force_relayout(self) -> None:
        """Recompute visibility (e.g. after an item’s force-hidden flag changes)."""
        self._last_visible = -1
        self._last_available = -1
        self._last_overflowed_ids = ()
        self.queue_resize()
        self.queue_allocate()


def mark_force_hidden(widget: Gtk.Widget, hidden: bool = True) -> None:
    """Exclude ``widget`` from overflow packing (stays hidden)."""
    widget._overflow_force_hidden = bool(hidden)  # noqa: SLF001 - intentional
    parent = widget.get_parent()
    toolbar = parent.get_parent() if parent is not None else None
    if isinstance(toolbar, OverflowToolbar):
        toolbar.force_relayout()


# Keep the layout manager type registered for introspection/GC.
GObject.type_ensure(_OverflowLayout.__gtype__)
