"""Broadcast banner behavior for MainWindow.

Extracted verbatim from window.py as a mixin (matching the existing
WindowActions pattern) to shrink the window.py god-object. MainWindow inherits
this; every method keeps its original signature and `self.` state access, so
this is a pure code move with no behavior change.
"""

import logging

from gi.repository import Adw, GLib, Gdk
from gettext import gettext as _

logger = logging.getLogger(__name__)


class WindowBroadcastMixin:
    """Broadcast-command banner: show/hide, entry handling, send, timeouts."""

    def on_broadcast_send_clicked(self, button):
        """Handle broadcast banner send button click"""
        command = self.broadcast_entry.get_text().strip()
        if command:
            sent_count, failed_count = self.terminal_manager.broadcast_command(command)

            # Update banner message with result (we'll need to find the title label)
            # For now, just hide the banner after sending
            self.broadcast_entry_dirty = False
            self._schedule_broadcast_hide_timeout()
        else:
            # Show error for empty command - could add error styling here
            self.broadcast_entry_dirty = False
            self._schedule_broadcast_hide_timeout()

    def on_broadcast_cancel_clicked(self, button):
        """Handle broadcast banner cancel button click"""
        self.hide_broadcast_banner()

    def on_broadcast_entry_activate(self, entry):
        """Handle Enter key press in broadcast entry"""
        self.on_broadcast_send_clicked(self.broadcast_send_button)

    def on_broadcast_entry_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses in broadcast entry"""
        if keyval == Gdk.KEY_Escape:
            self.hide_broadcast_banner()
            return True  # Consume the key event
        self._cancel_broadcast_hide_timeout()
        return False  # Let other keys pass through

    def on_broadcast_banner_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses on the entire broadcast banner"""
        if keyval == Gdk.KEY_Escape:
            self.hide_broadcast_banner()
            return True  # Consume the key event
        return False  # Let other keys pass through

    def hide_broadcast_banner(self):
        """Hide the broadcast banner"""
        self._cancel_broadcast_hide_timeout()
        self.broadcast_banner.set_reveal_child(False)
        self.broadcast_entry_dirty = False
        self._suppress_broadcast_entry_changed = True
        try:
            self.broadcast_entry.set_text("")
        finally:
            self._suppress_broadcast_entry_changed = False

        # Focus the active terminal tab after hiding the banner
        self._focus_active_terminal_tab()

    def _focus_active_terminal_tab(self):
        """Focus the currently active terminal tab"""
        try:
            if hasattr(self, 'tab_view') and self.tab_view:
                selected_page = self.tab_view.get_selected_page()
                if selected_page:
                    terminal_widget = selected_page.get_child()
                    if terminal_widget:
                        if hasattr(terminal_widget, 'vte') and hasattr(terminal_widget.vte, 'grab_focus'):
                            terminal_widget.vte.grab_focus()
                        elif hasattr(terminal_widget, 'grab_focus'):
                            terminal_widget.grab_focus()
        except Exception as e:
            logger.debug(f"Failed to focus active terminal tab: {e}")

    def show_broadcast_banner(self):
        """Show the broadcast banner"""
        self._cancel_broadcast_hide_timeout()
        self.broadcast_banner.set_reveal_child(True)
        self.broadcast_entry_dirty = bool(self.broadcast_entry.get_text())
        # Focus the entry after a short delay to ensure banner is visible
        def focus_entry():
            self.broadcast_entry.grab_focus()
            return False
        GLib.idle_add(focus_entry)

    def on_broadcast_entry_changed(self, entry):
        """Track user edits to the broadcast entry"""
        if self._suppress_broadcast_entry_changed:
            return
        self.broadcast_entry_dirty = True
        self._cancel_broadcast_hide_timeout()

    def on_broadcast_entry_focus_enter(self, controller, *args):
        """Cancel hide timeout when the entry gains focus"""
        self._cancel_broadcast_hide_timeout()

    def on_broadcast_entry_focus_leave(self, controller, *args):
        """Schedule hiding when the entry loses focus"""
        if not self.broadcast_banner.get_reveal_child():
            return
        self._schedule_broadcast_hide_timeout()

    def _cancel_broadcast_hide_timeout(self):
        """Cancel any pending hide timeout for the broadcast banner"""
        if self.broadcast_hide_timeout_id is not None:
            try:
                GLib.source_remove(self.broadcast_hide_timeout_id)
            except Exception:
                pass
            finally:
                self.broadcast_hide_timeout_id = None

    def _schedule_broadcast_hide_timeout(self, timeout_ms: int = 5000):
        """Schedule hiding the broadcast banner after a delay"""
        self._cancel_broadcast_hide_timeout()

        def maybe_hide_banner():
            self.broadcast_hide_timeout_id = None
            entry_has_focus = False
            try:
                entry_has_focus = self.broadcast_entry.has_focus()
            except Exception:
                entry_has_focus = False

            if entry_has_focus and self.broadcast_entry_dirty:
                return False

            self.hide_broadcast_banner()
            return False

        self.broadcast_hide_timeout_id = GLib.timeout_add(timeout_ms, maybe_hide_banner)

    def on_broadcast_command_action(self, action, param=None):
        """Handle broadcast command action - shows banner to input command"""
        try:
            # Check if there are any SSH terminals open
            ssh_terminals_count = sum(
                1 for _ in self.terminal_manager.iter_ssh_terminals()
            )

            if ssh_terminals_count == 0:
                # Show message dialog
                try:
                    error_dialog = Adw.MessageDialog(
                        transient_for=self,
                        modal=True,
                        heading=_("No SSH Terminals Open"),
                        body=_("Connect to your server first!")
                    )
                    error_dialog.add_response('ok', _('OK'))
                    error_dialog.present()
                except Exception as e:
                    logger.error(f"Failed to show error dialog: {e}")
                return

            # Show the broadcast banner instead of a dialog
            self.show_broadcast_banner()

        except Exception as e:
            logger.error(f"Failed to show broadcast command dialog: {e}")
            # Show error dialog
            try:
                error_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Error"),
                    body=_("Failed to open broadcast command dialog: {error}").format(error=str(e))
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()
            except Exception:
                pass
