"""Modern HIG-compliant SFTP transfer progress dialog.

Subclasses ``Adw.AlertDialog`` when available (libadwaita ≥ 1.5) and falls
back to ``Adw.MessageDialog`` on older systems. The heading names the
operation and the body names the route ("From US to this computer"); the
extra child is one boxed-list card holding the current file with its
progress, then the source and destination rows. Cancel / Done stay as
compact body buttons (a single Adw response would span the full footer).
UI updates are paced from a ``GLib.timeout`` so per-chunk progress callbacks
never touch widgets directly — they only mutate state that the render tick
reads.
"""

from __future__ import annotations

import collections
import logging
import os
import time
from gettext import gettext as _, ngettext
from typing import Optional

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .format_utils import safe_display_text
from .portal_docs import open_in_file_manager, resolve_download_locate_path


logger = logging.getLogger(__name__)


# Prefer Adw.AlertDialog (libadwaita ≥ 1.5, May 2024) — Adw.MessageDialog is
# deprecated since 1.6. Fall back to MessageDialog on older systems so the
# file manager still works there. The two classes differ in:
#   * constructor: AlertDialog uses ``heading=``, MessageDialog uses ``title=``
#   * setter: ``set_heading`` vs ``set_title``
#   * present: AlertDialog takes a parent widget; MessageDialog uses
#     set_transient_for + set_modal then present()
# All three differences are bridged below.
_HAS_ALERT_DIALOG = hasattr(Adw, "AlertDialog")
_PROGRESS_DIALOG_BASE = Adw.AlertDialog if _HAS_ALERT_DIALOG else Adw.MessageDialog

# Which side of the route is this computer. Copy / move / delete run on the
# server, so both paths belong to the host.
_LOCAL_SOURCE_OPERATIONS = frozenset({"upload"})
_LOCAL_DESTINATION_OPERATIONS = frozenset({"download"})


def _file_icon(name: str) -> Gio.Icon:
    """Themed mimetype icon for *name*, guessed from its extension."""
    try:
        content_type, _uncertain = Gio.content_type_guess(name or None, None)
        icon = Gio.content_type_get_icon(content_type)
        if icon is not None:
            return icon
    except (TypeError, GLib.Error):
        pass
    return Gio.ThemedIcon.new("text-x-generic")


