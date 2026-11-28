"""The file manager's transfer indicator (#1356).

Transfers used to open a progress dialog each; an Adw dialog is modal, so the
file manager was locked until it was dismissed. Transfers now run in the
background. This button in the toolbar shows how many are running and opens a
list of them, each with its progress, a Cancel button and its details (the
transfer's progress dialog, opened on demand).

The list reads each transfer's :meth:`SFTPProgressDialog.snapshot` on a
quarter-second tick while anything runs; it never touches the transfer.

As in Nautilus (nautilus-progress-indicator.c): a transfer that will take
less than two seconds in all never shows; finished ones leave the list three
seconds later unless it is open (failed ones stay until removed); a transfer
that showed and finished pulses the button and gets a toast.
"""

from __future__ import annotations

import logging
import time
from gettext import gettext as _, ngettext
from typing import Callable, List, Optional

from gi.repository import GLib, Gtk, Pango

logger = logging.getLogger(__name__)

# Finished transfers kept in the list until cleared; the oldest go first.
_MAX_FINISHED = 20
_TICK_MS = 250
# Nautilus's numbers: an operation shows once elapsed + remaining time
# passes OPERATION_MINIMUM_TIME, and finished ones go after
# REMOVE_FINISHED_OPERATIONS_TIEMOUT while nobody has the list open.
_MINIMUM_SECONDS = 2.0
_REMOVE_FINISHED_SECONDS = 3
_ATTENTION_MS = 2000
# The least the ring shows while a transfer runs.
_MINIMUM_ARC = 0.06

_css_installed = False


def _ensure_css() -> None:
    global _css_installed
    if _css_installed:
        return
    try:
        from gi.repository import Gdk

        provider = Gtk.CssProvider()
        # As Nautilus's needs-attention: the accent colour, on the button
        # node so it keeps the button's rounded shape. A grey tint on the
        # menubutton node was easy to miss (#1437), so the button also grows
        # and settles back once, then the colour fades over the two seconds.
        provider.load_from_data(b"""
@keyframes fm-transfer-attention {
    0% { }
    12% {
        background-color: @accent_bg_color;
        color: @accent_fg_color;
        transform: scale(1.3);
    }
    30% {
        background-color: @accent_bg_color;
        color: @accent_fg_color;
        transform: scale(1);
    }
    100% { }
}
.fm-transfer-attention > button {
    animation: fm-transfer-attention 2s ease-in-out;
}
""")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        _css_installed = True
    except Exception:  # pragma: no cover - headless/test doubles
        logger.debug("Transfer indicator CSS install failed", exc_info=True)


def combined_fraction(snaps) -> Optional[float]:
    """Overall progress of the running transfers, or None when unknown.

    Weighted by size, so a large transfer and a tiny one do not average to
    a half. A transfer that reports a fraction but no size counts as an
    average-sized one; one with no fraction yet is left out until it has one.
    """
    known = [
        (float(snap["fraction"]), float(snap.get("total_bytes") or 0))
        for snap in snaps
        if snap.get("state") == "running" and snap.get("fraction") is not None
    ]
    if not known:
        return None
    sizes = [size for _fraction, size in known if size > 0]
    fallback = (sum(sizes) / len(sizes)) if sizes else 1.0
    total = sum(size if size > 0 else fallback for _fraction, size in known)
    done = sum(
        fraction * (size if size > 0 else fallback) for fraction, size in known
    )
    return max(0.0, min(1.0, done / total))


def _draw_ring(cr, width: float, height: float, fraction: float, colour, line: float) -> None:
    """The ring both the toolbar button and the tab show: the done part
    clockwise from the top in *colour*, the rest of the circle at a quarter."""
    import math

    radius = (min(width, height) - line) / 2.0
    if radius <= 0:
        return
    start = -math.pi / 2
    cr.translate(width / 2.0, height / 2.0)
    cr.set_line_width(line)
    cr.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha * 0.25)
    cr.arc(0, 0, radius, 0, 2 * math.pi)
    cr.stroke()
    # Never an empty ring: the first percent of a large transfer would read
    # as nothing happening.
    shown = max(fraction, _MINIMUM_ARC)
    cr.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha)
    cr.arc(0, 0, radius, start, start + shown * 2 * math.pi)
    cr.stroke()


