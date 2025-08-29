import os
import logging

from gi.repository import Gio, GLib, Adw, Gdk
from gettext import gettext as _

from .terminal import TerminalWidget
from .preferences import is_running_in_flatpak

logger = logging.getLogger(__name__)


class TerminalManager:
    """Manage terminal creation and connection lifecycle"""

    def __init__(self, window):
        self.window = window

    # Connecting/disconnecting hosts
    def connect_to_host(self, connection, force_new: bool = False):
        window = self.window
        if not force_new:
            if connection in window.active_terminals:
                terminal = window.active_terminals[connection]
                page = window.tab_view.get_page(terminal)
                if page is not None:
                    window.tab_view.set_selected_page(page)
                    return
                else:
                    logger.warning(
                        f"Terminal for {connection.nickname} not found in tab view, removing from active terminals"
                    )
                    del window.active_terminals[connection]
            existing_terms = window.connection_to_terminals.get(connection) or []
            for t in reversed(existing_terms):
                page = window.tab_view.get_page(t)
                if page is not None:
                    window.active_terminals[connection] = t
                    window.tab_view.set_selected_page(page)
                    return

        use_external = window.config.get_setting('use-external-terminal', False)
        if use_external and not is_running_in_flatpak():
            window._open_connection_in_external_terminal(connection)
            return
        else:
            terminal = TerminalWidget(connection, window.config, window.connection_manager)
            terminal.connect('connection-established', self.on_terminal_connected)
            terminal.connect('connection-failed', lambda w, e: logger.error(f"Connection failed: {e}"))
            terminal.connect('connection-lost', self.on_terminal_disconnected)
            terminal.connect('title-changed', self.on_terminal_title_changed)

            page = window.tab_view.append(terminal)
            page.set_title(connection.nickname)
            page.set_icon(Gio.ThemedIcon.new('utilities-terminal-symbolic'))

            window.connection_to_terminals.setdefault(connection, []).append(terminal)
            window.terminal_to_connection[terminal] = connection
            window.active_terminals[connection] = terminal

            window.show_tab_view()
            window.tab_view.set_selected_page(page)

        def _set_terminal_colors():
            try:
                fg = Gdk.RGBA(); fg.parse('rgb(0,0,0)')
                bg = Gdk.RGBA(); bg.parse('rgb(255,255,255)')
                terminal.vte.set_color_foreground(fg)
                terminal.vte.set_color_background(bg)
                terminal.vte.set_colors(fg, bg, None)
                terminal.vte.queue_draw()
                if not terminal._connect_ssh():
                    logger.error('Failed to establish SSH connection')
                    window.tab_view.close_page(page)
                    try:
                        if connection in window.active_terminals and window.active_terminals[connection] is terminal:
                            del window.active_terminals[connection]
                        if terminal in window.terminal_to_connection:
                            del window.terminal_to_connection[terminal]
                        if connection in window.connection_to_terminals and terminal in window.connection_to_terminals[connection]:
                            window.connection_to_terminals[connection].remove(terminal)
                            if not window.connection_to_terminals[connection]:
                                del window.connection_to_terminals[connection]
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Error setting terminal colors: {e}")
                if not terminal._connect_ssh():
                    logger.error('Failed to establish SSH connection')
                    window.tab_view.close_page(page)
                    try:
                        if connection in window.active_terminals and window.active_terminals[connection] is terminal:
                            del window.active_terminals[connection]
                        if terminal in window.terminal_to_connection:
                            del window.terminal_to_connection[terminal]
                        if connection in window.connection_to_terminals and terminal in window.connection_to_terminals[connection]:
                            window.connection_to_terminals[connection].remove(terminal)
                            if not window.connection_to_terminals[connection]:
                                del window.connection_to_terminals[connection]
                    except Exception:
                        pass

        GLib.idle_add(_set_terminal_colors)

    def _on_disconnect_confirmed(self, dialog, response_id, connection):
        dialog.destroy()
        window = self.window
        if response_id == 'disconnect' and connection in window.active_terminals:
            terminal = window.active_terminals[connection]
            terminal.disconnect()
            if getattr(window, '_pending_delete_connection', None) is connection:
                try:
                    window.connection_manager.remove_connection(connection)
                finally:
                    window._pending_delete_connection = None

    def disconnect_from_host(self, connection):
        window = self.window
        if connection not in window.active_terminals:
            return
        confirm_disconnect = window.config.get_setting('confirm-disconnect', True)
        if confirm_disconnect:
            dialog = Adw.MessageDialog(
                transient_for=window,
                modal=True,
                heading=_("Disconnect from {}").format(connection.nickname or connection.host),
                body=_("Are you sure you want to disconnect from this host?")
            )
            dialog.add_response('cancel', _("Cancel"))
            dialog.add_response('disconnect', _("Disconnect"))
            dialog.set_response_appearance('disconnect', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close')
            dialog.set_close_response('cancel')
            dialog.connect('response', self._on_disconnect_confirmed, connection)
            dialog.present()
        else:
            terminal = window.active_terminals[connection]
            terminal.disconnect()

    # Tab management and local terminals
    def _add_terminal_tab(self, terminal_widget, title):
        try:
            page = self.window.tab_view.append(terminal_widget)
            page.set_title(title)
            page.set_icon(Gio.ThemedIcon.new('utilities-terminal-symbolic'))
            self.window.show_tab_view()
            self.window.tab_view.set_selected_page(page)
            logger.info(f"Added terminal tab: {title}")
        except Exception as e:
            logger.error(f"Failed to add terminal tab: {e}")

    def show_local_terminal(self):
        logger.info("Show local terminal tab")
        try:
            class LocalConnection:
                def __init__(self):
                    self.nickname = "Local Terminal"
                    self.host = "localhost"
                    self.username = os.getenv('USER', 'user')
                    self.port = 22
            local_connection = LocalConnection()
            terminal_widget = TerminalWidget(local_connection, self.window.config, self.window.connection_manager)
            terminal_widget.setup_local_shell()
            self._add_terminal_tab(terminal_widget, "Local Terminal")
            GLib.idle_add(terminal_widget.show)
            GLib.idle_add(terminal_widget.vte.show)
            logger.info("Local terminal tab created successfully")
        except Exception as e:
            logger.error(f"Failed to show local terminal: {e}")
            try:
                dialog = Adw.MessageDialog(
                    transient_for=self.window,
                    modal=True,
                    heading="Error",
                    body=f"Could not open local terminal.\n\n{e}"
                )
                dialog.add_response("ok", "OK")
                dialog.present()
            except Exception:
                pass

    # Broadcast commands
    def broadcast_command(self, command: str):
        cmd = (command + "\n").encode("utf-8")
        sent_count = 0
        failed_count = 0
        for i in range(self.window.tab_view.get_n_pages()):
            page = self.window.tab_view.get_nth_page(i)
            if page is None:
                continue
            terminal_widget = page.get_child()
            if terminal_widget is None or not hasattr(terminal_widget, 'vte'):
                continue
            if hasattr(terminal_widget, 'connection'):
                if (hasattr(terminal_widget.connection, 'nickname') and
                        terminal_widget.connection.nickname == "Local Terminal"):
                    continue
                if not hasattr(terminal_widget.connection, 'host'):
                    continue
                try:
                    terminal_widget.vte.feed_child(cmd)
                    sent_count += 1
                    logger.debug(
                        f"Sent command to SSH terminal: {terminal_widget.connection.nickname}")
                except Exception as e:
                    failed_count += 1
                    logger.error(
                        f"Failed to send command to terminal {terminal_widget.connection.nickname}: {e}")
        logger.info(
            f"Broadcast command completed: {sent_count} terminals received command, {failed_count} failed")
        return sent_count, failed_count

    # Terminal signal handlers
    def on_terminal_connected(self, terminal):
        terminal.connection.is_connected = True
        if terminal.connection in self.window.connection_rows:
            row = self.window.connection_rows[terminal.connection]
            row.update_status()
            row.queue_draw()

        if hasattr(self.window, "_hide_reconnecting_message"):
            GLib.idle_add(self.window._hide_reconnecting_message)

        self.window._is_controlled_reconnect = False
        if not getattr(self.window, '_is_controlled_reconnect', False):
            logger.info(
                f"Terminal connected: {terminal.connection.nickname} ({terminal.connection.username}@{terminal.connection.host})"
            )
        else:
            logger.debug(
                f"Terminal reconnected after settings update: {terminal.connection.nickname}")

    def on_terminal_disconnected(self, terminal):
        terminal.connection.is_connected = False
        if terminal.connection in self.window.connection_rows:
            row = self.window.connection_rows[terminal.connection]
            row.update_status()
            row.queue_draw()
        logger.info(
            f"Terminal disconnected: {terminal.connection.nickname} ({terminal.connection.username}@{terminal.connection.host})"
        )

    def on_terminal_title_changed(self, terminal, title):
        page = self.window.tab_view.get_page(terminal)
        if page:
            if title and title != terminal.connection.nickname:
                page.set_title(f"{terminal.connection.nickname} - {title}")
            else:
                page.set_title(terminal.connection.nickname)