class SFTPProgressDialog(_PROGRESS_DIALOG_BASE):
    """Modern GNOME HIG-compliant SFTP file transfer progress dialog.

    Subclasses ``Adw.AlertDialog`` when available (libadwaita ≥ 1.5) and
    falls back to the deprecated ``Adw.MessageDialog`` on older systems.
    No HeaderBar / Adw response footer — Cancel and Done are compact buttons
    in the extra-child (a lone AlertDialog response spans the full width).

    UI is driven from a fixed-cadence ``GLib.timeout`` (the canonical GTK
    ProgressBar pattern). The worker's per-chunk callbacks only mutate a few
    state fields and push samples into a sliding window — they do not touch
    widgets. The render tick reads that state and updates labels + bar at
    ~4 Hz, which keeps the UI responsive without burning cycles on transfers
    that emit thousands of callbacks per second.
    """

    _CARD_WIDTH = 372
    _RENDER_INTERVAL_MS = 250
    _SPEED_WINDOW_SECONDS = 5.0

    def __init__(self, parent=None, operation_type="transfer", host_label=None):
        self.operation_type = operation_type
        self.total_files = 0
        self._host_label = (
            safe_display_text(str(host_label).strip()) if host_label else ""
        ) or _("the server")
        title = self._running_heading()
        body = self._route_summary()

        # Different constructor kwargs for the two base classes.
        if _HAS_ALERT_DIALOG:
            super().__init__(heading=title, body=body)
        else:
            super().__init__(title=title, body=body)
            # MessageDialog is a Gtk.Window — old API: set transient + modal.
            if parent is not None:
                try:
                    self.set_transient_for(parent)
                except Exception:
                    pass
            try:
                self.set_modal(True)
            except Exception:
                pass

        # Transfer state
        self.is_cancelled = False
        self.current_file = ""
        self.files_completed = 0
        self._current_future = None
        self._futures = []  # Track all futures for multi-file operations
        self._completion_shown = False
        self._failed_files = []

        # Tracks whether the dialog is currently presented. Used to make
        # close() idempotent so that the shutdown cleanup path (which calls
        # close again after the user has dismissed the dialog) doesn't trip
        # the "trying to close a dialog that's not presented" Adwaita-CRITICAL.
        self._closed = False

        # Bytes-driven speed/ETA state (populated by progress-bytes signal).
        # Always read/written on the main thread.
        self._transferred_bytes = 0
        self._total_bytes = 0
        # Sliding window of (monotonic_time, transferred_bytes) samples used
        # to compute the *recent* throughput rather than the lifetime average.
        self._byte_samples: "collections.deque[tuple[float, int]]" = collections.deque()

        # Latest fraction/message/file pushed by ``update_progress``. The
        # render tick reads these — neither the worker callback nor the
        # signal handler touches widgets directly.
        self._latest_fraction: Optional[float] = None
        self._latest_message: Optional[str] = None
        self._latest_file: Optional[str] = None
        self._source_path: Optional[str] = None
        self._destination_path: Optional[str] = None
        self._locate_path: Optional[str] = None

        self._build_ui()

        # Start the render loop. Returning False from _render_tick removes the
        # source; we also clear the id on dialog close as a belt-and-braces.
        self._render_timeout_id: Optional[int] = GLib.timeout_add(
            self._RENDER_INTERVAL_MS, self._render_tick
        )

    def _running_heading(self) -> str:
        """Heading while the operation runs; transfers name their file count."""
        count = max(1, self.total_files)
        if self.operation_type == "download":
            return ngettext(
                "Downloading {count} File", "Downloading {count} Files", count
            ).format(count=count)
        if self.operation_type == "upload":
            return ngettext(
                "Uploading {count} File", "Uploading {count} Files", count
            ).format(count=count)
        titles = {
            "copy": _("Copying on Server"),
            "move": _("Moving on Server"),
            "delete": _("Deleting Files"),
        }
        return titles.get(self.operation_type, _("Transferring Files"))

    def _route_summary(self) -> str:
        """One line naming where the data goes, e.g. "From US to this computer"."""
        host = self._host_label
        if self.operation_type in _LOCAL_DESTINATION_OPERATIONS:
            return _("From {host} to this computer").format(host=host)
        if self.operation_type in _LOCAL_SOURCE_OPERATIONS:
            return _("From this computer to {host}").format(host=host)
        return _("On {host}").format(host=host)

    def _build_ui(self):
        """Build AlertDialog chrome: one boxed-list card + compact buttons.

        Cancel / Done / Show in Files are compact Gtk.Buttons in the body —
        not Adw responses. A single AlertDialog response spans the full
        footer width, which is too loud for a progress dialog. Esc-to-cancel
        is preserved via the ``closed`` signal.
        """
        self.connect("closed", self._on_dialog_closed)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        self.card = Gtk.ListBox()
        self.card.add_css_class("boxed-list")
        self.card.set_selection_mode(Gtk.SelectionMode.NONE)
        self.card.set_size_request(self._CARD_WIDTH, -1)
        content.append(self.card)

        # -- Current file + progress --------------------------------------
        progress_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=14,
            margin_bottom=14,
            margin_start=14,
            margin_end=14,
        )

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.file_icon = Gtk.Image.new_from_icon_name("package-x-generic")
        self.file_icon.set_pixel_size(32)
        self.file_icon.set_valign(Gtk.Align.CENTER)
        title_row.append(self.file_icon)

        name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name_box.set_hexpand(True)
        name_box.set_valign(Gtk.Align.CENTER)
        self.file_label = Gtk.Label()
        self.file_label.set_text("—")
        self.file_label.set_xalign(0.0)
        self.file_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.file_label.set_max_width_chars(1)
        self.file_label.set_hexpand(True)
        self.file_label.add_css_class("heading")
        name_box.append(self.file_label)

        self.counter_label = Gtk.Label()
        self.counter_label.set_xalign(0.0)
        self.counter_label.add_css_class("caption")
        self.counter_label.add_css_class("dim-label")
        self.counter_label.add_css_class("numeric")
        self.counter_label.set_visible(False)
        name_box.append(self.counter_label)
        title_row.append(name_box)

        self.percent_label = Gtk.Label()
        self.percent_label.set_valign(Gtk.Align.CENTER)
        self.percent_label.add_css_class("heading")
        self.percent_label.add_css_class("accent")
        self.percent_label.add_css_class("numeric")
        title_row.append(self.percent_label)
        progress_box.append(title_row)

        self.progress_bar = Gtk.ProgressBar()
        progress_box.append(self.progress_bar)

        stats_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for css in ("caption", "dim-label", "numeric"):
            stats_row.add_css_class(css)
        self.bytes_label = Gtk.Label()
        self.bytes_label.set_xalign(0.0)
        self.bytes_label.set_hexpand(True)
        self.bytes_label.set_ellipsize(Pango.EllipsizeMode.END)
        stats_row.append(self.bytes_label)
        self.speed_label = Gtk.Label()
        self.speed_label.set_text("—")
        stats_row.append(self.speed_label)
        self._stats_separator = Gtk.Label(label="·")
        stats_row.append(self._stats_separator)
        self.time_label = Gtk.Label()
        self.time_label.set_text("—")
        stats_row.append(self.time_label)
        progress_box.append(stats_row)

        self.progress_row = self._card_row(progress_box)

        # Kept for callers/tests that still write status via this label; the
        # AlertDialog body carries the visible copy.
        self.status_label = Gtk.Label()
        self.status_label.set_visible(False)
        self.status_label.set_text(_("Preparing transfer…"))

        # -- Route rows ---------------------------------------------------
        # Hidden until set_paths() so callers that never supply paths don't
        # get empty rows.
        local_source = self.operation_type in _LOCAL_SOURCE_OPERATIONS
        local_dest = self.operation_type in _LOCAL_DESTINATION_OPERATIONS
        host = self._host_label
        self.source_row, self.source_label = self._route_row(
            "computer-symbolic" if local_source else "network-server-symbolic",
            _("From this computer") if local_source
            else _("From {host}").format(host=host),
        )
        self.dest_row, self.dest_label = self._route_row(
            "computer-symbolic" if local_dest else "network-server-symbolic",
            _("To this computer") if local_dest
            else _("To {host}").format(host=host),
        )

        # Compact Cancel / Done (+ optional Show in Files). Not Adw responses
        # so they stay natural width instead of filling the footer.
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_row.set_halign(Gtk.Align.END)
        self.locate_button = Gtk.Button(label=_("Show in Files"))
        self.locate_button.set_visible(False)
        self.locate_button.set_sensitive(False)
        self.locate_button.connect("clicked", self._on_locate_clicked)
        action_row.append(self.locate_button)
        self.action_button = Gtk.Button(label=_("Cancel"))
        self.action_button.connect("clicked", self._on_action_button_clicked)
        action_row.append(self.action_button)
        content.append(action_row)

        self.set_extra_child(content)

    def _card_row(self, child: Gtk.Widget) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_child(child)
        self.card.append(row)
        return row

    def _route_row(self, icon_name: str, caption: str):
        """A card row with an icon, a dim caption and a monospace path."""
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_top=10,
            margin_bottom=10,
            margin_start=14,
            margin_end=14,
        )
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_valign(Gtk.Align.CENTER)
        box.append(icon)
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_hexpand(True)
        caption_label = Gtk.Label(label=caption)
        caption_label.set_xalign(0.0)
        caption_label.add_css_class("caption")
        caption_label.add_css_class("dim-label")
        text_box.append(caption_label)
        # Middle-ellipsis keeps both the head and the tail of a path visible.
        path_label = Gtk.Label()
        path_label.set_xalign(0.0)
        path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        path_label.set_max_width_chars(1)
        path_label.set_hexpand(True)
        path_label.add_css_class("monospace")
        text_box.append(path_label)
        box.append(text_box)
        row = self._card_row(box)
        row.set_visible(False)
        return row, path_label

    def is_reusable(self) -> bool:
        """Return True if this dialog can track another transfer in-place."""
        if self.is_cancelled or self._closed or self._completion_shown:
            return False
        try:
            return bool(self.get_visible())
        except Exception:
            return False

    def set_operation_details(self, total_files, filename=None):
        """Set the operation details"""
        # Only update total_files if it's larger (for adding more files to existing dialog)
        if total_files > self.total_files:
            self.total_files = total_files
            self._update_file_counter()
            if not self._completion_shown:
                try:
                    self._set_dialog_heading(self._running_heading())
                except (AttributeError, RuntimeError, GLib.Error):
                    pass

        if filename:
            self.current_file = safe_display_text(filename)
            self.file_label.set_text(self.current_file)
            try:
                self.file_icon.set_from_gicon(_file_icon(self.current_file))
            except (AttributeError, RuntimeError, GLib.Error):
                pass

    def _display_location(self, path: str, *, local: bool) -> str:
        """Folder the file lives in, with the home directory shortened to ~."""
        location = path.rstrip("/") or path
        if self.current_file and os.path.basename(location) == self.current_file:
            location = os.path.dirname(location) or "/"
        if local:
            home = os.path.expanduser("~").rstrip("/")
            if home and (location == home or location.startswith(home + "/")):
                location = "~" + location[len(home):]
        return safe_display_text(location)

    def set_paths(self, source: Optional[str] = None,
                  destination: Optional[str] = None) -> None:
        """Show the source and destination folders in the route rows.

        Each label is middle-ellipsized so long paths stay readable; the
        full untruncated path is exposed as a tooltip on hover.
        """
        if source:
            self._source_path = source
            self.source_label.set_text(self._display_location(
                source, local=self.operation_type in _LOCAL_SOURCE_OPERATIONS
            ))
            self.source_label.set_tooltip_text(safe_display_text(source))
            self.source_row.set_visible(True)
        if destination:
            self._destination_path = destination
            self.dest_label.set_text(self._display_location(
                destination, local=self.operation_type in _LOCAL_DESTINATION_OPERATIONS
            ))
            self.dest_label.set_tooltip_text(safe_display_text(destination))
            self.dest_row.set_visible(True)

    def _set_status_text(self, text: str) -> None:
        """Keep the hidden status label and AlertDialog body in sync."""
        try:
            self.status_label.set_text(text)
        except (AttributeError, RuntimeError, GLib.Error):
            pass
        try:
            self.set_body(text)
        except (AttributeError, RuntimeError, GLib.Error):
            pass

    def _enter_completion_actions(self, *, show_locate: bool) -> None:
        """Swap Cancel → Done and optionally reveal Show in Files."""
        try:
            self.action_button.set_label(_("Done"))
            self.action_button.add_css_class("suggested-action")
        except (AttributeError, RuntimeError, GLib.Error):
            pass
        try:
            self.locate_button.set_visible(show_locate)
            self.locate_button.set_sensitive(show_locate)
        except (AttributeError, RuntimeError, GLib.Error):
            pass

    def _on_locate_clicked(self, _button: Gtk.Button) -> None:
        """Open the completed download location in the desktop file manager."""
        target = self._locate_path
        if not target:
            return
        parent = None
        try:
            parent = self.get_root()
        except Exception:
            parent = None
        open_in_file_manager(target, parent=parent)

    def _on_action_button_clicked(self, _button: Gtk.Button) -> None:
        """Single click handler for the Cancel/Done button.

        Behaviour depends on which state the dialog is in: before completion
        the button cancels all tracked futures; after completion it just
        closes the dialog.
        """
        if self._completion_shown:
            # Already in "Done" mode — just close.
            self._stop_render_timer()
            self.close()
            return
        # Active-transfer mode — cancel everything.
        self._cancel_active_transfers()
        self._stop_render_timer()
        self.close()

    def _on_dialog_closed(self, _dialog) -> None:
        """Esc / parent-dismiss / WM-close all funnel through here.

        If the dialog is dismissed while a transfer is still running (no
        completion shown yet), treat it as a cancel so we don't leave the
        worker uploading bytes in the background.
        """
        if not self._completion_shown and not self.is_cancelled:
            self._cancel_active_transfers()
        self._stop_render_timer()
        # Mark as closed so a subsequent .close() (e.g. from the file
        # manager's shutdown cleanup) becomes a silent no-op.
        self._closed = True

    def close(self) -> bool:  # type: ignore[override]
        """Idempotent close — safe to call after the dialog is already gone.

        Adw.Dialog.close() asserts that the dialog is currently presented and
        emits an Adwaita-CRITICAL otherwise. The file manager calls close()
        from several places (action button, completion, shutdown cleanup),
        and timing means we sometimes hit a dialog that has already been
        dismissed. Bail early in that case instead of letting the assert
        fire.
        """
        if getattr(self, "_closed", False):
            return False
        self._closed = True
        try:
            return super().close()
        except Exception:
            return False

    def _cancel_active_transfers(self) -> None:
        """Flip the cancel flag and call .cancel() on every tracked future."""
        self.is_cancelled = True
        if self._current_future and hasattr(self._current_future, 'cancel'):
            try:
                self._current_future.cancel()
            except Exception:
                pass
        for future in self._futures:
            if future and hasattr(future, 'cancel') and not future.done():
                try:
                    future.cancel()
                except Exception:
                    pass

    def _stop_render_timer(self) -> None:
        """Remove the render GLib.timeout source if it's still scheduled."""
        if self._render_timeout_id is not None:
            try:
                GLib.source_remove(self._render_timeout_id)
            except Exception:
                pass
            self._render_timeout_id = None

    def update_progress(self, fraction, message=None, current_file=None):
        """Stash the latest fraction/message/file. Render tick paints it.

        Cheap: no widget access. Safe to call from any thread (we hop to the
        main loop). The caller is expected to have already computed the
        overall-progress fraction for multi-file batches — this dialog does
        not re-apply ``(files_completed + fraction) / total_files``.
        """
        GLib.idle_add(self._update_progress_state, fraction, message, current_file)

    def _update_progress_state(self, fraction, message, current_file):
        if fraction is not None:
            self._latest_fraction = fraction
        if message:
            self._latest_message = safe_display_text(message)
        if current_file:
            self.current_file = safe_display_text(current_file)
            self._latest_file = self.current_file
        return False

    def on_bytes(self, transferred: int, total: int) -> None:
        """Receive raw byte counts from the manager (main thread)."""
        try:
            transferred_int = int(transferred or 0)
            total_int = int(total or 0)
        except (TypeError, ValueError):
            return
        self._transferred_bytes = transferred_int
        if total_int > 0:
            self._total_bytes = total_int
        now = time.monotonic()
        self._byte_samples.append((now, transferred_int))
        cutoff = now - self._SPEED_WINDOW_SECONDS
        while self._byte_samples and self._byte_samples[0][0] < cutoff:
            self._byte_samples.popleft()

    def _bytes_text(self) -> str:
        """"3.2 GB of 7.6 GB" from real byte counts, else the caller's message."""
        if self._total_bytes > 0:
            return _("{done} of {total}").format(
                done=self._format_size(self._transferred_bytes),
                total=self._format_size(self._total_bytes),
            )
        if self._transferred_bytes > 0:
            return self._format_size(self._transferred_bytes)
        return self._latest_message or ""

    def _set_speed_and_time(self, speed_text: Optional[str],
                            time_text: Optional[str]) -> None:
        """Fill the right side of the stats row; the dot shows only between two."""
        self.speed_label.set_text(speed_text or "")
        self.speed_label.set_visible(bool(speed_text))
        self.time_label.set_text(time_text or "")
        self.time_label.set_visible(bool(time_text))
        separator = getattr(self, "_stats_separator", None)
        if separator is not None:
            separator.set_visible(bool(speed_text and time_text))

    def _render_tick(self) -> bool:
        """Repaint speed/ETA/progress at fixed cadence. Returns False to stop."""
        # If the dialog has been dismissed, drop the timer.
        try:
            visible = self.get_visible()
        except Exception:
            visible = False
        if not visible or self._completion_shown:
            self._render_timeout_id = None
            return False

        if self._latest_file:
            try:
                self.file_label.set_text(self._latest_file)
            except (AttributeError, RuntimeError):
                pass

        # Progress bar: pulse when we don't yet know what we're transferring,
        # otherwise show the fraction the caller computed.
        try:
            if self._total_bytes <= 0 and self._latest_fraction in (None, 0.0):
                self.progress_bar.pulse()
                self.percent_label.set_text("")
            elif self._latest_fraction is not None:
                fraction = max(0.0, min(1.0, float(self._latest_fraction)))
                self.progress_bar.set_fraction(fraction)
                self.percent_label.set_text(f"{int(fraction * 100)}%")
                if self.operation_type == "delete" and self.total_files > 0:
                    # Floor, not round: recursive deletes report a fractional
                    # share of one selected item, and rounding that up makes
                    # remaining items look finished mid-walk.
                    self.files_completed = self._delete_items_done(
                        fraction, self.total_files, self.files_completed
                    )
                    self._update_file_counter()
            self.bytes_label.set_text(self._bytes_text())
        except (AttributeError, RuntimeError):
            pass

        # Speed (sliding-window) + ETA from real byte counts.
        speed_text = None
        eta_text = None
        if len(self._byte_samples) >= 2:
            t0, b0 = self._byte_samples[0]
            t1, b1 = self._byte_samples[-1]
            dt = t1 - t0
            db = b1 - b0
            if dt > 0 and db > 0:
                bps = db / dt
                speed_text = self._format_speed(bps)
                if self._total_bytes > 0 and bps > 0:
                    remaining = max(0, self._total_bytes - self._transferred_bytes)
                    eta_text = (
                        _("Almost done…") if remaining == 0
                        else self._format_time(remaining / bps)
                    )
        try:
            self._set_speed_and_time(speed_text, eta_text)
        except (AttributeError, RuntimeError):
            pass

        return True  # keep the timer running

    @staticmethod
    def _format_speed(bps: float) -> str:
        if bps >= 1024 * 1024:
            return _("{speed:.1f} MB/s").format(speed=bps / (1024 * 1024))
        if bps >= 1024:
            return _("{speed:.1f} KB/s").format(speed=bps / 1024)
        return _("{speed} B/s").format(speed=int(bps))

    def _set_dialog_heading(self, text: str) -> None:
        """Set the dialog's primary heading on either base class."""
        if _HAS_ALERT_DIALOG:
            self.set_heading(text)
        else:
            self.set_title(text)

    @staticmethod
    def _delete_items_done(fraction: float, total_files: int, files_completed: int) -> int:
        """Map overall delete progress onto a completed-item counter.

        ``remove_many`` folds recursive tree walks into ``(base + unit) / total``
        so a half-finished directory is e.g. 0.25 of a 2-item batch. Rounding
        that to the nearest item advances the counter early; floor keeps
        ``done`` at fully completed selections only until the batch finishes.
        """
        if total_files <= 0:
            return 0
        unit = 0.0 if fraction < 0.0 else 1.0 if fraction > 1.0 else float(fraction)
        return min(total_files, max(int(files_completed), int(unit * total_files)))

    def increment_file_count(self):
        """Increment completed file counter"""
        GLib.idle_add(self._increment_file_count_ui)

    def set_files_completed(self, done: int) -> None:
        """Set the finished-file counter (main thread)."""
        self.files_completed = max(0, int(done))
        self._update_file_counter()

    def _increment_file_count_ui(self):
        """Update file counter (must be called from main thread)"""
        self.files_completed += 1
        self._update_file_counter()
        return False

    def _update_file_counter(self) -> None:
        """"File 3 of 12" under the file name; a single file needs no counter."""
        total = self.total_files
        index = min(total, self.files_completed + 1)
        if self.operation_type == "delete":
            text = ngettext(
                "Item {index} of {total}", "Item {index} of {total}", total
            ).format(index=index, total=total)
        else:
            text = ngettext(
                "File {index} of {total}", "File {index} of {total}", total
            ).format(index=index, total=total)
        self.counter_label.set_text(text)
        self.counter_label.set_visible(total > 1)

    def set_future(self, future):
        """Set the current operation future for cancellation"""
        self._current_future = future
        if future not in self._futures:
            self._futures.append(future)

    def _format_time(self, seconds):
        """Format time remaining for display"""
        if seconds > 3600:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return _("{hours}h {minutes}m remaining").format(hours=hours, minutes=minutes)
        elif seconds > 60:
            minutes = int(seconds // 60)
            return _("{minutes}m remaining").format(minutes=minutes)
        else:
            return _("{seconds}s remaining").format(seconds=int(seconds))

    def _format_size(self, size_bytes):
        """Format file size for display"""
        if size_bytes >= 1024 * 1024 * 1024:  # GB
            return _("{size:.1f} GB").format(size=size_bytes / (1024 * 1024 * 1024))
        elif size_bytes >= 1024 * 1024:  # MB
            return _("{size:.1f} MB").format(size=size_bytes / (1024 * 1024))
        elif size_bytes >= 1024:  # KB
            return _("{size:.1f} KB").format(size=size_bytes / 1024)
        else:
            return ngettext("{size} byte", "{size} bytes", size_bytes).format(size=size_bytes)

    def show_completion(self, success=True, error_message=None):
        """Show completion state"""
        GLib.idle_add(self._show_completion_ui, success, error_message)

    def _show_route_only(self) -> None:
        """Finished: drop the progress row; hide the card if no route is left."""
        try:
            self.progress_row.set_visible(False)
            self.card.set_visible(
                self.source_row.get_visible() or self.dest_row.get_visible()
            )
        except (AttributeError, RuntimeError, GLib.Error):
            pass

    def _completion_summary(self, count: int, size_bytes: int) -> str:
        """Body text after a transfer: "12 files · 7.6 GB"."""
        if count > 0 and size_bytes > 0:
            return ngettext(
                "{count} file · {size}", "{count} files · {size}", count
            ).format(count=count, size=self._format_size(size_bytes))
        if size_bytes > 0:
            return _("Transferred {size} successfully").format(
                size=self._format_size(size_bytes)
            )
        if count > 0:
            return ngettext(
                "Successfully transferred {count} file",
                "Successfully transferred {count} files", count,
            ).format(count=count)
        return _("Transfer completed successfully")

    def _show_completion_ui(self, success, error_message):
        """Update UI to show completion state (idempotent - safe to call multiple times)"""
        # Prevent multiple completion dialogs from being shown
        if self._completion_shown:
            return False
        self._completion_shown = True
        self._stop_render_timer()

        show_locate = False
        self._locate_path = None
        if success:
            headings = {
                "download": _("Download Complete"),
                "upload": _("Upload Complete"),
                "copy": _("Copy Complete"),
                "move": _("Move Complete"),
                "delete": _("Delete Complete"),
            }
            self._set_dialog_heading(
                headings.get(self.operation_type, _("Transfer Complete"))
            )
            count = self.files_completed or self.total_files
            if self.operation_type == "delete":
                self._set_status_text(
                    ngettext(
                        "Successfully deleted {count} item",
                        "Successfully deleted {count} items",
                        count,
                    ).format(count=count)
                    if count > 0
                    else _("Delete completed successfully")
                )
            else:
                self._set_status_text(
                    self._completion_summary(count, self._transferred_bytes)
                )
            self._show_route_only()
            # Downloads only: portal-aware reveal of the local destination.
            if self.operation_type == "download" and self._destination_path:
                sources = [self._source_path] if self._source_path else None
                self._locate_path = resolve_download_locate_path(
                    self._destination_path, sources
                )
                show_locate = self._locate_path is not None
        else:
            if self.operation_type == "delete":
                self._set_dialog_heading(_("Delete Failed"))
                fallback = _("An error occurred while deleting")
            else:
                self._set_dialog_heading(_("Transfer Failed"))
                fallback = _("An error occurred during transfer")
            # The body carries the error; the card keeps the file that was
            # running and how far the batch got — a batch with one failed
            # item still handled every byte.
            self._set_status_text(
                _("Error: {message}").format(message=error_message)
                if error_message else fallback
            )
            if self._latest_fraction is not None:
                fraction = max(0.0, min(1.0, float(self._latest_fraction)))
                self.progress_bar.set_fraction(fraction)
                self.percent_label.set_text(f"{int(fraction * 100)}%")
            self._set_speed_and_time(None, None)

        self._enter_completion_actions(show_locate=show_locate)
        return False