def progress_ring_icon(fraction: float, colour, scale: int = 1):
    """The ring as an icon, for places that take only a GIcon (a tab's
    indicator). Drawn into a texture, as Ptyxis does for its tab progress
    (ptyxis-tab.c): libadwaita has no progress for tabs."""
    import sys

    import cairo
    from gi.repository import Gdk

    scale = max(1, int(scale))
    size = 16 * scale
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    _draw_ring(cr, size, size, max(0.0, min(1.0, float(fraction))), colour, 2.0 * scale)
    surface.flush()
    # Cairo's ARGB32 is native-endian premultiplied.
    memory_format = (
        Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED
        if sys.byteorder == "little"
        else Gdk.MemoryFormat.A8R8G8B8_PREMULTIPLIED
    )
    return Gdk.MemoryTexture.new(
        size, size, memory_format,
        GLib.Bytes.new(bytes(surface.get_data())), surface.get_stride(),
    )


class _ProgressRing(Gtk.DrawingArea):
    """A ring whose outline fills clockwise from the top, as in Nautilus
    (nautilus-progress-paintable.c): the done part in the button's text
    colour, the rest of the circle at a quarter of it.

    The indicator refreshes four times a second; the ring glides to each new
    value so it does not move in steps.
    """

    _SIZE = 16
    _LINE = 2.0

    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(self._SIZE)
        self.set_content_height(self._SIZE)
        self.set_valign(Gtk.Align.CENTER)
        self._target = 0.0
        self._shown = 0.0
        self._tick: Optional[int] = None
        self._last_frame: Optional[int] = None
        self.set_draw_func(self._draw)

    def set_fraction(self, fraction: float, *, animate: bool = True) -> None:
        self._target = max(0.0, min(1.0, float(fraction)))
        if not animate or not self._animations_enabled():
            self._shown = self._target
            self._stop_tick()
            self.queue_draw()
            return
        if self._tick is None and self._shown != self._target:
            self._last_frame = None
            self._tick = self.add_tick_callback(self._on_frame)

    def _animations_enabled(self) -> bool:
        try:
            return bool(self.get_mapped()) and bool(
                self.get_settings().get_property("gtk-enable-animations")
            )
        except Exception:
            return False

    def _stop_tick(self) -> None:
        if self._tick is not None:
            self.remove_tick_callback(self._tick)
            self._tick = None

    def _on_frame(self, _widget, clock) -> bool:
        now = clock.get_frame_time()
        seconds = 0.0 if self._last_frame is None else (now - self._last_frame) / 1e6
        self._last_frame = now
        gap = self._target - self._shown
        if abs(gap) < 0.002:
            self._shown = self._target
            self._tick = None
            self.queue_draw()
            return False
        self._shown += gap * min(1.0, seconds * 8.0)
        self.queue_draw()
        return True

    def _draw(self, _area, cr, width, height) -> None:
        _draw_ring(cr, width, height, self._shown, self.get_color(), self._LINE)


