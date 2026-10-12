"""Broadcast banner behavior for MainWindow.

Extracted from window.py as a mixin (matching the existing WindowActions
pattern) to keep banner presentation separate from daemon session behavior.
MainWindow inherits this; the banner submits typed input to existing daemon
terminal sessions and does not own a backend operation.
"""

import logging

from gi.repository import Adw, GLib, Gdk, Gtk
from gettext import gettext as _

logger = logging.getLogger(__name__)


class WindowBroadcastMixin:
    """Broadcast-command banner: show/hide, entry handling, send.

    The banner stays open until the user dismisses it (Cancel or Escape); it
    never hides itself on a timer, after sending, or on focus loss.
    """

    def on_broadcast_send_clicked(self, button):
        """Handle broadcast banner send button click"""
        if getattr(self, "_broadcast_submission_pending", False):
            logger.info("Ignored overlapping broadcast submission")
            return
        command = self.broadcast_entry.get_text().strip()
        if command:
            self._broadcast_submission_pending = True
            self.broadcast_send_button.set_sensitive(False)
            try:
                submitted = self.terminal_manager.broadcast_command(
                    command,
                    on_success=self._on_broadcast_sent,
                    on_error=self._on_broadcast_failed,
                )
            except Exception as error:
                self._on_broadcast_failed(error)
                return
            if submitted is None:
                self._broadcast_submission_pending = False
                self.broadcast_send_button.set_sensitive(True)

    def _on_broadcast_sent(self, _result):
        """Handle the immediate session-input mutation acknowledgement."""
        self._broadcast_submission_pending = False
        self.broadcast_send_button.set_sensitive(True)
        logger.info("Broadcast command sent to existing terminal sessions")

    def _on_broadcast_failed(self, error):
        self._broadcast_submission_pending = False
        self.broadcast_send_button.set_sensitive(True)
        logger.warning("Terminal broadcast command was not accepted: %s", error)

    def on_broadcast_cancel_clicked(self, button):
        """Handle broadcast banner cancel button click"""
        self.hide_broadcast_banner()

    def on_broadcast_entry_activate(self, entry):
        """Handle Enter key press in broadcast entry"""
        self.on_broadcast_send_clicked(self.broadcast_send_button)

    def on_broadcast_entry_changed(self, entry):
        """Show the clear icon only while there is text to clear"""
        self._update_broadcast_clear_icon()

    def _update_broadcast_clear_icon(self):
        pos = Gtk.EntryIconPosition.SECONDARY
        if not self.broadcast_entry.get_text():
            self.broadcast_entry.set_icon_from_icon_name(pos, None)
            return
        self.broadcast_entry.set_icon_from_icon_name(pos, 'edit-clear-symbolic')
        self.broadcast_entry.set_icon_tooltip_text(pos, _("Clear"))
        self.broadcast_entry.set_icon_activatable(pos, True)

    def on_broadcast_entry_icon_release(self, entry, icon_pos):
        """Clear the command and keep typing in the entry"""
        if icon_pos != Gtk.EntryIconPosition.SECONDARY:
            return
        self.broadcast_entry.set_text("")
        self.broadcast_entry.grab_focus()

    def on_broadcast_entry_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses in broadcast entry"""
        if keyval == Gdk.KEY_Escape:
            self.hide_broadcast_banner()
            return True  # Consume the key event
        return False  # Let other keys pass through

    def on_broadcast_banner_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses on the entire broadcast banner"""
        if keyval == Gdk.KEY_Escape:
            self.hide_broadcast_banner()
            return True  # Consume the key event
        return False  # Let other keys pass through

    def hide_broadcast_banner(self):
        """Hide the broadcast banner"""
        self.broadcast_banner.set_reveal_child(False)
        self.broadcast_banner.set_visible(False)
        self.broadcast_entry.set_text("")

        # Focus the active terminal tab after hiding the banner
        self._focus_active_terminal_tab()

    def _focus_active_terminal_tab(self):
        """Focus the currently active terminal tab"""
        try:
            terminal_widget = self._get_active_terminal_widget()
            if terminal_widget is not None:
                terminal_widget.grab_terminal_focus()
        except Exception as e:
            logger.debug(f"Failed to focus active terminal tab: {e}")

    def show_broadcast_banner(self):
        """Show the broadcast banner"""
        self.broadcast_banner.set_visible(True)
        self.broadcast_banner.set_reveal_child(True)

        # Focus the entry after a short delay to ensure banner is visible
        def focus_entry():
            self.broadcast_entry.grab_focus()
            return False

        GLib.idle_add(focus_entry)

    def on_broadcast_command_action(self, action, param=None):
        """Handle broadcast command action - shows banner to input command"""
        try:
            # Check if there are any SSH terminals open
            ssh_terminals_count = sum(1 for _ in self.terminal_manager.iter_ssh_terminals())

            if ssh_terminals_count == 0:
                # Show message dialog
                try:
                    error_dialog = Adw.MessageDialog(
                        transient_for=self,
                        modal=True,
                        heading=_("No SSH Terminals Open"),
                        body=_("Connect to your server first!"),
                    )
                    error_dialog.add_response("ok", _("OK"))
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
                    body=_("Failed to open broadcast command dialog: {error}").format(error=str(e)),
                )
                error_dialog.add_response("ok", _("OK"))
                error_dialog.present()
            except Exception:
                pass
