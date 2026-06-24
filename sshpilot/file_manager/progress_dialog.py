"""Modern HIG-compliant SFTP transfer progress dialog.

Subclasses ``Adw.AlertDialog`` when available (libadwaita ≥ 1.5) and falls
back to ``Adw.MessageDialog`` on older systems. UI updates are paced from
a ``GLib.timeout`` so per-chunk progress callbacks never touch widgets
directly — they only mutate state that the render tick reads.
"""

from __future__ import annotations

import collections
import logging
import time
from typing import Optional

from gi.repository import Adw, GLib, Gtk, Pango


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


class SFTPProgressDialog(_PROGRESS_DIALOG_BASE):
    """Modern GNOME HIG-compliant SFTP file transfer progress dialog.

    Subclasses ``Adw.AlertDialog`` when available (libadwaita ≥ 1.5) and
    falls back to the deprecated ``Adw.MessageDialog`` on older systems.

    UI is driven from a fixed-cadence ``GLib.timeout`` (the canonical GTK
    ProgressBar pattern). The worker's per-chunk callbacks only mutate a few
    state fields and push samples into a sliding window — they do not touch
    widgets. The render tick reads that state and updates labels + bar at
    ~4 Hz, which keeps the UI responsive without burning cycles on transfers
    that emit thousands of callbacks per second.
    """

    _LABEL_WIDTH_CHARS = 48
    _RENDER_INTERVAL_MS = 250
    _SPEED_WINDOW_SECONDS = 5.0

    def __init__(self, parent=None, operation_type="transfer"):
        _titles = {
            "download": "Downloading Files",
            "upload": "Uploading Files",
            "copy": "Copying on Server",
            "move": "Moving on Server",
        }
        title = _titles.get(operation_type, "Transferring Files")

        # Different constructor kwargs for the two base classes.
        if _HAS_ALERT_DIALOG:
            super().__init__(
                heading=title,
                body="Transferring files…",
                default_response="cancel",
            )
        else:
            super().__init__(
                title=title,
                body="Transferring files…",
                default_response="cancel",
            )
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
        self.total_files = 0
        self.operation_type = operation_type
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

        self._build_ui()

        # Start the render loop. Returning False from _render_tick removes the
        # source; we also clear the id on dialog close as a belt-and-braces.
        self._render_timeout_id: Optional[int] = GLib.timeout_add(
            self._RENDER_INTERVAL_MS, self._render_tick
        )
        
    def _build_ui(self):
        """Build the modern GNOME HIG-compliant UI.

        We deliberately do NOT add an Adw response button for Cancel. Single
        responses in Adw.AlertDialog / Adw.MessageDialog span the entire
        footer width — too prominent for a progress dialog. Instead, Cancel
        is a regular Gtk.Button placed at the bottom of the body content,
        right-aligned. Esc-to-cancel is preserved via the ``closed`` signal
        (Adw.Dialog fires that for any close path, including Esc).
        """

        # No add_response(); no set_default_response(); no set_close_response().
        # We listen on ``closed`` to catch Esc / parent dismiss / etc.
        self.connect("closed", self._on_dialog_closed)

        # Create progress content area
        progress_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12
        )
        
        def _configure_progress_label(label: Gtk.Label) -> None:
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_justify(Gtk.Justification.CENTER)
            label.set_width_chars(self._LABEL_WIDTH_CHARS)
            label.set_max_width_chars(self._LABEL_WIDTH_CHARS)

        def _configure_path_label(label: Gtk.Label) -> None:
            # Middle-ellipsis keeps both the head (drive/scheme/leading dir)
            # and tail (filename) visible — best fit for paths.
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            label.set_halign(Gtk.Align.START)
            label.set_xalign(0.0)
            label.set_width_chars(self._LABEL_WIDTH_CHARS)
            label.set_max_width_chars(self._LABEL_WIDTH_CHARS)
            label.add_css_class("caption")
            label.add_css_class("dim-label")

        # Current file label (primary info)
        self.file_label = Gtk.Label()
        self.file_label.set_text("—")
        _configure_progress_label(self.file_label)
        progress_box.append(self.file_label)

        # Status label for detailed progress messages
        self.status_label = Gtk.Label()
        self.status_label.set_text("Preparing transfer…")
        _configure_progress_label(self.status_label)
        progress_box.append(self.status_label)

        # Source / destination paths. Hidden until set_paths() is called so
        # we don't leave two empty "From:" / "To:" lines for callers that
        # didn't supply them.
        self.source_label = Gtk.Label()
        _configure_path_label(self.source_label)
        self.source_label.set_visible(False)
        progress_box.append(self.source_label)

        self.dest_label = Gtk.Label()
        _configure_path_label(self.dest_label)
        self.dest_label.set_visible(False)
        progress_box.append(self.dest_label)

        # Main progress bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_text("0%")
        progress_box.append(self.progress_bar)
        
        # Transfer details
        details_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6
        )
        progress_box.append(details_box)
        
        # Speed and time info
        info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        details_box.append(info_box)
        
        self.speed_label = Gtk.Label()
        self.speed_label.set_text("—")
        self.speed_label.set_halign(Gtk.Align.START)
        self.speed_label.add_css_class("caption")
        info_box.append(self.speed_label)
        
        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        info_box.append(spacer)
        
        self.time_label = Gtk.Label()
        self.time_label.set_text("—")
        self.time_label.set_halign(Gtk.Align.END)
        self.time_label.add_css_class("caption")
        info_box.append(self.time_label)
        
        # File counter
        self.counter_label = Gtk.Label()
        self.counter_label.set_text("0 of 0 files")
        self.counter_label.set_halign(Gtk.Align.CENTER)
        self.counter_label.add_css_class("caption")
        details_box.append(self.counter_label)

        # Cancel / Done action button. Placed in the body (not as an Adw
        # response) so it doesn't get the full-width single-response styling.
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        action_row.set_halign(Gtk.Align.END)
        action_row.set_margin_top(6)
        self.action_button = Gtk.Button(label="Cancel")
        self.action_button.add_css_class("pill")
        self.action_button.connect("clicked", self._on_action_button_clicked)
        action_row.append(self.action_button)
        progress_box.append(action_row)

        # Set the progress content as extra child
        self.set_extra_child(progress_box)
    
    def set_operation_details(self, total_files, filename=None):
        """Set the operation details"""
        # Only update total_files if it's larger (for adding more files to existing dialog)
        if total_files > self.total_files:
            self.total_files = total_files
            self.counter_label.set_text(f"{self.files_completed} of {total_files} files")

        if filename:
            self.current_file = filename
            self.file_label.set_text(filename)

    def set_paths(self, source: Optional[str] = None,
                  destination: Optional[str] = None) -> None:
        """Display the source and destination paths.

        Each label is middle-ellipsized so long paths stay readable; the
        full untruncated path is exposed as a tooltip on hover.
        """
        if source:
            self.source_label.set_text(f"From: {source}")
            self.source_label.set_tooltip_text(source)
            self.source_label.set_visible(True)
        if destination:
            self.dest_label.set_text(f"To: {destination}")
            self.dest_label.set_tooltip_text(destination)
            self.dest_label.set_visible(True)
    
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
            self._latest_message = message
        if current_file:
            self.current_file = current_file
            self._latest_file = current_file
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

        # Status + current-file text.
        if self._latest_message:
            try:
                self.status_label.set_text(self._latest_message)
            except (AttributeError, RuntimeError):
                pass
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
                self.progress_bar.set_text("")
            elif self._latest_fraction is not None:
                fraction = max(0.0, min(1.0, float(self._latest_fraction)))
                self.progress_bar.set_fraction(fraction)
                self.progress_bar.set_text(f"{int(fraction * 100)}%")
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
                        "Almost done…" if remaining == 0
                        else self._format_time(remaining / bps)
                    )
        try:
            self.speed_label.set_text(speed_text or "—")
            self.time_label.set_text(eta_text or "—")
        except (AttributeError, RuntimeError):
            pass

        return True  # keep the timer running

    @staticmethod
    def _format_speed(bps: float) -> str:
        if bps >= 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f} MB/s"
        if bps >= 1024:
            return f"{bps / 1024:.1f} KB/s"
        return f"{int(bps)} B/s"

    def _set_dialog_heading(self, text: str) -> None:
        """Set the dialog's primary heading on either base class."""
        if _HAS_ALERT_DIALOG:
            self.set_heading(text)
        else:
            self.set_title(text)
    
    def increment_file_count(self):
        """Increment completed file counter"""
        GLib.idle_add(self._increment_file_count_ui)
    
    def _increment_file_count_ui(self):
        """Update file counter (must be called from main thread)"""
        self.files_completed += 1
        self.counter_label.set_text(f"{self.files_completed} of {self.total_files} files")
        return False
    
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
            return f"{hours}h {minutes}m remaining"
        elif seconds > 60:
            minutes = int(seconds // 60)
            return f"{minutes}m remaining"
        else:
            return f"{int(seconds)}s remaining"
    
    def _format_size(self, size_bytes):
        """Format file size for display"""
        if size_bytes >= 1024 * 1024 * 1024:  # GB
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        elif size_bytes >= 1024 * 1024:  # MB
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes >= 1024:  # KB
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes} bytes"
    
    def show_completion(self, success=True, error_message=None):
        """Show completion state"""
        GLib.idle_add(self._show_completion_ui, success, error_message)
    
    def _show_completion_ui(self, success, error_message):
        """Update UI to show completion state (idempotent - safe to call multiple times)"""
        # Prevent multiple completion dialogs from being shown
        if self._completion_shown:
            return False
        self._completion_shown = True
        self._stop_render_timer()
        
        if success:
            self._set_dialog_heading("Transfer Complete")
            self.status_label.set_text("Transfer completed successfully")
            self.file_label.set_text(f"Successfully transferred {self.files_completed} files")
            self.progress_bar.set_fraction(1.0)
            self.progress_bar.set_text("100%")
        else:
            self._set_dialog_heading("Transfer Failed")
            self.status_label.set_text("Transfer failed")
            if error_message:
                self.file_label.set_text(f"Error: {error_message}")
            else:
                self.file_label.set_text("An error occurred during transfer")
        
        # Swap the body button's label from Cancel → Done. _on_action_button_clicked
        # reads _completion_shown (set above) and takes the "just close" path.
        try:
            self.action_button.set_label("Done")
            self.action_button.add_css_class("suggested-action")
        except (AttributeError, RuntimeError, GLib.GError):
            # Button may have been destroyed if the dialog is mid-close.
            pass

        return False