class _TransferRow:
    """One transfer in the list: title, file, progress and buttons."""

    def __init__(self, transfer, indicator: "TransferIndicator") -> None:
        self.transfer = transfer
        self.last_state: Optional[str] = None
        self.last_snap: Optional[dict] = None
        self.started = time.monotonic()
        self.finished_at: Optional[float] = None
        # Hidden until it is worth showing (see TransferIndicator._worth_showing).
        self.shown = False
        self.row = Gtk.ListBoxRow()
        self.row.set_activatable(False)
        self.row.set_visible(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(10)
        outer.set_margin_end(6)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text.set_hexpand(True)
        self.title = Gtk.Label(xalign=0)
        self.title.add_css_class("heading")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.subtitle = Gtk.Label(xalign=0)
        self.subtitle.add_css_class("caption")
        self.subtitle.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.progress = Gtk.ProgressBar()
        self.detail = Gtk.Label(xalign=0)
        self.detail.add_css_class("caption")
        self.detail.add_css_class("dim-label")
        self.detail.set_ellipsize(Pango.EllipsizeMode.END)
        for widget in (self.title, self.subtitle, self.progress, self.detail):
            text.append(widget)
        outer.append(text)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        buttons.set_valign(Gtk.Align.CENTER)
        self.locate = self._button(
            "folder-open-symbolic", _("Show in Files"),
            lambda *_a: indicator.locate(self),
        )
        self.details = self._button(
            "view-more-horizontal-symbolic", _("Details"),
            lambda *_a: indicator.show_details(self),
        )
        self.cancel = self._button(
            "process-stop-symbolic", _("Cancel"),
            lambda *_a: indicator.cancel(self),
        )
        self.remove = self._button(
            "window-close-symbolic", _("Remove From List"),
            lambda *_a: indicator.remove(self),
        )
        for button in (self.locate, self.details, self.cancel, self.remove):
            buttons.append(button)
        outer.append(buttons)
        self.row.set_child(outer)

    @staticmethod
    def _button(icon: str, tooltip: str, on_click: Callable) -> Gtk.Button:
        from sshpilot import icon_utils

        button = Gtk.Button()
        icon_utils.set_button_icon(button, icon)
        button.add_css_class("flat")
        button.add_css_class("circular")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(tooltip)
        button.connect("clicked", on_click)
        return button

    def update(self, snap: dict) -> None:
        state = snap["state"]
        running = state == "running"
        title = snap.get("title") or ""
        if state == "cancelled":
            title = _("Cancelled")
        self.title.set_text(title)
        self.subtitle.set_text(snap.get("subtitle") or "")
        self.subtitle.set_visible(bool(snap.get("subtitle")))
        detail = snap.get("detail") or ""
        if state == "failed" and snap.get("error"):
            detail = snap["error"]
        self.detail.set_text(detail)
        self.detail.set_visible(bool(detail))
        self.detail.set_tooltip_text(detail or None)
        if state == "failed":
            self.detail.add_css_class("error")
        else:
            self.detail.remove_css_class("error")

        self.progress.set_visible(running)
        if running:
            fraction = snap.get("fraction")
            if fraction is None:
                self.progress.pulse()
            else:
                self.progress.set_fraction(fraction)

        self.cancel.set_visible(running)
        self.remove.set_visible(not running)
        self.locate.set_visible(state == "done" and snap.get("can_locate", False))
        self.details.set_visible(state != "cancelled")
        self.last_state = state
        self.last_snap = snap


class TransferIndicator(Gtk.MenuButton):
    """Toolbar button listing the file manager's transfers.

    Hidden while there are none. While any runs it shows a ring filling with
    their combined progress (a spinner until a size is known) and, for more
    than one, the count; once all are finished, a check (or a warning when
    one failed) until the list is cleared.
    """

    def __init__(
        self,
        *,
        on_details: Callable[[object], None],
        on_failed: Optional[Callable[[object], None]] = None,
        on_finished: Optional[Callable[[object], None]] = None,
        on_face: Optional[Callable[[str, Optional[float], Optional[str]], None]] = None,
    ) -> None:
        super().__init__()
        _ensure_css()
        # Told what the button shows, for a second indicator elsewhere (the
        # file manager's tab): ("idle" | "running" | "failed", fraction or
        # None, tooltip).
        self._on_face = on_face
        self._face_told: Optional[tuple] = None
        self._on_details = on_details
        self._on_failed = on_failed
        self._on_finished = on_finished
        self._rows: List[_TransferRow] = []
        self._tick_id: Optional[int] = None
        self._attention_id: Optional[int] = None

        self.add_css_class("flat")
        self.set_valign(Gtk.Align.CENTER)
        self.set_visible(False)
        face = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._spinner = Gtk.Spinner()
        self._ring = _ProgressRing()
        self._ring.set_visible(False)
        self._status_icon = Gtk.Image()
        self._count = Gtk.Label()
        self._count.add_css_class("numeric")
        for widget in (self._spinner, self._ring, self._status_icon, self._count):
            face.append(widget)
        self.set_child(face)

        popover = Gtk.Popover()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(8)
        content.set_margin_end(8)
        content.set_size_request(380, -1)
        header = Gtk.Label(label=_("Transfers"), xalign=0)
        header.add_css_class("heading")
        content.append(header)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_max_content_height(420)
        self._list = Gtk.ListBox()
        self._list.add_css_class("boxed-list")
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.set_child(self._list)
        content.append(scrolled)
        self._clear_button = Gtk.Button(label=_("Clear Finished"))
        self._clear_button.set_halign(Gtk.Align.END)
        self._clear_button.connect("clicked", lambda *_a: self.clear_finished())
        content.append(self._clear_button)
        popover.set_child(content)
        self.set_popover(popover)
        popover.connect("notify::visible", lambda *_a: self._ensure_tick())

    # -- the list -----------------------------------------------------------

    def add(self, transfer) -> None:
        """Track a transfer (an SFTPProgressDialog) from its start."""
        if any(row.transfer is transfer for row in self._rows):
            return
        row = _TransferRow(transfer, self)
        self._rows.insert(0, row)
        self._list.prepend(row.row)
        self._trim_finished()
        self._refresh()
        self._ensure_tick()

    def transfers(self) -> list:
        return [row.transfer for row in self._rows]

    def running_count(self) -> int:
        return sum(1 for row in self._rows if row.last_state == "running")

    def running_transfers(self) -> list:
        """Transfers still running, shown or not (a quick one too)."""
        running = []
        for row in self._rows:
            try:
                if row.transfer.snapshot()["state"] == "running":
                    running.append(row.transfer)
            except Exception:
                continue
        return running

    def _list_open(self) -> bool:
        popover = self.get_popover()
        return bool(popover is not None and popover.get_visible())

    def remove(self, row: _TransferRow) -> None:
        if row not in self._rows:
            return
        self._rows.remove(row)
        self._list.remove(row.row)
        if row.last_state != "running":
            discard = getattr(row.transfer, "discard", None)
            if callable(discard):
                discard()
        self._refresh()

    def clear_finished(self) -> None:
        for row in [r for r in self._rows if r.last_state != "running"]:
            self.remove(row)
        if not self._rows:
            self.popdown()

    def cancel(self, row: _TransferRow) -> None:
        try:
            row.transfer.cancel()
        except Exception:
            logger.debug("Cancelling a transfer failed", exc_info=True)
        self._refresh()

    def show_details(self, row: _TransferRow) -> None:
        self.popdown()
        self._on_details(row.transfer)

    def locate(self, row: _TransferRow) -> None:
        try:
            row.transfer.open_location()
        except Exception:
            logger.debug("Showing a download failed", exc_info=True)

    def _trim_finished(self) -> None:
        finished = [r for r in self._rows if r.last_state not in (None, "running")]
        for row in finished[_MAX_FINISHED:]:
            self.remove(row)

    # -- refresh --------------------------------------------------------------

    def _ensure_tick(self) -> None:
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add(_TICK_MS, self._on_tick)

    def _on_tick(self) -> bool:
        self._refresh()
        if self.running_count() == 0 and not self._pending_removal():
            self._tick_id = None
            return False
        return True

    def _pending_removal(self) -> bool:
        return any(
            row.finished_at is not None and row.last_state in ("done", "cancelled")
            for row in self._rows
        )

    @staticmethod
    def _worth_showing(row: "_TransferRow", snap: dict) -> bool:
        """Nautilus shows an operation once elapsed + remaining > 2 s."""
        if snap["state"] == "failed":
            return True
        elapsed = time.monotonic() - row.started
        if elapsed >= _MINIMUM_SECONDS:
            return True
        remaining = getattr(row.transfer, "remaining_seconds", None)
        try:
            remaining = remaining() if callable(remaining) else None
        except Exception:
            remaining = None
        return snap["state"] == "running" and remaining is not None and (
            elapsed + remaining >= _MINIMUM_SECONDS
        )

    def _attention(self) -> None:
        """Pulse the button: something worth a look just happened."""
        self.remove_css_class("fm-transfer-attention")
        self.add_css_class("fm-transfer-attention")
        if self._attention_id is not None:
            GLib.source_remove(self._attention_id)

        def _done() -> bool:
            self.remove_css_class("fm-transfer-attention")
            self._attention_id = None
            return False

        self._attention_id = GLib.timeout_add(_ATTENTION_MS, _done)

    def _refresh(self) -> None:
        now = time.monotonic()
        list_open = self._list_open()
        for row in list(self._rows):
            try:
                snap = row.transfer.snapshot()
            except Exception:
                logger.debug("Transfer snapshot failed", exc_info=True)
                continue
            previous = row.last_state
            row.update(snap)
            state = snap["state"]
            if not row.shown and self._worth_showing(row, snap):
                row.shown = True
                row.row.set_visible(True)
                if state == "running":
                    self._attention()
            if state != "running" and row.finished_at is None:
                row.finished_at = now
                if not row.shown:
                    # Over before it was worth a row: gone without a trace,
                    # as in Nautilus (the pane's refresh shows the result).
                    self.remove(row)
                    continue
                self._attention()
                callback = self._on_failed if state == "failed" else (
                    self._on_finished if state == "done" else None
                )
                if callback is not None and previous == "running" and not list_open:
                    try:
                        callback(row.transfer)
                    except Exception:
                        logger.debug("Transfer notice failed", exc_info=True)
            # Finished and cancelled ones leave on their own while the list is
            # closed; a failure stays until it is removed.
            if (
                state in ("done", "cancelled")
                and row.finished_at is not None
                and not list_open
                and now - row.finished_at >= _REMOVE_FINISHED_SECONDS
            ):
                self.remove(row)
        self._update_face()

    def _update_face(self) -> None:
        state, fraction = self._draw_face()
        if self._on_face is None:
            return
        # A whole percent at a time: the tab's icon is redrawn for each.
        tooltip = self.get_tooltip_text() if state != "idle" else None
        face = (state, None if fraction is None else int(fraction * 100), tooltip)
        if face == self._face_told:
            return
        try:
            taken = self._on_face(
                state, None if fraction is None else face[1] / 100.0, tooltip
            )
        except Exception:
            logger.debug("Transfer face callback failed", exc_info=True)
            taken = False
        # Remembered only once the tab took it: a refused face (the tab was
        # between windows) is told again on the next tick.
        if taken is not False:
            self._face_told = face

    def retell_face(self) -> None:
        """Tell the tab again, as when the light or dark style changed."""
        self._face_told = None
        self._update_face()

    def _draw_face(self) -> tuple:
        snaps = [
            row.last_snap for row in self._rows
            if row.shown and row.last_state == "running" and row.last_snap
        ]
        running = len(snaps)
        self.set_visible(any(row.shown for row in self._rows))
        self._clear_button.set_visible(any(r.last_state != "running" for r in self._rows))
        if running:
            self._status_icon.set_visible(False)
            fraction = combined_fraction(snaps)
            self._count.set_text(str(running))
            # One transfer is just the ring; the number says there are more.
            self._count.set_visible(running > 1)
            if fraction is None:
                # No size known yet: nothing to fill a ring with.
                self._ring.set_visible(False)
                self._spinner.set_visible(True)
                self._spinner.start()
                self.set_tooltip_text(
                    ngettext(
                        "{count} transfer running",
                        "{count} transfers running",
                        running,
                    ).format(count=running)
                )
                return "running", None
            self._spinner.stop()
            self._spinner.set_visible(False)
            # Glide only while the ring is already on screen; a ring that
            # just appeared starts at its value.
            self._ring.set_fraction(fraction, animate=self._ring.get_visible())
            self._ring.set_visible(True)
            self.set_tooltip_text(
                ngettext(
                    "{count} transfer running ({percent}%)",
                    "{count} transfers running ({percent}%)",
                    running,
                ).format(count=running, percent=int(fraction * 100))
            )
            return "running", fraction
        self._spinner.stop()
        self._spinner.set_visible(False)
        self._ring.set_visible(False)
        self._count.set_visible(False)
        failed = any(r.last_state == "failed" for r in self._rows)
        from sshpilot import icon_utils

        icon_utils.set_icon_from_name(
            self._status_icon,
            "dialog-warning-symbolic" if failed else "object-select-symbolic",
        )
        self._status_icon.set_visible(True)
        self.set_tooltip_text(_("Transfers"))
        return ("failed" if failed else "idle"), None

    def shutdown(self) -> None:
        """Stop refreshing (the file manager is closing)."""
        if self._attention_id is not None:
            try:
                GLib.source_remove(self._attention_id)
            except Exception:
                pass
            self._attention_id = None
        if self._tick_id is not None:
            try:
                GLib.source_remove(self._tick_id)
            except Exception:
                pass
            self._tick_id = None
