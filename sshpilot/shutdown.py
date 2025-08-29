"""Utilities for clean application shutdown.

This module contains helpers that were previously methods on ``MainWindow``
for disconnecting terminals, presenting progress dialogs and showing
reconnection feedback.  Extracting them here keeps ``window.py`` a little
leaner and makes the quit logic reusable.
"""

from gettext import gettext as _
import logging

from gi.repository import Gtk, GLib


logger = logging.getLogger(__name__)


def cleanup_and_quit(window):
    """Clean up all connections and quit.

    Parameters
    ----------
    window: MainWindow
        The main application window invoking the shutdown.
    """

    if getattr(window, "_is_quitting", False):
        logger.debug("Already quitting, ignoring duplicate request")
        return

    logger.info("Starting cleanup before quit...")
    window._is_quitting = True

    connections_to_disconnect = []
    for conn, terms in getattr(window, "connection_to_terminals", {}).items():
        for term in terms:
            connections_to_disconnect.append((conn, term))

    if not connections_to_disconnect:
        window._do_quit()
        return

    total = len(connections_to_disconnect)
    _show_cleanup_progress(window, total)

    GLib.idle_add(
        _perform_cleanup_and_quit,
        window,
        connections_to_disconnect,
        priority=GLib.PRIORITY_DEFAULT_IDLE,
    )

    try:
        GLib.timeout_add_seconds(5, window._do_quit)
    except Exception:
        pass


def _perform_cleanup_and_quit(window, connections_to_disconnect):
    """Disconnect terminals with UI progress, then quit. Runs on idle."""

    try:
        total = len(connections_to_disconnect)
        for index, (connection, terminal) in enumerate(
            connections_to_disconnect, start=1
        ):
            try:
                logger.debug(
                    f"Disconnecting {connection.nickname} ({index}/{total})"
                )
                if hasattr(terminal, "process_pid") and terminal.process_pid:
                    try:
                        import os, signal

                        os.kill(terminal.process_pid, signal.SIGTERM)
                    except Exception:
                        pass
                if hasattr(terminal, "is_connected") and not terminal.is_connected:
                    logger.debug("Terminal not connected; skipped disconnect")
                else:
                    _disconnect_terminal_safely(terminal)
            finally:
                _update_cleanup_progress(window, index, total)
                GLib.MainContext.default().iteration(False)
    except Exception as e:
        logger.error(f"Cleanup during quit encountered an error: {e}")
    finally:
        try:
            from .terminal import SSHProcessManager

            process_manager = SSHProcessManager()
            with process_manager.lock:
                pids = list(process_manager.processes.keys())
                for pid in pids:
                    process_manager._terminate_process_by_pid(pid)
                process_manager.processes.clear()
                process_manager.terminals.clear()
        except Exception as e:
            logger.debug(f"Final SSH cleanup failed: {e}")
        window.active_terminals.clear()
        _hide_cleanup_progress(window)
        GLib.idle_add(window._do_quit)
    return False


def _show_cleanup_progress(window, total_connections):
    """Show cleanup progress dialog."""

    window._progress_dialog = Gtk.Window()
    window._progress_dialog.set_title("Closing Connections")
    window._progress_dialog.set_transient_for(window)
    window._progress_dialog.set_modal(True)
    window._progress_dialog.set_default_size(350, 120)
    window._progress_dialog.set_resizable(False)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    box.set_margin_top(20)
    box.set_margin_bottom(20)
    box.set_margin_start(20)
    box.set_margin_end(20)

    window._progress_bar = Gtk.ProgressBar()
    window._progress_bar.set_fraction(0)
    box.append(window._progress_bar)

    window._progress_label = Gtk.Label()
    window._progress_label.set_text(
        f"Closing {total_connections} connection(s)..."
    )
    box.append(window._progress_label)

    window._progress_dialog.set_child(box)
    window._progress_dialog.present()


def _update_cleanup_progress(window, completed, total):
    """Update cleanup progress."""

    if getattr(window, "_progress_bar", None):
        fraction = completed / total if total > 0 else 1.0
        window._progress_bar.set_fraction(fraction)

    if getattr(window, "_progress_label", None):
        window._progress_label.set_text(
            f"Closed {completed} of {total} connection(s)..."
        )


def _hide_cleanup_progress(window):
    """Hide cleanup progress dialog."""

    if getattr(window, "_progress_dialog", None):
        try:
            window._progress_dialog.close()
            window._progress_dialog = None
            window._progress_bar = None
            window._progress_label = None
        except Exception as e:
            logger.debug(f"Error closing progress dialog: {e}")


def show_reconnecting_message(window, connection):
    """Show a small modal indicating reconnection is in progress."""

    try:
        if getattr(window, "_reconnect_dialog", None):
            return

        window._reconnect_dialog = Gtk.Window()
        window._reconnect_dialog.set_title(_("Reconnecting"))
        window._reconnect_dialog.set_transient_for(window)
        window._reconnect_dialog.set_modal(True)
        window._reconnect_dialog.set_default_size(320, 100)
        window._reconnect_dialog.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        spinner = Gtk.Spinner()
        spinner.set_hexpand(False)
        spinner.set_vexpand(False)
        spinner.start()
        box.append(spinner)

        label = Gtk.Label()
        label.set_text(
            _("Reconnecting to {}...").format(getattr(connection, "nickname", ""))
        )
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        box.append(label)

        window._reconnect_spinner = spinner
        window._reconnect_label = label
        window._reconnect_dialog.set_child(box)
        window._reconnect_dialog.present()
    except Exception as e:
        logger.debug(f"Failed to show reconnecting message: {e}")


def hide_reconnecting_message(window):
    """Hide the reconnection progress dialog if shown."""

    try:
        if getattr(window, "_reconnect_dialog", None):
            window._reconnect_dialog.close()
        window._reconnect_dialog = None
        window._reconnect_spinner = None
        window._reconnect_label = None
    except Exception as e:
        logger.debug(f"Failed to hide reconnecting message: {e}")


def _disconnect_terminal_safely(terminal):
    """Safely disconnect a terminal."""

    try:
        if hasattr(terminal, "disconnect"):
            terminal.disconnect()
        elif hasattr(terminal, "close_connection"):
            terminal.close_connection()
        elif hasattr(terminal, "close"):
            terminal.close()

        if hasattr(terminal, "force_close"):
            terminal.force_close()

    except Exception as e:
        logger.error(f"Error disconnecting terminal: {e}")


__all__ = [
    "cleanup_and_quit",
    "show_reconnecting_message",
    "hide_reconnecting_message",
]

