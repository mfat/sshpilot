import os
import asyncio
import logging

from gi.repository import Gio, GLib, Adw, Gdk
from gettext import gettext as _

from .terminal import TerminalWidget
from .preferences import should_hide_external_terminal_options

logger = logging.getLogger(__name__)


class TerminalManager:
    """Manage terminal creation and connection lifecycle"""

    def __init__(self, window):
        self.window = window

    def _ensure_backend_alignment(self, terminal) -> None:
        """Ensure the given terminal uses the configured backend."""
        if not terminal or not hasattr(self.window, 'config'):
            return
        desired = self.window.config.get_setting('terminal.backend', 'vte')
        getter = getattr(terminal, 'get_backend_name', None)
        current = getter() if callable(getter) else 'vte'
        if isinstance(desired, str) and isinstance(current, str):
            if current.lower() == (desired or 'vte').lower():
                return
        aligner = getattr(terminal, 'ensure_backend', None)
        if callable(aligner):
            try:
                aligner(desired)
            except Exception:
                logger.error("Failed to align terminal backend", exc_info=True)

    def refresh_backends(self) -> None:
        """Ensure all existing terminals use the configured backend."""
        seen = set()
        collections = []
        connection_terms = getattr(self.window, 'connection_to_terminals', None)
        if isinstance(connection_terms, dict):
            collections.extend(connection_terms.values())
        active = getattr(self.window, 'active_terminals', None)
        if isinstance(active, dict):
            collections.append(active.values())
        for group in collections:
            for term in list(group):
                if term in seen:
                    continue
                self._ensure_backend_alignment(term)
                seen.add(term)
        tab_view = getattr(self.window, 'tab_view', None)
        if tab_view is not None and hasattr(tab_view, 'get_n_pages'):
            try:
                for index in range(tab_view.get_n_pages()):
                    page = tab_view.get_nth_page(index)
                    if page is None:
                        continue
                    term = page.get_child()
                    if term is None or term in seen:
                        continue
                    self._ensure_backend_alignment(term)
                    seen.add(term)
            except Exception:
                logger.debug("Failed to iterate tab view while refreshing backends", exc_info=True)

    # Connecting/disconnecting hosts
    def connect_to_host(self, connection, force_new: bool = False):
        window = self.window
        if not force_new:
            if connection in window.active_terminals:
                terminal = window.active_terminals[connection]
                self._ensure_backend_alignment(terminal)
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
                    self._ensure_backend_alignment(t)
                    window.active_terminals[connection] = t
                    window.tab_view.set_selected_page(page)
                    return

        # The user's "use external terminal" preference is only applied when
        # external terminal options are not hidden by policy or environment.
        use_external = window.config.get_setting('use-external-terminal', False)
        if use_external and not should_hide_external_terminal_options():
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

        def _cleanup_failed_terminal():
            connection.is_connected = False
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

        def _set_terminal_colors():

            try:
                if hasattr(window, 'get_application'):
                    try:
                        app = window.get_application()
                    except Exception:
                        app = None
                else:
                    app = None
                if app is None:
                    try:
                        app = Adw.Application.get_default()
                    except Exception:
                        app = None
                connection_manager = getattr(window, 'connection_manager', None)
                use_native = bool(getattr(connection_manager, 'native_connect_enabled', False))
                if not use_native and app is not None and hasattr(app, 'native_connect_enabled'):
                    use_native = bool(app.native_connect_enabled)
                if not getattr(connection, 'ssh_cmd', None):
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            fut = asyncio.run_coroutine_threadsafe(
                                connection.native_connect() if use_native and hasattr(connection, 'native_connect') else connection.connect(),
                                loop,
                            )
                            fut.result()
                        else:
                            if use_native and hasattr(connection, 'native_connect'):
                                loop.run_until_complete(connection.native_connect())
                            else:
                                loop.run_until_complete(connection.connect())
                    except Exception as prep_err:
                        logger.error(f"Failed to prepare SSH command: {prep_err}")

                terminal.apply_theme()
                refresher = getattr(terminal, 'queue_draw_terminal', None)
                if callable(refresher):
                    refresher()
                if not terminal._connect_ssh():
                    logger.error('Failed to establish SSH connection')
                    _cleanup_failed_terminal()
            except Exception as e:
                logger.error(f"Error setting terminal colors: {e}")
                if not terminal._connect_ssh():
                    logger.error('Failed to establish SSH connection')
                    _cleanup_failed_terminal()

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
            host_value = getattr(connection, 'hostname', getattr(connection, 'host', getattr(connection, 'nickname', '')))
            dialog = Adw.MessageDialog(
                transient_for=window,
                modal=True,
                heading=_("Disconnect from {}").format(connection.nickname or host_value),
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
                    self.hostname = "localhost"
                    self.host = self.hostname
                    self.username = os.getenv('USER', 'user')
                    self.port = 22
                    self.is_connected = True
            local_connection = LocalConnection()
            terminal_widget = TerminalWidget(local_connection, self.window.config, self.window.connection_manager)
            terminal_widget.setup_local_shell()
            self._add_terminal_tab(terminal_widget, "Local Terminal")

            # Register terminal so theme/font updates affect existing local tabs
            window = self.window
            window.connection_to_terminals.setdefault(local_connection, []).append(terminal_widget)
            window.terminal_to_connection[terminal_widget] = local_connection
            window.active_terminals[local_connection] = terminal_widget

            GLib.idle_add(terminal_widget.show)
            GLib.idle_add(getattr(terminal_widget, 'show_terminal_widget', terminal_widget.show))
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
            if terminal_widget is None or not hasattr(terminal_widget, 'feed_child'):
                continue
            if hasattr(terminal_widget, 'connection'):
                if (hasattr(terminal_widget.connection, 'nickname') and
                        terminal_widget.connection.nickname == "Local Terminal"):
                    continue
                if not hasattr(terminal_widget.connection, 'hostname'):
                    continue
                try:
                    if not terminal_widget.feed_child(cmd):
                        failed_count += 1
                        continue
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
            host_value = getattr(terminal.connection, 'hostname', getattr(terminal.connection, 'host', getattr(terminal.connection, 'nickname', '')))
            logger.info(
                f"Terminal connected: {terminal.connection.nickname} ({terminal.connection.username}@{host_value})"
            )
        else:
            logger.debug(
                f"Terminal reconnected after settings update: {terminal.connection.nickname}")
        
        # Apply focus after connection is established (same as Ctrl+L + Enter method)
        def apply_focus_after_connection():
            try:
                # Use special focus method for pyxtermjs backend
                if hasattr(terminal, 'backend') and hasattr(terminal.backend, '_pyxterm'):
                    logger.debug("Using pyxtermjs special focus method")
                    # For pyxtermjs, use the special focus method with JavaScript injection
                    if hasattr(terminal.backend, 'grab_focus_with_js'):
                        terminal.backend.grab_focus_with_js()
                        logger.debug("Called grab_focus_with_js for pyxtermjs")
                    else:
                        logger.debug("grab_focus_with_js not available, using normal focus")
                        self.window._focus_terminal_widget(terminal)
                else:
                    logger.debug("Using normal focus method for non-pyxtermjs backend")
                    # For other backends, use the normal focus method
                    self.window._focus_terminal_widget(terminal)
            except Exception:
                logger.debug("Failed to apply focus after terminal connection", exc_info=True)
            return False  # Don't repeat
        
        # Use a longer delay for pyxtermjs backend to ensure WebView is fully ready
        delay = 500 if hasattr(terminal, 'backend') and hasattr(terminal.backend, '_pyxterm') else 100
        GLib.timeout_add(delay, apply_focus_after_connection)

    def on_terminal_disconnected(self, terminal):
        terminal.connection.is_connected = False
        if terminal.connection in self.window.connection_rows:
            row = self.window.connection_rows[terminal.connection]
            row.update_status()
            row.queue_draw()
        host_value = getattr(
            terminal.connection,
            'hostname',
            getattr(terminal.connection, 'host', getattr(terminal.connection, 'nickname', ''))
        )
        logger.info(
            f"Terminal disconnected: {terminal.connection.nickname} ({terminal.connection.username}@{host_value})"
        )

    def on_terminal_title_changed(self, terminal, title):
        page = self.window.tab_view.get_page(terminal)
        if page:
            if title and title != terminal.connection.nickname:
                page.set_title(f"{terminal.connection.nickname} - {title}")
            else:
                page.set_title(terminal.connection.nickname)

    def get_terminal_job_status(self, terminal):
        """
        Get the job status of a terminal.
        Only works for local terminals.
        
        Args:
            terminal: TerminalWidget instance
            
        Returns:
            dict: Job status information including idle state and status string
        """
        if not hasattr(terminal, 'is_terminal_idle'):
            return {
                'is_idle': False,
                'has_active_job': False,
                'status': 'UNKNOWN',
                'error': 'Terminal does not support job detection'
            }
        
        try:
            is_idle = terminal.is_terminal_idle()
            has_active_job = terminal.has_active_job()
            status = terminal.get_job_status()
            
            # Check if this is an SSH terminal
            is_ssh = status == "SSH_TERMINAL"
            
            return {
                'is_idle': is_idle,
                'has_active_job': has_active_job,
                'status': status,
                'is_ssh_terminal': is_ssh,
                'error': None
            }
        except Exception as e:
            return {
                'is_idle': False,
                'has_active_job': False,
                'status': 'ERROR',
                'is_ssh_terminal': False,
                'error': str(e)
            }

    def get_all_terminal_job_statuses(self):
        """
        Get job status for all active terminals.
        Only local terminals will have meaningful job detection.
        
        Returns:
            list: List of dicts with terminal info and job status
        """
        statuses = []
        
        for i in range(self.window.tab_view.get_n_pages()):
            page = self.window.tab_view.get_nth_page(i)
            if page is None:
                continue
                
            terminal_widget = page.get_child()
            if terminal_widget is None or not hasattr(terminal_widget, 'vte'):
                continue
            
            # Get connection info
            connection_info = terminal_widget.get_connection_info() if hasattr(terminal_widget, 'get_connection_info') else None
            
            # Get job status
            job_status = self.get_terminal_job_status(terminal_widget)
            
            statuses.append({
                'page_title': page.get_title(),
                'connection_info': connection_info,
                'job_status': job_status
            })
        
        return statuses
