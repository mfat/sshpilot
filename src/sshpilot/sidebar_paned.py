"""Resizable sidebar split view.

The window used to host the connection sidebar in an ``AdwOverlaySplitView``,
which computes the sidebar width itself (a fraction of the window, clamped to a
min/max) and offers the user no way to resize it. ``SidebarPaned`` replaces it
with a ``Gtk.Paned`` so the divider can be dragged, while still answering the
split-view API the rest of the window already drives:

``pin_width`` / ``release_width``
    Freeze the sidebar at one width and let it go again — what the minimal icon
    strip and every tick of its animation need. A pin overrides the resting
    geometry without disturbing it, so releasing brings the sidebar back to the
    width it was resting at.
``get_sidebar_width`` / ``get_resting_sidebar_width``
    The live width, and the width it would rest at if released right now (which
    the strip animation and the search popup need while it is pinned).
``set_show_sidebar`` / ``get_show_sidebar``
    Sidebar visibility, mapped to the start child's visibility.
``set_sidebar`` / ``set_content``
    Aliases for the start/end child.

A width the user drags to is remembered (``user_width``) and wins over the
fraction-derived width from then on, including after the icon strip animates
back open. The owner is told about it through ``on_user_resize`` so it can be
persisted; the callback is debounced so a drag writes the setting once.
``on_drag`` fires on every divider move while the pointer is dragging (not on
layout reflow), so the owner can drop tall secondary row chrome for the
duration of the gesture.

The divider is also how the sidebar leaves the minimal strip: dragging a pinned
strip out to ``_expand_threshold()`` — the width the full sidebar needs — is a
request for the full sidebar, reported through ``on_mode_switch``, which the
window answers with ``set_sidebar_minimal``. Below that threshold a pinned strip
follows the pointer (staying in minimal mode) so the mode switch is continuous.
The way *into* the strip by drag — shoving the divider past the sidebar's floor
— is behind :data:`COLLAPSE_BY_DRAG` and currently off. Nothing else in this
widget knows what a mode is.

Overlay presentation (``AdwOverlaySplitView.collapsed``) has no ``Gtk.Paned``
equivalent — see ``docs/sidebar-modes.md``.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import GLib, Gtk

logger = logging.getLogger(__name__)

#: Space the content side keeps when the user drags the divider right.
_CONTENT_MIN_WIDTH = 320

#: Hard floor for the divider, for the case where the window is too narrow to
#: give both the sidebar its content minimum and the content side its own.
_ABSOLUTE_MIN_WIDTH = 44

#: Quiet period after the last divider move before the width is persisted.
_PERSIST_DELAY_MS = 400

#: How far past the wall a drag must go before it counts as a mode switch
#: rather than the user simply running the divider into the end of its travel.
_MODE_SWITCH_SLACK = 40

#: Whether shoving the divider past the sidebar's floor collapses to the icon
#: strip. **Off**: the answer to "the sidebar is too wide" is a full sidebar
#: that lays out narrower, not a different mode the user did not ask for, so a
#: drag into the wall now simply stops there. The strip itself is untouched —
#: ``ui.sidebar_mode``, the "When a Terminal Opens" behaviour and a restored
#: session still enter it, dragging a pinned strip open still leaves it, and
#: flipping this back to True restores the drag-in gesture.
COLLAPSE_BY_DRAG = False

#: Widest the sidebar makes itself before the user has ever sized it. Only the
#: automatic (fraction-of-window) width is capped — a dragged width is not.
DEFAULT_MAX_WIDTH = 400


def drag_ceiling(split_width: int, max_width: int) -> int:
    """Widest the user may drag the sidebar in a ``split_width``-wide split.

    The content side keeps ``_CONTENT_MIN_WIDTH``; before the first allocation
    (``split_width`` 0) the configured maximum is all there is to go on.
    """
    if split_width <= 0:
        return max(_ABSOLUTE_MIN_WIDTH, int(max_width))
    return max(_ABSOLUTE_MIN_WIDTH, int(split_width) - _CONTENT_MIN_WIDTH)


def resolve_position(
    split_width: int,
    *,
    min_width: int,
    max_width: int,
    fraction: float,
    user_width: Optional[int],
) -> int:
    """Divider position for one set of constraints.

    ``min_width`` is what the sidebar's own content needs; a sidebar whose
    content asks for more than ``max_width`` gets what it asks for, because the
    maximum only caps the width the sidebar picks *for itself*. Otherwise a
    width the user dragged to wins over the fraction-of-window default, and only
    the drag ceiling bounds it — again, the maximum does not.

    Kept as a module-level pure function so the geometry is unit testable
    without building a GTK widget.
    """
    min_width = int(min_width)
    max_width = int(max_width)
    if max_width <= min_width:
        return max(_ABSOLUTE_MIN_WIDTH, min_width)
    ceiling = drag_ceiling(split_width, max_width)
    floor = max(_ABSOLUTE_MIN_WIDTH, min(min_width, ceiling))
    if user_width is not None:
        return max(floor, min(int(user_width), ceiling))
    automatic = int(fraction * split_width) if split_width > 0 else max_width
    return max(floor, min(automatic, min(max_width, ceiling)))


class _ClipStart(Gtk.Widget):
    """Single-child bin that keeps the child's *start* edge when squeezed.

    ``Gtk.Paned`` shrinks a start child by allocating its minimum size flush
    with the divider, so what gets cut off is the left of the sidebar — exactly
    the icons the minimal strip is made of. A split view clips the other way.
    This bin lays the child out at its minimum from x=0 and hides the overflow,
    so squeezing the sidebar down to the 64px strip reveals the icons instead of
    the empty tail behind them.

    It deliberately reports *no* horizontal minimum of its own: a bin that
    forwarded the child's minimum would just be pushed off to the left by the
    paned itself, which is the very thing being fixed. The sidebar's real floor
    is enforced by :class:`SidebarPaned`, which will not let a drag go below
    what the sidebar's content needs.
    """

    __gtype_name__ = 'SshPilotClipStart'

    def __init__(self, child: Optional[Gtk.Widget] = None) -> None:
        super().__init__()
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._child: Optional[Gtk.Widget] = None
        if child is not None:
            self.set_child(child)

    def set_child(self, child: Optional[Gtk.Widget]) -> None:
        if self._child is child:
            return
        if self._child is not None:
            self._child.unparent()
        self._child = child
        if child is not None:
            child.set_parent(self)

    def get_child(self) -> Optional[Gtk.Widget]:
        return self._child

    def do_measure(self, orientation, for_size):  # noqa: D102 - GTK vfunc
        child = self._child
        if child is None:
            return (0, 0, -1, -1)
        if orientation == Gtk.Orientation.HORIZONTAL:
            _minimum, natural, _a, _b = child.measure(orientation, for_size)
            return (0, natural, -1, -1)
        # Height for the width the child will actually be given, not for the
        # squeezed width this bin was asked about.
        child_min, _n, _a, _b = child.measure(Gtk.Orientation.HORIZONTAL, -1)
        return child.measure(
            orientation, max(for_size, child_min) if for_size >= 0 else -1)

    def do_size_allocate(self, width, height, baseline):  # noqa: D102 - GTK vfunc
        if self._child is None:
            return
        minimum, _natural, _a, _b = self._child.measure(
            Gtk.Orientation.HORIZONTAL, height)
        self._child.allocate(max(width, minimum), height, baseline, None)

    def do_dispose(self):  # noqa: D102 - GTK vfunc
        # GTK4 requires children to be unparented before the parent finalizes.
        if self._child is not None:
            self._child.unparent()
            self._child = None
        Gtk.Widget.do_dispose(self)


class SidebarPaned(Gtk.Paned):
    """Horizontal ``Gtk.Paned`` that speaks the split-view width API."""

    __gtype_name__ = 'SshPilotSidebarPaned'

    def __init__(
        self,
        *,
        max_width: int = DEFAULT_MAX_WIDTH,
        fraction: float = 0.25,
        user_width: Optional[int] = None,
        on_user_resize: Optional[Callable[[int], None]] = None,
        on_mode_switch: Optional[Callable[[bool], None]] = None,
        on_drag: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.add_css_class('sidebar-paned')
        # A thin handle draws the same hairline a split view puts between the
        # sidebar and the content, so the two panes stay edge to edge; GTK gives
        # the handle a wider input area than it paints, so it stays grabbable.
        self.set_wide_handle(False)
        # The sidebar keeps its width when the window resizes; the content side
        # takes the slack. That also makes an unprompted position change a
        # divider drag rather than a reflow.
        self.set_resize_start_child(False)
        # The sidebar is wrapped in a _ClipStart bin with no minimum of its own,
        # so the paned never has to squeeze the child itself; the floor a drag
        # stops at is measured off the sidebar's content and enforced in
        # _target_position / _on_position_notify.
        self.set_shrink_start_child(True)
        self.set_resize_end_child(True)
        self.set_shrink_end_child(True)

        self._max_width = int(max_width)
        # Set while pin_width() holds the sidebar at one width (the minimal icon
        # strip and every tick of its animation). The resting geometry is left
        # untouched by a pin, so "how wide would the sidebar be if released?"
        # can still be answered while it is pinned.
        self._pinned_width: Optional[int] = None
        # Narrowest width this pin session may shrink to (the settled strip).
        # Updated by pin_width; cleared on release. Dragging the strip open
        # widens _pinned_width without raising this floor.
        self._pin_floor: Optional[int] = None
        # Last floor measured with the full sidebar laid out (see _floor).
        self._full_floor = 0
        self._fraction = float(fraction)
        self._user_width = int(user_width) if user_width else None
        self._on_user_resize = on_user_resize
        self._on_mode_switch = on_mode_switch
        self._on_drag = on_drag
        self._show_sidebar = True

        self._applying = False      # position is being set by us, not dragged
        self._allocating = False    # inside size-allocate: reflow, not a drag
        self._switching_mode = False  # inside on_mode_switch: ignore its moves
        self._last_alloc_width = 0
        self._persist_source = 0

        self.set_position(self._target_position(0))
        self.connect('notify::position', self._on_position_notify)
        self.connect('destroy', self._on_destroy)

    # --- geometry -----------------------------------------------------------

    def _pinned(self) -> bool:
        """True while pin_width() is holding the width (minimal strip)."""
        return self._pinned_width is not None

    def _drag_ceiling(self, width: int) -> int:
        """Widest the user may drag the sidebar in a ``width``-wide split."""
        return drag_ceiling(width, self._max_width)

    def _content_min_width(self) -> int:
        """The narrowest the sidebar's own content can be laid out."""
        sidebar = self.get_sidebar()
        if sidebar is None:
            return 0
        try:
            minimum, _natural, _a, _b = sidebar.measure(
                Gtk.Orientation.HORIZONTAL, -1)
            return int(minimum)
        except Exception:
            logger.debug("sidebar content measure failed", exc_info=True)
            return 0

    def _floor(self) -> int:
        """Narrowest a *drag* may leave the sidebar: what its own content needs,
        so nothing inside it ever has to be clipped.

        There is no configured minimum behind this. The sidebar used to carry
        one (180px, inherited from ``AdwOverlaySplitView``'s default
        ``min-sidebar-width``) on top of the measured minimum, which only ever
        held the divider back from a width the content was fine with.
        """
        floor = self._content_min_width()
        # Remembered for the pinned case: a strip measures its own (much
        # smaller) chrome, so while pinned this is the only way to know how
        # wide the full sidebar needs to be to come back.
        if floor > 0 and not self._pinned():
            self._full_floor = floor
        return floor

    def _expand_threshold(self, width: int) -> int:
        """How far a pinned strip must be dragged open to become the full
        sidebar: exactly the width the full sidebar needs.

        Switching any earlier would mean expanding to a width the sidebar
        cannot be laid out in, so it would have to jump — or animate — away
        from the pointer. Switching here lets the divider stay under the
        pointer the whole way.
        """
        if self._full_floor:
            return self._full_floor
        # Never measured (the app started in the strip): the width it would
        # rest at is the best guess available.
        return max(self._pinned_width or 0, self._free_position(width))

    def _free_position(self, width: int) -> int:
        """Divider position for a ``width``-wide split with no pin in effect."""
        return resolve_position(
            width,
            min_width=self._floor(),
            max_width=self._max_width,
            fraction=self._fraction,
            user_width=self._user_width,
        )

    def _target_position(self, width: int) -> int:
        """Resting divider position for a ``width``-wide split (pinned included)."""
        if self._pinned_width is not None:
            return self._pinned_width
        return self._free_position(width)

    def _sync_position(self, width: Optional[int] = None) -> None:
        if width is None:
            width = self.get_width()
        target = self._target_position(width)
        if target == self.get_position():
            return
        self._applying = True
        try:
            self.set_position(target)
        finally:
            self._applying = False

    def do_size_allocate(self, width, height, baseline):  # noqa: D102 - GTK vfunc
        self._allocating = True
        try:
            if width > 0 and width != self._last_alloc_width:
                self._last_alloc_width = width
                self._sync_position(width)
            Gtk.Paned.do_size_allocate(self, width, height, baseline)
        finally:
            self._allocating = False

    def _emit_drag(self, position: int) -> None:
        """Tell the owner the divider is being dragged (live, not debounced)."""
        if self._on_drag is None:
            return
        try:
            self._on_drag(int(position))
        except Exception:
            logger.debug('sidebar on_drag failed', exc_info=True)

    def _on_position_notify(self, *_args) -> None:
        """Clamp the divider, switch mode, and remember a dragged width."""
        if self._applying or self._switching_mode:
            return
        width = self.get_width()
        position = self.get_position()
        dragging = not self._allocating and width > 0
        if self._pinned():
            # Pulling a pinned strip open follows the pointer in minimal mode
            # up to the full sidebar's floor; crossing that floor asks for the
            # full sidebar. Adopting the drag position as the remembered width
            # is what keeps the divider under the pointer when the sidebar
            # comes back — releasing otherwise restores the width the sidebar
            # had *before* the strip, and the divider would travel there on
            # its own after the user stopped moving.
            if dragging:
                self._emit_drag(position)
                threshold = self._expand_threshold(width)
                if position >= threshold:
                    previous = self._user_width
                    self._user_width = position
                    if self._request_mode(False):
                        self._schedule_persist()
                        return
                    self._user_width = previous
                else:
                    strip_floor = self._pin_floor or _ABSOLUTE_MIN_WIDTH
                    # Stay strictly below the switch point while minimal; the
                    # branch above owns the threshold itself.
                    clamped = max(strip_floor, min(position, threshold - 1))
                    if clamped != self._pinned_width:
                        self._pinned_width = clamped
            self._sync_position(width)
            return
        if not self._show_sidebar:
            self._sync_position(width)
            return
        ceiling = self._drag_ceiling(width)
        floor = max(_ABSOLUTE_MIN_WIDTH, min(self._floor(), ceiling))
        # Shoving the divider well past the narrowest the sidebar can be laid
        # out in asks for the icon strip rather than for an impossible width —
        # while COLLAPSE_BY_DRAG is on. With it off the drag just stops at the
        # floor and the sidebar stays in full mode.
        if COLLAPSE_BY_DRAG and dragging and position < floor - _MODE_SWITCH_SLACK:
            self._emit_drag(position)
            if self._request_mode(True):
                # The owner pins the strip's *resting* width, but the pointer is
                # still on the divider: snapping there and jumping back out to
                # the pointer on the next motion event is the flicker the drag
                # is not supposed to produce. Keep the pin's floor and put the
                # strip back under the pointer, the way the pinned branch above
                # keeps it there for the rest of the drag.
                if self._pinned_width is not None:
                    strip_floor = self._pin_floor or _ABSOLUTE_MIN_WIDTH
                    threshold = self._expand_threshold(width)
                    self._pinned_width = max(
                        strip_floor, min(position, threshold - 1))
                    self._sync_position(width)
                return
        clamped = max(floor, min(position, ceiling))
        if clamped != position:
            self._applying = True
            try:
                self.set_position(clamped)
            finally:
                self._applying = False
        # A move during allocation is the layout reflowing, not the user; only a
        # drag redefines the remembered width.
        if not dragging:
            return
        self._emit_drag(clamped)
        if clamped != self._user_width:
            self._user_width = clamped
            self._schedule_persist()

    def _request_mode(self, minimal: bool) -> bool:
        """Ask the owner for the minimal strip (or the full sidebar).

        Returns True when the owner handled it, in which case the caller leaves
        the divider alone — the mode change moves it. The flag keeps the moves
        the owner makes from being read back as more drags.
        """
        if self._on_mode_switch is None:
            return False
        self._switching_mode = True
        try:
            self._on_mode_switch(minimal)
        except Exception:
            logger.debug('sidebar mode switch failed', exc_info=True)
            return False
        finally:
            self._switching_mode = False
        return True

    # --- persistence --------------------------------------------------------

    def _schedule_persist(self) -> None:
        if self._on_user_resize is None:
            return
        if self._persist_source:
            GLib.source_remove(self._persist_source)
        self._persist_source = GLib.timeout_add(_PERSIST_DELAY_MS, self._persist)

    def _persist(self) -> bool:
        self._persist_source = 0
        if self._on_user_resize is not None and self._user_width:
            try:
                self._on_user_resize(int(self._user_width))
            except Exception:
                logger.debug('Failed to persist sidebar width', exc_info=True)
        return GLib.SOURCE_REMOVE

    def _on_destroy(self, *_args) -> None:
        if self._persist_source:
            GLib.source_remove(self._persist_source)
            self._persist_source = 0

    # --- split-view compatible API -----------------------------------------

    def set_sidebar(self, widget: Optional[Gtk.Widget]) -> None:
        # Wrapped so a squeeze keeps the sidebar's leading edge (see _ClipStart).
        if widget is None:
            self.set_start_child(None)
            return
        holder = self.get_start_child()
        if not isinstance(holder, _ClipStart):
            holder = _ClipStart()
            self.set_start_child(holder)
        holder.set_child(widget)
        # Visibility may have been decided (hide-on-startup) before the sidebar
        # widget existed; apply it to the holder that carries it.
        holder.set_visible(self._show_sidebar)

    def get_sidebar(self) -> Optional[Gtk.Widget]:
        holder = self.get_start_child()
        if isinstance(holder, _ClipStart):
            return holder.get_child()
        return holder

    def set_content(self, widget: Optional[Gtk.Widget]) -> None:
        self.set_end_child(widget)

    def get_content(self) -> Optional[Gtk.Widget]:
        return self.get_end_child()

    def set_show_sidebar(self, show: bool) -> None:
        show = bool(show)
        self._show_sidebar = show
        child = self.get_start_child()
        if child is not None:
            child.set_visible(show)
        if show:
            self._sync_position()

    def get_show_sidebar(self) -> bool:
        child = self.get_start_child()
        if child is not None:
            return bool(child.get_visible())
        return self._show_sidebar

    def pin_width(self, width: float) -> None:
        """Freeze the sidebar at exactly ``width`` px.

        Used by the minimal icon strip and every tick of the animation into and
        out of it. Pinning does not disturb the width the sidebar rests at once
        :meth:`release_width` is called.
        """
        pinned = max(_ABSOLUTE_MIN_WIDTH, int(width))
        self._pinned_width = pinned
        # The strip's shrink floor is the narrowest pin in this session, so a
        # collapse animation (full → strip) settles it and a later drag-open
        # cannot raise it.
        if self._pin_floor is None:
            self._pin_floor = pinned
        else:
            self._pin_floor = min(self._pin_floor, pinned)
        self._sync_position()

    def release_width(self) -> None:
        """Let the sidebar rest at its own width again after :meth:`pin_width`."""
        self._pinned_width = None
        self._pin_floor = None
        self._sync_position()

    def get_sidebar_width(self) -> int:
        """Current sidebar width in pixels (the divider position)."""
        return int(self.get_position())

    def get_resting_sidebar_width(self) -> int:
        """Width the sidebar returns to when the pin is released.

        Answered from the resting geometry even while the width is pinned, so
        the minimal strip can animate straight to the width it will rest at.
        """
        return int(self._free_position(self.get_width()))

    @property
    def user_width(self) -> Optional[int]:
        """The width the user last dragged to, if any."""
        return self._user_width

    # Overlay presentation is an AdwOverlaySplitView feature with no Gtk.Paned
    # equivalent; the sidebar is always a side-by-side column here.
    def set_collapsed(self, collapsed: bool) -> None:
        return None

    def get_collapsed(self) -> bool:
        return False
