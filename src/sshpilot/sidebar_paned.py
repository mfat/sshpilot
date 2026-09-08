"""Resizable sidebar split view.

The window used to host the connection sidebar in an ``AdwOverlaySplitView``,
which computes the sidebar width itself (a fraction of the window, clamped to a
min/max) and offers the user no way to resize it. ``SidebarPaned`` replaces it
with a ``Gtk.Paned`` so the divider can be dragged, while still answering the
split-view API the rest of the window already drives:

``pin_width`` / ``release_width``
    Freeze the sidebar at one width and let it go again — what the minimal icon
    strip and every tick of its animation need. Pinning drives the width bounds
    together; releasing restores the range they had before, so the sidebar comes
    back to the width it was resting at.
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

#: Hard floor for a dragged sidebar, whatever the configured minimum is.
_ABSOLUTE_MIN_WIDTH = 44

#: Quiet period after the last divider move before the width is persisted.
_PERSIST_DELAY_MS = 400

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

    ``min_width >= max_width`` means the width levers are pinned together (the
    minimal icon strip and the animation into and out of it), so the position is
    exactly that width. Otherwise a width the user dragged to wins over the
    fraction-of-window default, and only the drag ceiling bounds it — the
    configured maximum caps the automatic width, not the user's own choice.

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
        min_width: int = 180,
        max_width: int = DEFAULT_MAX_WIDTH,
        fraction: float = 0.25,
        user_width: Optional[int] = None,
        on_user_resize: Optional[Callable[[int], None]] = None,
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
        # stops at is enforced in _target_position / _on_position_notify.
        self.set_shrink_start_child(True)
        self.set_resize_end_child(True)
        self.set_shrink_end_child(True)

        self._min_width = int(min_width)
        self._max_width = int(max_width)
        # The range the sidebar is free to rest in. pin_width() drives the pair
        # together to freeze the width, so the free range is kept aside to
        # answer "how wide would the sidebar be if released?" while pinned.
        self._free_min = self._min_width
        self._free_max = self._max_width
        self._fraction = float(fraction)
        self._user_width = int(user_width) if user_width else None
        self._on_user_resize = on_user_resize
        self._show_sidebar = True

        self._applying = False      # position is being set by us, not dragged
        self._allocating = False    # inside size-allocate: reflow, not a drag
        self._last_alloc_width = 0
        self._persist_source = 0

        self.set_position(self._target_position(0))
        self.connect('notify::position', self._on_position_notify)
        self.connect('destroy', self._on_destroy)

    # --- geometry -----------------------------------------------------------

    def _pinned(self) -> bool:
        """True while the width levers are clamped shut (minimal strip)."""
        return self._max_width <= self._min_width

    def _drag_ceiling(self, width: int) -> int:
        """Widest the user may drag the sidebar in a ``width``-wide split."""
        return drag_ceiling(width, max(self._free_max, self._max_width))

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
        """Narrowest a *drag* may leave the sidebar: never so narrow that its
        own content would have to be clipped."""
        return max(self._min_width, self._content_min_width())

    def _free_position(self, width: int) -> int:
        """Divider position for a ``width``-wide split with the levers released."""
        floor = max(self._free_min, self._content_min_width())
        return resolve_position(
            width,
            min_width=floor,
            max_width=max(self._free_max, floor + 1),
            fraction=self._fraction,
            user_width=self._user_width,
        )

    def _target_position(self, width: int) -> int:
        """Resting divider position for a ``width``-wide split (pinned included)."""
        if self._pinned():
            return max(_ABSOLUTE_MIN_WIDTH, self._min_width)
        floor = self._floor()
        return resolve_position(
            width,
            min_width=floor,
            max_width=max(self._max_width, floor + 1),
            fraction=self._fraction,
            user_width=self._user_width,
        )

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

    def _on_position_notify(self, *_args) -> None:
        """Clamp the divider and remember a width the user dragged to."""
        if self._applying:
            return
        width = self.get_width()
        position = self.get_position()
        if self._pinned() or not self._show_sidebar:
            self._sync_position(width)
            return
        ceiling = self._drag_ceiling(width)
        floor = max(_ABSOLUTE_MIN_WIDTH, min(self._floor(), ceiling))
        clamped = max(floor, min(position, ceiling))
        if clamped != position:
            self._applying = True
            try:
                self.set_position(clamped)
            finally:
                self._applying = False
        # A move during allocation is the layout reflowing, not the user; only a
        # drag redefines the remembered width.
        if self._allocating or width <= 0:
            return
        if clamped != self._user_width:
            self._user_width = clamped
            self._schedule_persist()

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
        width = max(_ABSOLUTE_MIN_WIDTH, int(width))
        self._min_width = width
        self._max_width = width
        self._sync_position()

    def release_width(self) -> None:
        """Let the sidebar rest at its own width again after :meth:`pin_width`."""
        self._min_width = self._free_min
        self._max_width = self._free_max
        self._sync_position()

    def get_sidebar_width(self) -> int:
        """Current sidebar width in pixels (the divider position)."""
        return int(self.get_position())

    def get_resting_sidebar_width(self) -> int:
        """Width the sidebar returns to when the width levers are released.

        Answered from the free range even while the width is pinned, so the
        minimal strip can animate straight to the width it will rest at.
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
