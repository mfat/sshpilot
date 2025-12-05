import os
import asyncio
import logging
import math

import cairo


from gi.repository import Gio, GLib, Adw, Gdk, GdkPixbuf, Gtk
from gettext import gettext as _

from .terminal import TerminalWidget
from .preferences import should_hide_external_terminal_options

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

        # The user's "use external terminal" preference is only applied when
        # external terminal options are not hidden by policy or environment.
        use_external = window.config.get_setting('use-external-terminal', False)
        if use_external and not should_hide_external_terminal_options():
            window._open_connection_in_external_terminal(connection)
            return
        else:
            group_color, group_name = self._resolve_group_color_and_name(connection)

            terminal = TerminalWidget(
                connection,
                window.config,
                window.connection_manager,
                group_color=group_color,
            )
            terminal.connect('connection-established', self.on_terminal_connected)
            terminal.connect('connection-failed', lambda w, e: logger.error(f"Connection failed: {e}"))
            terminal.connect('connection-lost', self.on_terminal_disconnected)
            terminal.connect('title-changed', self.on_terminal_title_changed)

            from sshpilot import icon_utils
            page = window.tab_view.append(terminal)
            page.set_title(connection.nickname)
            page.set_icon(icon_utils.new_gicon_from_icon_name('utilities-terminal-symbolic'))
            if group_name:
                setattr(terminal, 'group_name', group_name)
            self._apply_tab_group_color(page, group_color, tooltip=group_name)


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
                terminal.vte.queue_draw()
                if not terminal._connect_ssh():
                    logger.error('Failed to establish SSH connection')
                    _cleanup_failed_terminal()
            except Exception as e:
                logger.error(f"Error setting terminal colors: {e}")
                if not terminal._connect_ssh():
                    logger.error('Failed to establish SSH connection')
                    _cleanup_failed_terminal()

        GLib.idle_add(_set_terminal_colors)

    def _resolve_group_color(self, connection):
        color_value, _ = self._resolve_group_color_and_name(connection)
        return color_value

    def _resolve_group_color_and_name(self, connection):
        manager = getattr(self.window, 'group_manager', None)
        if not manager:
            return None, None

        nickname = getattr(connection, 'nickname', None)
        if not nickname:
            return None, None

        try:
            group_id = manager.get_connection_group(nickname)
        except Exception:
            group_id = None

        visited = set()
        while group_id:
            if group_id in visited:
                break
            visited.add(group_id)

            try:
                group_info = getattr(manager, 'groups', {}).get(group_id)
            except Exception:
                group_info = None

            if not isinstance(group_info, dict):
                break

            color = group_info.get('color')
            if color:
                return color, group_info.get('name')

            group_id = group_info.get('parent_id')

        return None, None

    def _create_tab_color_icon(self, rgba: Gdk.RGBA):
        # Use a simple themed icon instead of creating custom icons
        # This avoids the pixbuf save issues and works reliably
        return Gio.ThemedIcon.new("media-record-symbolic")

    def _apply_tab_group_color(self, page, color_value, tooltip=None):
        use_pref = False
        try:
            use_pref = bool(
                self.window.config.get_setting('ui.use_group_color_in_tab', False)
            )
        except Exception:
            use_pref = False

        if not use_pref or not color_value:
            self._clear_tab_group_color(page)
            return

        tooltip_text = tooltip or _('Group color')

        rgba = Gdk.RGBA()
        try:
            if not rgba.parse(str(color_value)):
                self._clear_tab_group_color(page)
                return
        except Exception:
            self._clear_tab_group_color(page)
            return

        # Create a themed icon and apply color via CSS
        icon = self._create_tab_color_icon(rgba)
        if icon is None:
            self._clear_tab_group_color(page)
            return

        if hasattr(page, 'set_indicator_icon'):
            try:
                page.set_indicator_icon(icon)
                if hasattr(page, 'set_indicator_tooltip'):
                    page.set_indicator_tooltip(tooltip_text)

                # Apply color to the icon using CSS
                self._apply_tab_icon_color(page, rgba, tooltip_text)
                setattr(page, '_sshpilot_group_indicator_icon', icon)
                setattr(page, '_sshpilot_group_color', rgba.to_string())
            except Exception as exc:
                logger.debug("Failed to set tab indicator icon: %s", exc)
        elif hasattr(page, 'set_icon') and hasattr(page, 'get_icon'):
            try:
                if not hasattr(page, '_sshpilot_original_icon'):
                    setattr(page, '_sshpilot_original_icon', page.get_icon())
                page.set_icon(icon)

                if hasattr(page, 'set_indicator_tooltip'):
                    page.set_indicator_tooltip(tooltip_text)

                # Apply color to the icon using CSS
                self._apply_tab_icon_color(page, rgba, tooltip_text)
                setattr(page, '_sshpilot_group_indicator_icon', icon)
                setattr(page, '_sshpilot_group_color', rgba.to_string())
            except Exception as exc:
                logger.debug("Failed to set tab icon for group color: %s", exc)

    def _clear_tab_group_color(self, page):
        if hasattr(page, 'set_indicator_icon'):
            try:
                page.set_indicator_icon(None)
                if hasattr(page, 'set_indicator_tooltip'):
                    page.set_indicator_tooltip(None)
            except Exception:
                pass
        elif hasattr(page, 'set_icon') and hasattr(page, '_sshpilot_original_icon'):
            try:
                original = getattr(page, '_sshpilot_original_icon')
                if original is not None:
                    page.set_icon(original)
            except Exception:
                pass

        # Clear the custom icon from the tab page
        if hasattr(page, 'set_indicator_icon'):
            try:
                page.set_indicator_icon(None)
                if hasattr(page, 'set_indicator_tooltip'):
                    page.set_indicator_tooltip(None)
            except Exception:
                pass

        # No CSS provider to remove since we're using direct colored icons

        setattr(page, '_sshpilot_group_indicator_icon', None)
        if hasattr(page, '_sshpilot_group_color'):
            delattr(page, '_sshpilot_group_color')

    def _create_colored_tab_icon(self, rgba: Gdk.RGBA):
        """Create a simple colored icon for the tab indicator"""
        try:
            # Use a simple approach - create a basic colored icon
            # Convert RGBA to hex for CSS
            hex_color = f"#{int(rgba.red * 255):02x}{int(rgba.green * 255):02x}{int(rgba.blue * 255):02x}"
            
            # Create a simple SVG icon with the color
            svg_data = f"""<svg width="12" height="12" xmlns="http://www.w3.org/2000/svg">
                <circle cx="6" cy="6" r="5" fill="{hex_color}" stroke="none"/>
            </svg>"""
            
            # Create a bytes icon from the SVG
            svg_bytes = svg_data.encode('utf-8')
            return Gio.BytesIcon.new(GLib.Bytes.new(svg_bytes))
        except Exception as exc:
            logger.debug(f"Failed to create colored tab icon: {exc}")
            # Fallback to themed icon
            return Gio.ThemedIcon.new("media-record-symbolic")

    def _apply_tab_css_color(self, page, rgba: Gdk.RGBA):
        """Apply CSS color to the tab view to color indicator icons"""
        try:
            # Get the tab view
            tab_view = None
            try:
                if hasattr(self.window, 'tab_view'):
                    tab_view = self.window.tab_view
            except Exception:
                pass
            
            if not tab_view:
                return
            
            # Remove any existing CSS provider
            if hasattr(tab_view, '_sshpilot_tab_css_provider'):
                tab_view.get_style_context().remove_provider(tab_view._sshpilot_tab_css_provider)
            
            # Create new CSS provider with the group color
            provider = Gtk.CssProvider()
            color_value = rgba.to_string()
            
            # Use a simple CSS rule to color indicator icons
            css_data = f"tab page indicator-icon {{ color: {color_value}; }}"
            provider.load_from_data(css_data.encode())
            tab_view.get_style_context().add_provider(
                provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
            )
            tab_view._sshpilot_tab_css_provider = provider
            
        except Exception as exc:
            logger.debug(f"Failed to apply tab CSS color: {exc}")

    def _apply_tab_icon_color(self, page, rgba: Gdk.RGBA, tooltip_text=None):
        """Apply color to tab icon by setting a colored icon"""
        try:
            # Create a colored icon
            icon = self._create_colored_tab_icon(rgba)
            if icon is None:
                self._clear_tab_group_color(page)
                return
            
            # Set the colored icon on the tab page
            page.set_indicator_icon(icon)
            page.set_indicator_activatable(True)
            if hasattr(page, 'set_indicator_tooltip'):
                page.set_indicator_tooltip(tooltip_text or _('Group color'))

            # Store reference to the icon and color for cleanup
            setattr(page, '_sshpilot_group_indicator_icon', icon)
            setattr(page, '_sshpilot_group_color', rgba)
        except Exception as exc:
            logger.debug("Failed to apply tab icon color: %s", exc)
            self._clear_tab_group_color(page)

    def restyle_open_terminals(self):
        window = self.window
        n_pages = window.tab_view.get_n_pages() if hasattr(window.tab_view, 'get_n_pages') else 0
        for index in range(n_pages):
            try:
                page = window.tab_view.get_nth_page(index)
            except Exception:
                continue
            if not page:
                continue

            terminal = page.get_child() if hasattr(page, 'get_child') else None
            if terminal is None:
                continue

            connection = window.terminal_to_connection.get(terminal)
            if connection:
                color_value, group_name = self._resolve_group_color_and_name(connection)
                if group_name:
                    setattr(terminal, 'group_name', group_name)
            else:
                color_value, group_name = None, None

            if hasattr(terminal, 'set_group_color'):
                try:
                    terminal.set_group_color(color_value, force=True)
                except Exception as exc:
                    logger.debug("Failed to update terminal group color: %s", exc)

            tooltip = getattr(terminal, 'group_name', None)
            self._apply_tab_group_color(page, color_value, tooltip=tooltip)


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
            from sshpilot import icon_utils
            page = self.window.tab_view.append(terminal_widget)
            page.set_title(title)
            page.set_icon(icon_utils.new_gicon_from_icon_name('utilities-terminal-symbolic'))
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
            GLib.idle_add(terminal_widget.vte.show)

            def _focus_local_terminal():
                try:
                    if hasattr(terminal_widget, 'get_mapped') and not terminal_widget.get_mapped():
                        return
                    if hasattr(terminal_widget, 'vte') and hasattr(terminal_widget.vte, 'grab_focus'):
                        terminal_widget.vte.grab_focus()
                    elif hasattr(terminal_widget, 'grab_focus'):
                        terminal_widget.grab_focus()
                except Exception as focus_error:
                    logger.debug(f"Failed to focus local terminal: {focus_error}")

            # Use window's focus coordinator to avoid race conditions
            if hasattr(self.window, '_queue_focus_operation'):
                self.window._queue_focus_operation(_focus_local_terminal)
            else:
                # Fallback for older versions
                GLib.timeout_add(100, _focus_local_terminal)
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
                if not hasattr(terminal_widget.connection, 'hostname'):
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
            host_value = getattr(terminal.connection, 'hostname', getattr(terminal.connection, 'host', getattr(terminal.connection, 'nickname', '')))
            logger.info(
                f"Terminal connected: {terminal.connection.nickname} ({terminal.connection.username}@{host_value})"
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
