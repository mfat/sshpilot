import logging
import threading
from typing import Callable, Optional

from gettext import gettext as _
from gi.repository import Gtk, Adw, GLib

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.client import SshPilotClient
from sshpilot.gtk.known_hosts_controller import KnownHostsController
from .shortcut_utils import install_esc_to_close, install_search_esc


logger = logging.getLogger(__name__)


@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/known_hosts_editor.ui")
class KnownHostsEditorWindow(Adw.Window):
    """Simple window for viewing and removing entries from known_hosts."""

    __gtype_name__ = "KnownHostsEditorWindow"

    search_entry = Gtk.Template.Child()
    listbox = Gtk.Template.Child()
    save_button = Gtk.Template.Child()

    # Class-level defaults so stub-built instances (tests) start idle/open.
    _operation_in_flight = False
    _closed = False

    def __init__(self, parent, client: SshPilotClient, on_saved: Optional[Callable] = None):
        super().__init__()
        self.set_transient_for(parent)
        install_esc_to_close(self)
        install_search_esc(self.search_entry, self)

        self._on_saved = on_saved
        self._controller = KnownHostsController(client)
        self._all_entries = []  # List[KnownHostEntrySummary] for filtering
        self._operation_in_flight = False
        self._closed = False
        self.connect("close-request", self._on_close_request)

        # Populate after present() so the window appears immediately
        # (matches AuthorizedKeysWindow's deferred load).
        GLib.idle_add(self._load_entries)

    @Gtk.Template.Callback()
    def _on_cancel_clicked(self, _btn):
        self._closed = True
        self.close()

    def _on_close_request(self, *_args):
        self._closed = True
        return False

    def _set_operation_in_flight(self, in_flight: bool) -> None:
        """Update the UI busy state for the interactive controls."""
        self._operation_in_flight = in_flight
        sensitive = not in_flight
        for widget in (self.save_button, self.listbox, self.search_entry):
            set_sensitive = getattr(widget, "set_sensitive", None)
            if set_sensitive is not None:
                set_sensitive(sensitive)

    def _load_entries(self):
        """Load known_hosts entries through the daemon client on a worker thread."""
        if self._closed:
            return
        # Ignore a second load request while any operation is running.
        if self._operation_in_flight:
            return
        # Drop any previously rendered rows so a failed reload never shows
        # stale data as current.
        self._all_entries = []
        while True:
            child = self.listbox.get_first_child()
            if child is None:
                break
            self.listbox.remove(child)
        self._set_operation_in_flight(True)
        self._start_load_worker()

    def _start_load_worker(self):
        """Start one load worker thread; the busy state stays on until it reports."""
        if self._closed:
            return

        def worker():
            try:
                snapshot = self._controller.load()
                payload = ("ok", snapshot.entries)
            except Exception as e:
                logger.error("Failed to load known_hosts: %s", e)
                payload = ("error", str(e))
            GLib.idle_add(lambda: (self._on_load_finished(payload), False)[1])

        threading.Thread(target=worker, daemon=True).start()

    def _on_load_finished(self, payload):
        if self._closed:
            self._operation_in_flight = False
            return
        if payload[0] != "ok":
            self._set_operation_in_flight(False)
            self._show_error(_("Could not load known hosts"), payload[1])
            return
        self._all_entries = list(payload[1])
        self._display_entries(self._all_entries)
        # Save stays enabled even with no staged changes: an unchanged Save is
        # a valid successful no-op.
        self._set_operation_in_flight(False)

    def _display_entries(self, entries):
        """Display the given entry summaries in the listbox."""
        # Clear existing entries
        while True:
            child = self.listbox.get_first_child()
            if child is None:
                break
            self.listbox.remove(child)

        for entry in entries:
            # Create a ListBoxRow to properly contain our content
            list_row = Gtk.ListBoxRow()
            list_row.set_margin_start(12)
            list_row.set_margin_end(12)
            list_row.set_margin_top(6)
            list_row.set_margin_bottom(6)
            # Store the entry ID so duplicates stay distinguishable on removal.
            list_row._entry_id = entry.entry_id
            list_row._summary = entry

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.set_margin_start(6)
            row.set_margin_end(6)

            from sshpilot import icon_utils
            remove_btn = icon_utils.new_button_from_icon_name('user-trash-symbolic')
            remove_btn.set_valign(Gtk.Align.START)
            remove_btn.set_tooltip_text(_("Remove this entry"))
            remove_btn.connect('clicked', self._on_remove_clicked, list_row)

            # Create a vertical box for the host information
            info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            info_box.set_hexpand(True)

            # Parse the known_hosts line to make it more readable
            parts = entry.display_line.split()
            if len(parts) >= 3:
                hostname = parts[0]
                key_type = parts[1]
                key_data = parts[2]

                # Hostname label
                host_label = Gtk.Label(label=hostname)
                host_label.set_xalign(0)
                host_label.add_css_class("heading")
                host_label.set_selectable(True)
                info_box.append(host_label)

                # Key type and truncated key data
                key_info = f"{key_type} • {key_data[:50]}{'...' if len(key_data) > 50 else ''}"
                key_label = Gtk.Label(label=key_info)
                key_label.set_xalign(0)
                key_label.add_css_class("dim-label")
                key_label.set_selectable(True)
                info_box.append(key_label)
            else:
                # Fallback for malformed lines
                label = Gtk.Label(label=entry.display_line)
                label.set_xalign(0)
                label.set_wrap(True)
                label.set_selectable(True)
                info_box.append(label)

            row.append(remove_btn)
            row.append(info_box)
            list_row.set_child(row)
            self.listbox.append(list_row)

    def _on_remove_clicked(self, _btn, row):
        if self._closed or self._operation_in_flight:
            return
        entry_id = getattr(row, '_entry_id', None)
        if entry_id is None:
            return
        try:
            self._controller.stage_remove(entry_id)
        except RuntimeError:
            # A load/save is running; the list is disabled so this is defensive.
            logger.info("Ignored remove click while a known-hosts operation runs")
            return
        try:
            # Add visual feedback before removal
            from sshpilot import icon_utils
            _btn.set_sensitive(False)  # Disable button to prevent double-clicks
            icon_utils.set_button_icon(_btn, 'process-working-symbolic')  # Show working icon

            # Create a smooth fade-out animation
            def animate_removal():
                # Start with full opacity
                current_opacity = 1.0

                def fade_step():
                    nonlocal current_opacity
                    current_opacity -= 0.1  # Reduce opacity by 10% each step
                    row.set_opacity(current_opacity)

                    if current_opacity <= 0.1:
                        # Animation complete, remove the row
                        try:
                            # Remove from the listbox
                            self.listbox.remove(row)
                            # Remove the staged summary from the filter list
                            summary = getattr(row, '_summary', None)
                            if summary is not None and summary in self._all_entries:
                                self._all_entries.remove(summary)
                        except Exception as e:
                            logger.error("Failed to remove known_host entry: %s", e)
                        return False  # Stop the animation

                    return True  # Continue animation

                # Start the animation with 50ms intervals
                GLib.timeout_add(50, fade_step)

            # Start the animation
            animate_removal()

        except Exception as e:
            logger.error("Failed to remove known_host entry: %s", e)
            # Restore button state on error
            from sshpilot import icon_utils
            _btn.set_sensitive(True)
            icon_utils.set_button_icon(_btn, 'user-trash-symbolic')

    @Gtk.Template.Callback()
    def _on_search_changed(self, search_entry):
        """Handle search text changes."""
        search_text = search_entry.get_text().lower().strip()
        if not search_text:
            filtered = self._all_entries
        else:
            filtered = [
                entry
                for entry in self._all_entries
                if search_text in entry.display_line.lower()
            ]
        self._display_entries(filtered)

    @Gtk.Template.Callback()
    def _on_save_clicked(self, _btn):
        if self._closed:
            return
        # Ignore another Save click while loading or saving.
        if self._operation_in_flight:
            return
        self._set_operation_in_flight(True)

        def worker():
            try:
                self._controller.save()
                payload = ("ok", "")
            except SshPilotError as e:
                if e.code is ErrorCode.STALE_EDITOR:
                    logger.info("Known hosts changed since the snapshot was loaded")
                    payload = ("stale", _(
                        "The known-hosts file changed on disk. Your list has been "
                        "refreshed — review it before removing entries again."
                    ))
                else:
                    logger.error("Failed to save known_hosts: %s", e)
                    payload = ("error", str(e))
            except Exception as e:
                logger.error("Failed to save known_hosts: %s", e)
                payload = ("error", str(e))
            GLib.idle_add(lambda: (self._on_save_finished(payload), False)[1])

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_finished(self, payload):
        if self._closed:
            self._operation_in_flight = False
            if payload[0] == "ok" and self._on_saved:
                self._on_saved()
            return
        status = payload[0]
        if status == "ok":
            self._set_operation_in_flight(False)
            if self._on_saved:
                self._on_saved()
            self.close()
            return
        if status == "stale":
            # Show the message and start exactly one reload; keep the controls
            # disabled until that reload finishes. Never retry the mutation.
            self._show_error(_("Known hosts changed"), payload[1])
            self._start_load_worker()
            return
        # Non-stale error: restore the controls and keep staged removals so the
        # user can retry manually.
        self._set_operation_in_flight(False)
        self._show_error(_("Could not save known hosts"), payload[1])

    def _show_error(self, heading: str, body: str):
        try:
            dialog = Adw.MessageDialog(
                transient_for=self, modal=True, heading=heading, body=body,
            )
            dialog.add_response("ok", _("OK"))
            dialog.set_default_response("ok")
            dialog.present()
        except Exception:
            logger.error("Could not show error dialog: %s - %s", heading, body)
