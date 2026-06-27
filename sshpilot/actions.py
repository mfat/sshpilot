"""Action handlers for MainWindow and registration helper."""

import logging
import os
import random
from typing import Optional
from gi.repository import Gio, Gtk, Adw, GLib, Gdk
from gettext import gettext as _

from .preferences import (
    should_hide_external_terminal_options,
    should_hide_file_manager_options,
)
from .shortcut_utils import get_primary_modifier_label
from .platform_utils import is_macos
from . import wol

HAS_NAV_SPLIT = hasattr(Adw, 'NavigationSplitView')
HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')

# Grace delay before the usage-tips banner eases into the update banner's area.
TIPS_BANNER_DELAY_SECONDS = 4

logger = logging.getLogger(__name__)


class WindowActions:
    """Mixin providing action handlers for :class:`MainWindow`."""

    def _update_sidebar_accelerators(self):
        """Apply sidebar accelerators respecting pass-through settings."""
        app = None
        try:
            if hasattr(self, 'get_application'):
                app = self.get_application()
        except Exception:
            app = None

        if not app:
            return

        # Delegate to the app's shortcut registry so config overrides from
        # the shortcut editor are respected (it also clears accels while
        # accelerators are suspended, e.g. terminal pass-through mode).
        if hasattr(app, '_apply_shortcut_for_action'):
            app._apply_shortcut_for_action('toggle_sidebar')
            return

        # Fallback for app objects without the registry (tests/fakes).
        shortcuts = ['F9']
        if is_macos():
            shortcuts.append('<Meta>b')

        enabled = getattr(app, 'accelerators_enabled', True)
        app.set_accels_for_action('win.toggle_sidebar', shortcuts if enabled else [])

    def on_toggle_sidebar_action(self, action, param):
        """Handle sidebar toggle action (for keyboard shortcuts)"""
        try:
            # Get current sidebar visibility
            if hasattr(self, 'split_view') and hasattr(self, '_toggle_sidebar_visibility'):
                split_variant = getattr(self, '_split_variant', '')
                
                if HAS_NAV_SPLIT and split_variant == 'navigation':
                    # NavigationSplitView doesn't have get_show_sidebar, use tracked state
                    current_visible = getattr(self, '_sidebar_visible', True)
                elif HAS_OVERLAY_SPLIT and split_variant == 'overlay':
                    # OverlaySplitView has get_show_sidebar
                    current_visible = self.split_view.get_show_sidebar()
                else:
                    # Fallback for Gtk.Paned
                    sidebar_widget = self.split_view.get_start_child()
                    current_visible = sidebar_widget.get_visible() if sidebar_widget else True

                # Toggle to opposite state
                new_visible = not current_visible

                # Update sidebar visibility
                self._toggle_sidebar_visibility(new_visible)

                # A manual toggle cancels any pending "hide on terminal open" delay.
                if hasattr(self, '_cancel_pending_sidebar_hide'):
                    self._cancel_pending_sidebar_hide()

                # Update button state if it exists (inverted logic: active = should hide)
                if hasattr(self, 'sidebar_toggle_button'):
                    self.sidebar_toggle_button.set_active(not new_visible)
        except Exception as e:
            logger.error(f"Failed to toggle sidebar via action: {e}")

    def on_open_new_connection_action(self, action, param=None):
        """Open a new tab for each targeted connection via context menu.

        Acts on the multi-selection when present, otherwise on the
        context-menu (or selected) connection.
        """
        try:
            # Prefer the snapshot taken when the context menu was opened.
            connections = list(getattr(self, '_context_menu_connections', None) or [])
            if not connections and hasattr(self, '_get_target_connections'):
                connections = self._get_target_connections(prefer_context=True)
            if not connections:
                connection = getattr(self, '_context_menu_connection', None)
                if connection is None:
                    row = self.connection_list.get_selected_row()
                    connection = getattr(row, 'connection', None) if row else None
                connections = [connection] if connection else []
            self._open_new_connection_tabs(connections)
        except Exception as e:
            logger.error(f"Failed to open new connection tab: {e}")

    def _open_new_connection_tabs(self, connections):
        """Open a new tab per connection, isolating per-connection failures."""
        if not connections:
            return
        if hasattr(self, '_return_to_tab_view_if_welcome'):
            self._return_to_tab_view_if_welcome()
        for connection in connections:
            try:
                self.terminal_manager.connect_to_host(connection, force_new=True)
            except Exception as e:
                logger.error(
                    "Failed to open tab for %s: %s",
                    getattr(connection, 'nickname', '?'), e,
                )

    def on_duplicate_connection_action(self, action, param=None):
        """Duplicate the currently selected connection."""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            if hasattr(self, 'duplicate_connection'):
                self.duplicate_connection(connection)
        except Exception as e:
            logger.error(f"Failed to duplicate connection: {e}")

    def on_open_new_connection_tab_action(self, action, param=None):
        """Open a tab for each selected connection via global shortcut (Ctrl/⌘+Alt+N)."""
        try:
            # Live selection only: a global shortcut should not inherit
            # context-menu targets.
            connections = self._connections_from_rows(
                self._get_selected_connection_rows()
            )
            if connections:
                self._open_new_connection_tabs(connections)
            else:
                # If no connection is selected, fall back to the new connection dialog
                logger.debug(
                    "No connection selected for %s+Alt+N, opening new connection dialog",
                    get_primary_modifier_label(),
                )
                self.show_connection_dialog()
        except Exception as e:
            logger.error(
                "Failed to open new connection tab with %s+Alt+N: %s",
                get_primary_modifier_label(),
                e,
            )

    def on_new_split_view_tab(self, action, param=None):
        """Open a new empty split-view tab."""
        try:
            from .split_view import SplitViewTab
            from sshpilot import icon_utils
            svt = SplitViewTab(self)
            page = self.tab_view.append(svt)
            page.set_title(_("Split View"))
            page.set_icon(icon_utils.new_gicon_from_icon_name('view-dual-symbolic'))
            svt._tab_page = page
            self.show_tab_view()
            self.tab_view.set_selected_page(page)
        except Exception as exc:
            logger.error("Failed to open new split view tab: %s", exc)

    def on_open_in_split_view_action(self, action, param=None):
        """Open the selected connection(s) in a new split-view tab."""
        try:
            from .split_view import SplitViewTab
            from sshpilot import icon_utils

            # Collect connections from the current selection, falling back to the
            # context-menu connection when nothing specific is selected.
            connections = []
            try:
                selected_rows = list(self.connection_list.get_selected_rows())
                for r in selected_rows:
                    conn = getattr(r, 'connection', None)
                    if conn is not None:
                        connections.append(conn)
            except Exception:
                pass

            if not connections:
                conn = getattr(self, '_context_menu_connection', None)
                if conn is not None:
                    connections.append(conn)

            if not connections:
                return

            svt = SplitViewTab(self)
            page = self.tab_view.append(svt)
            page.set_title(_("Split View"))
            page.set_icon(icon_utils.new_gicon_from_icon_name('view-dual-symbolic'))
            svt._tab_page = page
            svt.populate(connections)
            self.show_tab_view()
            self.tab_view.set_selected_page(page)
        except Exception as exc:
            logger.error("Failed to open connections in split view: %s", exc)

    def on_open_group_in_split_view_action(self, action, param=None):
        """Open all connections in a group as a new split-view tab."""
        try:
            from .split_view import SplitViewTab
            from sshpilot import icon_utils

            group_row = getattr(self, '_context_menu_group_row', None)
            if group_row is None:
                return

            group_info = group_row.group_info
            connections = []
            for nick in group_info.get('connections', []):
                conn = self.connection_manager.find_connection_by_nickname(nick)
                if conn is not None:
                    connections.append(conn)

            if not connections:
                return

            svt = SplitViewTab(self)
            page = self.tab_view.append(svt)
            name = group_info.get('name', '')
            page.set_title(_("Split View — {name}").format(name=name) if name else _("Split View"))
            page.set_icon(icon_utils.new_gicon_from_icon_name('view-dual-symbolic'))
            svt._tab_page = page
            svt.populate(connections)
            self.show_tab_view()
            self.tab_view.set_selected_page(page)
        except Exception as exc:
            logger.error("Failed to open group in split view: %s", exc)

    def on_copy_key_to_server_action(self, action, param=None):
        """Handle copy key to server action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return

            from .plugins.api import Capability
            from .plugins.registry import capabilities_for
            if Capability.KEY_DEPLOYMENT not in capabilities_for(connection):
                logger.debug("ssh-copy-id unavailable: protocol %r has no key deployment",
                             getattr(connection, 'protocol', 'ssh'))
                return

            # Open the copy key window directly
            from .sshcopyid_window import SshCopyIdWindow
            win = SshCopyIdWindow(self, connection, self.key_manager, self.connection_manager)
            win.present()
        except Exception as e:
            logger.error(f"Failed to copy key to server: {e}")
            # Show error dialog
            try:
                error_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Error"),
                    body=_("Could not open the Copy Key window.\n\n{}").format(str(e))
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()
            except Exception:
                pass

    def on_edit_connection_action(self, action, param=None):
        """Handle edit connection action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            self.show_connection_dialog(connection)
        except Exception as e:
            logger.error(f"Failed to edit connection: {e}")

    def on_delete_connection_action(self, action, param=None):
        """Handle delete connection action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return

            # Use the same logic as the button click handler
            # If host has active connections/tabs, warn about closing them first
            has_active_terms = bool(self.connection_to_terminals.get(connection, []))
            if getattr(connection, 'is_connected', False) or has_active_terms:
                dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_('Remove host?'),
                    body=_('Close connections and remove host?')
                )
                dialog.add_response('cancel', _('Cancel'))
                dialog.add_response('close_remove', _('Close and Remove'))
                dialog.set_response_appearance('close_remove', Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response('close')
                dialog.set_close_response('cancel')
            else:
                # Simple delete confirmation when not connected
                dialog = Adw.MessageDialog.new(self, _('Delete Connection?'),
                                             _('Are you sure you want to delete "{}"?').format(connection.nickname))
                dialog.add_response('cancel', _('Cancel'))
                dialog.add_response('delete', _('Delete'))
                dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response('cancel')
                dialog.set_close_response('cancel')

            dialog.connect('response', self.on_delete_connection_response, connection)
            dialog.present()
        except Exception as e:
            logger.error(f"Failed to delete connection: {e}")

    def on_open_in_system_terminal_action(self, action, param=None):
        """Handle open in system terminal action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return

            self.open_in_system_terminal(connection)
        except Exception as e:
            logger.error(f"Failed to open in system terminal: {e}")

    def on_wake_on_lan_action(self, action, param=None):
        """Send Wake-on-LAN magic packets for the targeted connections.

        Acts on the multi-selection when present (connections without a
        stored MAC are skipped), otherwise on the context-menu connection.
        """
        try:
            config = getattr(self, 'config', None)
            if not config:
                return
            # Prefer the snapshot taken when the context menu was opened.
            connections = list(getattr(self, '_context_menu_connections', None) or [])
            if not connections and hasattr(self, '_get_target_connections'):
                connections = self._get_target_connections(prefer_context=True)
            if not connections:
                connection = getattr(self, '_context_menu_connection', None)
                if connection is None:
                    row = self.connection_list.get_selected_row()
                    connection = getattr(row, 'connection', None) if row else None
                connections = [connection] if connection else []
            sent = 0
            failures = []
            for connection in connections:
                try:
                    nickname = getattr(connection, 'nickname', '').strip() if connection else ''
                    if not nickname:
                        continue
                    meta = config.get_connection_meta(nickname)
                    mac = (meta.get('wol_mac') or '').strip()
                    if not mac:
                        continue
                    broadcast = (meta.get('wol_broadcast_ip') or '').strip() or None
                    try:
                        port = int(meta.get('wol_port', 9) or 9)
                    except (TypeError, ValueError):
                        port = 9
                    host = getattr(connection, 'hostname', None) or getattr(connection, 'host', None)
                    host_str = (host or '').strip() or None
                    ok, msg = wol.send_wol(mac, broadcast_ip=broadcast, port=port, host=host_str)
                    if ok:
                        sent += 1
                    else:
                        failures.append(f"{nickname}: {msg}")
                except Exception as e:
                    failures.append(f"{getattr(connection, 'nickname', '?')}: {e}")
            if sent == 0 and not failures:
                return
            toast_overlay = getattr(self, 'toast_overlay', None)
            if toast_overlay:
                if failures:
                    toast_msg = _("Wake-on-LAN failed: %s") % "; ".join(failures)
                elif sent == 1:
                    toast_msg = _("Wake-on-LAN sent")
                else:
                    toast_msg = _("Wake-on-LAN sent to {n} hosts").format(n=sent)
                toast = Adw.Toast.new(toast_msg)
                toast.set_timeout(4 if failures else 3)
                toast_overlay.add_toast(toast)
        except Exception as e:
            logger.debug("WoL action: %s", e)

    def on_sort_connections_action(self, action, param=None):
        """Apply a requested connection sort preset."""
        try:
            preset_id = param.get_string() if param is not None else None
        except AttributeError:
            preset_id = None

        if not preset_id:
            return

        if hasattr(self, 'apply_connection_sort_preset'):
            self.apply_connection_sort_preset(preset_id)

    def on_edit_known_hosts_action(self, action, param=None):
        """Open the known hosts editor window."""
        try:
            if hasattr(self, 'show_known_hosts_editor'):
                self.show_known_hosts_editor()
        except Exception as e:
            logger.error(f"Failed to open known hosts editor: {e}")

    def on_create_group_action(self, action, param=None):
        """Handle create group action"""
        try:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Create Group'),
                body=_('Enter a name for the new group:'),
            )

            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content_box.set_margin_start(20)
            content_box.set_margin_end(20)
            content_box.set_margin_top(20)
            content_box.set_margin_bottom(20)

            entry = Gtk.Entry()
            entry.set_placeholder_text(_('e.g., Work Servers'))
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            content_box.append(entry)

            color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            color_row.set_hexpand(True)
            color_label = Gtk.Label(label=_("Group color"))
            color_label.set_xalign(0)
            color_label.set_hexpand(True)
            color_row.append(color_label)

            color_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            color_button = Gtk.ColorButton()
            color_button.set_use_alpha(True)
            color_button.set_title(_("Select group color"))
            default_rgba = Gdk.RGBA()
            default_rgba.red = default_rgba.green = default_rgba.blue = 0
            default_rgba.alpha = 0
            color_button.set_rgba(default_rgba)
            color_controls.append(color_button)

            color_selected = False

            def mark_color_selected(_button):
                nonlocal color_selected
                color_selected = True

            color_button.connect('color-set', mark_color_selected)

            def reset_color_selection() -> None:
                nonlocal color_selected
                color_selected = False
                cleared = Gdk.RGBA()
                cleared.red = cleared.green = cleared.blue = 0
                cleared.alpha = 0
                color_button.set_rgba(cleared)

            clear_color_button = Gtk.Button(label=_("Clear"))
            clear_color_button.add_css_class('flat')
            clear_color_button.connect('clicked', lambda _btn: reset_color_selection())
            color_controls.append(clear_color_button)

            color_row.append(color_controls)
            content_box.append(color_row)

            dialog.set_extra_child(content_box)

            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('create', _('Create'))
            dialog.set_response_appearance('create', Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response('create')
            dialog.set_close_response('cancel')

            def on_response(dialog, response):
                if response == 'create':
                    name = entry.get_text().strip()
                    if name:
                        selected_color = None
                        rgba_value = color_button.get_rgba()
                        if color_selected and rgba_value.alpha > 0:
                            selected_color = rgba_value.to_string()
                        self.group_manager.create_group(name, color=selected_color)
                        self.rebuild_connection_list()
                    else:
                        error_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Error"),
                            body=_("Please enter a group name."),
                        )
                        error_dialog.add_response('ok', _('OK'))
                        error_dialog.present()
                dialog.destroy()

            dialog.connect('response', on_response)
            dialog.present()

            def focus_entry():
                entry.grab_focus()
                return False

            GLib.idle_add(focus_entry)

        except Exception as e:
            logger.error(f"Failed to show create group dialog: {e}")

    def on_edit_group_action(self, action, param=None):
        """Handle edit group action"""
        try:
            selected_row = getattr(self, '_context_menu_group_row', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
            if not selected_row or not hasattr(selected_row, 'group_id'):
                return

            group_id = selected_row.group_id
            group_info = self.group_manager.groups.get(group_id)
            if not group_info:
                return

            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Edit Group'),
                body=_('Enter a new name for the group:'),
            )

            entry = Gtk.Entry(text=group_info['name'])
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            dialog.set_extra_child(entry)

            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('save', _('Save'))
            dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response('save')
            dialog.set_close_response('cancel')

            def on_response(dialog, response):
                if response == 'save':
                    new_name = entry.get_text().strip()
                    if new_name:
                        group_info['name'] = new_name
                        self.group_manager._save_groups()
                        self.rebuild_connection_list()
                    else:
                        error_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Error"),
                            body=_("Please enter a group name."),
                        )
                        error_dialog.add_response('ok', _('OK'))
                        error_dialog.present()
                dialog.destroy()

            dialog.connect('response', on_response)
            dialog.present()

            def focus_entry():
                entry.grab_focus()
                entry.select_region(0, -1)
                return False

            GLib.idle_add(focus_entry)

        except Exception as e:
            logger.error(f"Failed to show edit group dialog: {e}")

    def on_delete_group_action(self, action, param=None):
        """Handle delete group action"""
        try:
            # Get the group row from context menu or selected row
            selected_row = getattr(self, '_context_menu_group_row', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
            if not selected_row or not hasattr(selected_row, 'group_id'):
                return

            group_id = selected_row.group_id
            group_info = self.group_manager.groups.get(group_id)
            if not group_info:
                return

            # Check if group has connections (filter to only existing connections)
            all_connections = self.connection_manager.get_connections()
            connections_dict = {conn.nickname: conn for conn in all_connections}
            
            actual_connections = [
                c
                for c in group_info.get('connections', [])
                if c in connections_dict
            ]
            connection_count = len(actual_connections)
            
            if connection_count > 0:
                # Show dialog asking what to do with connections
                dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Delete Group"),
                    body=_("The group '{}' contains {} connection(s).\n\nWhat would you like to do with the connections?").format(
                        group_info['name'], connection_count
                    )
                )
                
                dialog.add_response('cancel', _('Cancel'))
                dialog.add_response('move', _('Move to Parent/Ungrouped'))
                dialog.add_response('delete_all', _('Delete All Connections'))
                dialog.set_response_appearance('delete_all', Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response('move')
                
                def on_response_with_connections(dialog, response):
                    if response == 'move':
                        # Just delete the group, connections will be moved
                        self.group_manager.delete_group(group_id)
                        self.rebuild_connection_list()
                    elif response == 'delete_all':
                        # Delete all connections in the group first
                        connections_to_delete = list(actual_connections)  # Use filtered list
                        for conn_nickname in connections_to_delete:
                            # Find the connection object and delete it
                            connection = connections_dict.get(conn_nickname)
                            if connection:
                                self.connection_manager.remove_connection(connection)
                        
                        # Then delete the group
                        self.group_manager.delete_group(group_id)
                        self.rebuild_connection_list()
                    dialog.destroy()
                
                dialog.connect('response', on_response_with_connections)
                dialog.present()
            else:
                # Group is empty, show simple confirmation
                dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Delete Group"),
                    body=_("Are you sure you want to delete the empty group '{}'?").format(group_info['name'])
                )
                
                dialog.add_response('cancel', _('Cancel'))
                dialog.add_response('delete', _('Delete'))
                dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response('cancel')
                
                def on_response_empty_group(dialog, response):
                    if response == 'delete':
                        self.group_manager.delete_group(group_id)
                        self.rebuild_connection_list()
                    dialog.destroy()
                
                dialog.connect('response', on_response_empty_group)
                dialog.present()

        except Exception as e:
            logger.error(f"Failed to show delete group dialog: {e}")

    def on_move_to_ungrouped_action(self, action, param=None):
        """Handle move to ungrouped action"""
        try:
            # Get the connection from context menu or selected row
            selected_row = getattr(self, '_context_menu_connection', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
                if selected_row and hasattr(selected_row, 'connection'):
                    selected_row = selected_row.connection

            if not selected_row:
                return

            connection_nickname = selected_row.nickname if hasattr(selected_row, 'nickname') else selected_row

            # Move to ungrouped (None group)
            self.group_manager.move_connection(connection_nickname, None)
            self.rebuild_connection_list()

        except Exception as e:
            logger.error(f"Failed to move connection to ungrouped: {e}")

    def on_export_config_action(self, action, param=None):
        """Handle export configuration action"""
        try:
            if hasattr(self, 'show_export_dialog'):
                self.show_export_dialog()
        except Exception as e:
            logger.error(f"Failed to show export dialog: {e}")

    def on_import_config_action(self, action, param=None):
        """Handle import configuration action"""
        try:
            if hasattr(self, 'show_import_dialog'):
                self.show_import_dialog()
        except Exception as e:
            logger.error(f"Failed to show import dialog: {e}")

    def on_save_session_action(self, action, param=None):
        """Prompt for a name and save the current set of open tabs as a session."""
        session_manager = getattr(self, 'session_manager', None)
        if session_manager is None:
            return

        dialog = Adw.MessageDialog(
            transient_for=self,
            modal=True,
            heading=_("Save Session"),
            body=_("Enter a name for this session. Saving with an existing name overwrites it."),
        )
        entry = Gtk.Entry()
        entry.set_placeholder_text(_("Session name"))
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', _("Cancel"))
        dialog.add_response('save', _("Save"))
        dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('save')
        dialog.set_close_response('cancel')

        def _do_save(name):
            try:
                session_manager.save_session(name, self.capture_session())
            except Exception as exc:
                logger.error(f"Failed to save session '{name}': {exc}")

        def _on_response(dlg, response):
            if response != 'save':
                return
            name = entry.get_text().strip()
            if not name:
                return
            if session_manager.has_session(name):
                confirm = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Overwrite Session?"),
                    body=_('A session named "{}" already exists. Overwrite it?').format(name),
                )
                confirm.add_response('cancel', _("Cancel"))
                confirm.add_response('overwrite', _("Overwrite"))
                confirm.set_response_appearance('overwrite', Adw.ResponseAppearance.DESTRUCTIVE)
                confirm.set_close_response('cancel')
                confirm.connect('response', lambda d, r: _do_save(name) if r == 'overwrite' else None)
                confirm.present()
            else:
                _do_save(name)

        dialog.connect('response', _on_response)
        dialog.present()

    def on_open_session_action(self, action, param=None):
        """Show a list of saved sessions and open the selected one."""
        session_manager = getattr(self, 'session_manager', None)
        if session_manager is None:
            return

        names = session_manager.list_session_names()
        if not names:
            info = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("No Saved Sessions"),
                body=_("You have not saved any sessions yet."),
            )
            info.add_response('ok', _("OK"))
            info.present()
            return

        dialog = Adw.MessageDialog(
            transient_for=self,
            modal=True,
            heading=_("Open Session"),
            body=_("Select a session to open:"),
        )
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        listbox.add_css_class('boxed-list')
        for name in names:
            row = Adw.ActionRow(title=name)
            row._session_name = name
            listbox.append(row)
        first_row = listbox.get_row_at_index(0)
        if first_row is not None:
            listbox.select_row(first_row)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(180)
        scroller.set_child(listbox)
        dialog.set_extra_child(scroller)

        dialog.add_response('cancel', _("Cancel"))
        dialog.add_response('open', _("Open"))
        dialog.set_response_appearance('open', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('open')
        dialog.set_close_response('cancel')

        def _on_response(dlg, response):
            if response != 'open':
                return
            row = listbox.get_selected_row()
            if row is None:
                return
            name = getattr(row, '_session_name', None)
            if not name:
                return
            data = session_manager.get_session(name)
            if not data:
                return
            self._prompt_open_session(name, data)

        dialog.connect('response', _on_response)
        dialog.present()

    def _prompt_open_session(self, name, data):
        """Open a session, prompting to replace or add when tabs are already open."""
        try:
            has_open = self.tab_view.get_n_pages() > 0
        except Exception:
            has_open = False

        if not has_open:
            self.restore_session(data, replace=True)
            return

        dialog = Adw.MessageDialog(
            transient_for=self,
            modal=True,
            heading=_("Open Session"),
            body=_('Replace the current tabs with session "{}", or add it to the current tabs?').format(name),
        )
        dialog.add_response('cancel', _("Cancel"))
        dialog.add_response('add', _("Add to Current"))
        dialog.add_response('replace', _("Replace"))
        dialog.set_response_appearance('replace', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('replace')
        dialog.set_close_response('cancel')

        def _on_response(dlg, response):
            if response == 'replace':
                self.restore_session(data, replace=True)
            elif response == 'add':
                self.restore_session(data, replace=False)

        dialog.connect('response', _on_response)
        dialog.present()

    def on_manage_sessions_action(self, action, param=None):
        """Open a manager to rename, delete, or pin saved sessions."""
        session_manager = getattr(self, 'session_manager', None)
        if session_manager is None:
            return

        existing = getattr(self, '_session_manager_window', None)
        if existing is not None:
            try:
                existing.present()
                return
            except Exception:
                self._session_manager_window = None

        window = Adw.Window(transient_for=self, modal=True)
        window.set_title(_("Session Manager"))
        window.set_default_size(480, 460)
        self._session_manager_window = window

        def _on_closed(_w):
            self._session_manager_window = None

        window.connect('close-request', lambda _w: (_on_closed(_w), False)[1])

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        clamp = Adw.Clamp()
        clamp.set_margin_top(18)
        clamp.set_margin_bottom(18)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(content_box)
        scroller.set_child(clamp)
        toolbar_view.set_content(scroller)
        window.set_content(toolbar_view)

        def rebuild():
            child = content_box.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                content_box.remove(child)
                child = nxt

            names = session_manager.list_session_names()
            if not names:
                status = Adw.StatusPage()
                status.set_icon_name('document-open-recent-symbolic')
                status.set_title(_("No Saved Sessions"))
                status.set_description(_("Use Save Session to capture your open tabs."))
                status.set_vexpand(True)
                content_box.append(status)
                return

            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            listbox.add_css_class('boxed-list')
            for name in names:
                listbox.append(self._build_session_manager_row(name, rebuild))
            content_box.append(listbox)

        rebuild()
        window.present()

    def _build_session_manager_row(self, name, rebuild):
        """Build an action row for one session with pin/rename/delete controls."""
        session_manager = self.session_manager
        row = Adw.ActionRow()
        row.set_title(name)
        payload = session_manager.get_session(name) or {}
        tab_count = len(payload.get('tabs', []) if isinstance(payload, dict) else [])
        row.set_subtitle(_("{n} tab(s)").format(n=tab_count))

        from sshpilot import icon_utils

        pin_button = Gtk.ToggleButton()
        icon_utils.set_button_icon(pin_button, 'view-pin-symbolic')
        pin_button.set_tooltip_text(_("Pin to start page"))
        pin_button.set_valign(Gtk.Align.CENTER)
        pin_button.add_css_class('flat')
        pin_button.set_active(session_manager.is_pinned(name))

        def _on_pin_toggled(btn):
            session_manager.set_pinned(name, btn.get_active())
            self._refresh_pinned_sessions()

        pin_button.connect('toggled', _on_pin_toggled)
        row.add_suffix(pin_button)

        rename_button = Gtk.Button()
        icon_utils.set_button_icon(rename_button, 'document-edit-symbolic')
        rename_button.set_tooltip_text(_("Rename"))
        rename_button.set_valign(Gtk.Align.CENTER)
        rename_button.add_css_class('flat')
        rename_button.connect('clicked', lambda _b: self._prompt_rename_session(name, rebuild))
        row.add_suffix(rename_button)

        delete_button = Gtk.Button()
        icon_utils.set_button_icon(delete_button, 'user-trash-symbolic')
        delete_button.set_tooltip_text(_("Delete"))
        delete_button.set_valign(Gtk.Align.CENTER)
        delete_button.add_css_class('flat')
        delete_button.connect('clicked', lambda _b: self._prompt_delete_session(name, rebuild))
        row.add_suffix(delete_button)

        return row

    def _prompt_rename_session(self, name, rebuild):
        session_manager = self.session_manager
        parent = getattr(self, '_session_manager_window', None) or self
        dialog = Adw.MessageDialog(
            transient_for=parent,
            modal=True,
            heading=_("Rename Session"),
            body=_('Enter a new name for "{}".').format(name),
        )
        entry = Gtk.Entry()
        entry.set_text(name)
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', _("Cancel"))
        dialog.add_response('rename', _("Rename"))
        dialog.set_response_appearance('rename', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('rename')
        dialog.set_close_response('cancel')

        def _on_response(dlg, response):
            if response != 'rename':
                return
            new_name = entry.get_text().strip()
            if not new_name or new_name == name:
                return
            try:
                session_manager.rename_session(name, new_name)
            except Exception as exc:
                self._show_session_error(_("Could not rename session"), str(exc))
                return
            rebuild()
            self._refresh_pinned_sessions()

        dialog.connect('response', _on_response)
        dialog.present()

    def _prompt_delete_session(self, name, rebuild):
        session_manager = self.session_manager
        parent = getattr(self, '_session_manager_window', None) or self
        dialog = Adw.MessageDialog(
            transient_for=parent,
            modal=True,
            heading=_("Delete Session?"),
            body=_('The session "{}" will be permanently deleted.').format(name),
        )
        dialog.add_response('cancel', _("Cancel"))
        dialog.add_response('delete', _("Delete"))
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')

        def _on_response(dlg, response):
            if response != 'delete':
                return
            session_manager.delete_session(name)
            rebuild()
            self._refresh_pinned_sessions()

        dialog.connect('response', _on_response)
        dialog.present()

    def _show_session_error(self, heading, body):
        parent = getattr(self, '_session_manager_window', None) or self
        dialog = Adw.MessageDialog(
            transient_for=parent,
            modal=True,
            heading=heading,
            body=body,
        )
        dialog.add_response('ok', _("OK"))
        dialog.present()

    def _refresh_pinned_sessions(self):
        """Refresh the start page so pinned-session changes are reflected."""
        try:
            if hasattr(self, 'welcome_view') and self.welcome_view:
                self.welcome_view.refresh_pinned()
        except Exception as exc:
            logger.debug(f"Failed to refresh pinned sessions: {exc}")

    def on_move_to_group_action(self, action, param=None):
        """Handle move to group action"""
        try:
            # Get the connection from context menu or selected row
            selected_row = getattr(self, '_context_menu_connection', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
                if selected_row and hasattr(selected_row, 'connection'):
                    selected_row = selected_row.connection

            if not selected_row:
                return

            connection_nickname = selected_row.nickname if hasattr(selected_row, 'nickname') else selected_row

            # Get available groups
            available_groups = self.get_available_groups()
            logger.debug(f"Available groups for move dialog: {len(available_groups)} groups")

            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Move to Group"),
                body=_("Select a group to move the connection to:"),
            )

            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content_box.set_margin_start(20)
            content_box.set_margin_end(20)
            content_box.set_margin_top(20)
            content_box.set_margin_bottom(20)

            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
            listbox.set_vexpand(True)

            create_section_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            create_section_box.set_margin_start(12)
            create_section_box.set_margin_end(12)
            create_section_box.set_margin_top(6)
            create_section_box.set_margin_bottom(6)

            create_label = Gtk.Label(label=_("Create New Group"))
            create_label.set_xalign(0)
            create_label.add_css_class("heading")
            create_section_box.append(create_label)

            create_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

            create_group_entry = Gtk.Entry()
            create_group_entry.set_placeholder_text(_("Enter group name"))
            create_group_entry.set_hexpand(True)
            create_group_entry.set_activates_default(True)
            create_box.append(create_group_entry)

            create_section_box.append(create_box)

            color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            color_row.set_hexpand(True)
            color_label = Gtk.Label(label=_("Group color"))
            color_label.set_xalign(0)
            color_label.set_hexpand(True)
            color_row.append(color_label)

            color_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            color_button = Gtk.ColorButton()
            color_button.set_use_alpha(True)
            color_button.set_title(_("Select group color"))
            initial_rgba = Gdk.RGBA()
            initial_rgba.red = initial_rgba.green = initial_rgba.blue = 0
            initial_rgba.alpha = 0
            color_button.set_rgba(initial_rgba)
            color_controls.append(color_button)

            color_selected = False

            def mark_color_selected(_button):
                nonlocal color_selected
                color_selected = True

            color_button.connect('color-set', mark_color_selected)

            def reset_color_selection() -> None:
                nonlocal color_selected
                color_selected = False
                cleared = Gdk.RGBA()
                cleared.red = cleared.green = cleared.blue = 0
                cleared.alpha = 0
                color_button.set_rgba(cleared)

            clear_color_button = Gtk.Button(label=_("Clear"))
            clear_color_button.add_css_class('flat')
            clear_color_button.connect('clicked', lambda _btn: reset_color_selection())
            color_controls.append(clear_color_button)

            color_row.append(color_controls)
            create_section_box.append(color_row)
            content_box.append(create_section_box)

            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            content_box.append(separator)

            if available_groups:
                existing_label = Gtk.Label(label=_("Existing Groups"))
                existing_label.set_xalign(0)
                existing_label.add_css_class("heading")
                content_box.append(existing_label)

            for group in available_groups:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

                from sshpilot import icon_utils
                icon = icon_utils.new_image_from_icon_name('folder-symbolic')
                icon.set_pixel_size(16)
                box.append(icon)

                name_label = Gtk.Label(label=group['name'])
                name_label.set_xalign(0)
                name_label.set_hexpand(True)
                box.append(name_label)

                row.set_child(box)
                row.group_id = group['id']
                listbox.append(row)

            content_box.append(listbox)

            dialog.set_extra_child(content_box)

            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('move', _('Move'))
            dialog.set_response_appearance('move', Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response('move')
            dialog.set_close_response('cancel')

            # Connect entry and button events
            def find_existing_group_id(name: str) -> Optional[str]:
                lowered = name.lower()
                for group in available_groups:
                    if group['name'].lower() == lowered:
                        return group['id']
                return None

            def has_valid_target() -> bool:
                if create_group_entry.get_text().strip():
                    return True
                return listbox.get_selected_row() is not None

            def update_move_response_state(*_args) -> None:
                dialog.set_response_enabled('move', has_valid_target())

            listbox.connect('row-selected', lambda _lb, _row: update_move_response_state())

            def on_entry_changed(_entry):
                update_move_response_state()

            def on_entry_activated(_entry):
                if has_valid_target():
                    dialog.response('move')

            create_group_entry.connect('changed', on_entry_changed)
            create_group_entry.connect('activate', on_entry_activated)
            update_move_response_state()

            def perform_move() -> bool:
                group_name = create_group_entry.get_text().strip()
                if group_name:
                    existing_group_id = find_existing_group_id(group_name)
                    if existing_group_id:
                        self.group_manager.move_connection(connection_nickname, existing_group_id)
                        self.rebuild_connection_list()
                        return True
                    try:
                        selected_color = None
                        rgba_value = color_button.get_rgba()
                        if color_selected and rgba_value.alpha > 0:
                            selected_color = rgba_value.to_string()
                        new_group_id = self.group_manager.create_group(group_name, color=selected_color)
                        self.group_manager.move_connection(connection_nickname, new_group_id)
                        self.rebuild_connection_list()
                        return True
                    except ValueError as e:
                        error_dialog = Gtk.Dialog(
                            title=_("Group Already Exists"),
                            transient_for=dialog,
                            modal=True,
                            destroy_with_parent=True
                        )
                        error_dialog.set_default_size(400, 150)
                        error_dialog.set_resizable(False)

                        content_area = error_dialog.get_content_area()
                        content_area.set_margin_start(20)
                        content_area.set_margin_end(20)
                        content_area.set_margin_top(20)
                        content_area.set_margin_bottom(20)

                        error_label = Gtk.Label(label=str(e))
                        error_label.set_wrap(True)
                        error_label.set_xalign(0)
                        content_area.append(error_label)

                        error_dialog.add_button(_('OK'), Gtk.ResponseType.OK)
                        error_dialog.set_default_response(Gtk.ResponseType.OK)

                        def on_error_response(dialog, _response):
                            dialog.destroy()

                        error_dialog.connect('response', on_error_response)
                        error_dialog.present()

                        create_group_entry.set_text("")
                        reset_color_selection()
                        create_group_entry.grab_focus()
                        update_move_response_state()
                        return False

                selected_row = listbox.get_selected_row()
                if selected_row:
                    target_group_id = selected_row.group_id
                    self.group_manager.move_connection(connection_nickname, target_group_id)
                    self.rebuild_connection_list()
                    return True
                return False

            def on_response(dialog, response):
                if response == 'move':
                    if perform_move():
                        dialog.destroy()
                        return
                    return
                dialog.destroy()

            dialog.connect('response', on_response)
            dialog.present()

        except Exception as e:
            logger.error(f"Failed to show move to group dialog: {e}")

    def on_check_for_updates_action(self, action, param=None):
        """Handle check for updates action from menu"""
        logger.info("Checking for updates...")
        
        # Import here to avoid circular imports
        from .update_checker import check_for_updates_async
        
        def on_update_check_complete(latest_version):
            """Callback when update check completes"""
            GLib.idle_add(self._handle_update_check_result, latest_version)
        
        # Check for updates in background
        check_for_updates_async(on_update_check_complete)
    
    def _handle_update_check_result(self, latest_version, from_startup=False):
        """Handle the result of an update check (runs on main thread).

        ``from_startup`` is True for the automatic check run at startup; it
        suppresses the "you're running the latest version" toast (which would be
        noise on every launch) while still surfacing the tips banner.
        """
        if latest_version:
            self._latest_version = latest_version
            self._show_update_banner(latest_version)
        else:
            # No update available - tell the user (unless this was the silent
            # startup check) ...
            if not from_startup:
                toast = Adw.Toast.new("You're running the latest version")
                toast.set_timeout(3)
                if hasattr(self, 'toast_overlay'):
                    self.toast_overlay.add_toast(toast)
            # ... and free up the banner area for a usage tip.
            self._maybe_show_tips_banner()
    
    def _show_update_banner(self, version):
        """Show the update notification banner"""
        if not self.update_banner:
            return
        
        title = f"SSH Pilot {version} is available!"
        
        self.update_banner.set_title(title)
        self.update_banner.set_button_label("Download")
        
        # Apply CSS styling for blue button
        self._apply_update_banner_css()
        
        # Connect button clicked signal
        try:
            # Disconnect any previous handler
            if hasattr(self, '_update_banner_handler_id'):
                self.update_banner.disconnect(self._update_banner_handler_id)
        except Exception:
            pass
        
        self._update_banner_handler_id = self.update_banner.connect(
            'button-clicked',
            self._on_update_banner_clicked
        )
        
        # The update banner takes priority over the tips banner — hide tips so
        # the two never stack in the same area.
        self._hide_tips_banner()

        # Show the banner and its container
        self.update_banner.set_revealed(True)
        if hasattr(self, 'update_banner_container'):
            self.update_banner_container.set_visible(True)

    def _on_update_banner_clicked(self, banner):
        """Handle update banner button click"""
        # Import here to avoid circular imports
        from .update_checker import get_update_url
        
        url = get_update_url()
        logger.info(f"Opening update URL: {url}")
        
        try:
            Gtk.show_uri(self, url, Gdk.CURRENT_TIME)
        except Exception as e:
            logger.error(f"Failed to open update URL: {e}")
    
    def _on_update_banner_dismiss(self, button):
        """Handle dismiss button click on update banner"""
        logger.info("Update banner dismissed by user")
        self.update_banner.set_revealed(False)
        if hasattr(self, 'update_banner_container'):
            self.update_banner_container.set_visible(False)
        # Now that the update banner is gone, surface a usage tip in its place.
        self._maybe_show_tips_banner()

    # --- Terminal tips banner (shares the update banner's area) ---------------

    def _build_window_tips(self):
        """Return the usage tips shown in the banner area.

        Tips are read from ``sshpilot/resources/tips.md`` — one tip per line — so
        they can be added or edited without touching the source. That file lives
        in the bundled ``resources`` directory, which the packaging copies into
        every install, so it ships everywhere. Blank lines and lines starting
        with ``#`` are ignored. Returns an empty list when the file is missing or
        unreadable, in which case no tips are shown.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = (
            os.path.join(here, 'resources', 'tips.md'),
        )
        for path in candidates:
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    raw_lines = fh.readlines()
            except OSError:
                continue
            tips = []
            for line in raw_lines:
                text = line.strip()
                if not text or text.startswith('#'):
                    continue
                tips.append(text)
            return tips
        return []

    def _maybe_show_tips_banner(self):
        """Show a usage tip in the banner area, if the user hasn't opted out.

        Called once the update banner's area is free — either there was no
        update available, or the user dismissed the update banner. The tip is
        revealed after a short delay so it eases in gracefully rather than
        snapping into place the instant the window settles or the update banner
        disappears. ``show_terminal_tip`` itself suppresses the tip while the
        update banner is still revealed, so the two never stack.
        """
        try:
            if not getattr(self, 'tips_revealer', None):
                return
            if not bool(self.config.get_setting('terminal.show_tips', True)):
                return
            # Cancel any pending reveal so repeated triggers don't stack.
            if getattr(self, '_tips_banner_timeout_id', 0):
                GLib.source_remove(self._tips_banner_timeout_id)
            self._tips_banner_timeout_id = GLib.timeout_add_seconds(
                TIPS_BANNER_DELAY_SECONDS, self._reveal_delayed_tips
            )
        except Exception as exc:
            logger.debug("Failed to schedule tips banner: %s", exc)

    def _reveal_delayed_tips(self):
        """Reveal a tip once the grace delay has elapsed (one-shot timeout)."""
        self._tips_banner_timeout_id = 0
        try:
            if not getattr(self, 'tips_revealer', None):
                return False
            # Re-check the opt-out in case the user disabled tips during the wait.
            if not bool(self.config.get_setting('terminal.show_tips', True)):
                return False
            tips = self._build_window_tips()
            if tips:
                self.show_terminal_tip(tips)
        except Exception as exc:
            logger.debug("Failed to show tips banner: %s", exc)
        return False  # one-shot

    def show_terminal_tip(self, tips):
        """Show a terminal usage tip in the window banner area.

        ``tips`` is the list of tip strings (a single string is also accepted).
        A random tip is shown first; the "Next tip" button cycles through the
        rest. The update banner takes priority: if it is currently shown, the
        tip is suppressed so the two never stack.
        """
        if not getattr(self, 'tips_revealer', None):
            return
        if getattr(self, 'update_banner', None) is not None and self.update_banner.get_revealed():
            return
        if isinstance(tips, str):
            tips = [tips]
        tips = [t for t in (tips or []) if t]
        if not tips:
            return
        self._terminal_tips = tips
        self._terminal_tip_index = random.randrange(len(tips))
        self._display_current_terminal_tip()

    def _display_current_terminal_tip(self):
        """Render the current tip and toggle the Next button to match the list."""
        try:
            tip = self._terminal_tips[self._terminal_tip_index]
            self.tips_label.set_label(f"\N{ELECTRIC LIGHT BULB} {tip}")
            # Make sure the container is visible before revealing so the
            # slide-in animation actually runs.
            if getattr(self, 'tips_banner_container', None) is not None:
                self.tips_banner_container.set_visible(True)
            self.tips_revealer.set_reveal_child(True)
            # The Next button is only useful when there's more than one tip.
            if getattr(self, 'tips_next_button', None) is not None:
                self.tips_next_button.set_visible(len(self._terminal_tips) > 1)
        except Exception as exc:
            logger.debug("Failed to show terminal tip: %s", exc)

    def _on_tips_banner_next(self, *args):
        """Advance to the next tip, wrapping around the list."""
        tips = getattr(self, '_terminal_tips', None)
        if not tips:
            return
        self._terminal_tip_index = (getattr(self, '_terminal_tip_index', 0) + 1) % len(tips)
        self._display_current_terminal_tip()

    def _hide_tips_banner(self):
        """Hide the terminal tips banner (used on dismiss and update priority).

        Only toggle the revealer's reveal-child so the slide-out transition
        actually plays; the revealer collapses to zero height on its own once the
        animation finishes. (Setting the container invisible here would skip the
        animation — the container stays visible; only fullscreen toggles it.)
        """
        try:
            if getattr(self, 'tips_revealer', None) is not None:
                self.tips_revealer.set_reveal_child(False)
        except Exception:
            pass

    def _on_tips_banner_dismiss(self, *args):
        """Hide the tips banner for this session only."""
        self._hide_tips_banner()

    def _on_tips_banner_dont_show_again(self, *args):
        """Hide the tips banner and never show tips again."""
        self._hide_tips_banner()
        try:
            if getattr(self, 'config', None) is not None:
                self.config.set_setting('terminal.show_tips', False)
        except Exception as exc:
            logger.error("Failed to update show terminal tips preference: %s", exc)

    def _apply_update_banner_css(self):
        """Apply CSS styling to update banner"""
        try:
            from gi.repository import Gdk
            
            display = Gdk.Display.get_default()
            if not display:
                logger.warning("No display available for banner CSS installation")
                return
            
            # Check if CSS is already installed
            if getattr(display, '_update_banner_css_installed', False):
                return
            
            provider = Gtk.CssProvider()
            css = """
            /* Blue download button */
            banner button {
                background-image: none;
                background-color: #3b82f6;
                color: white;
                border: none;
                font-weight: bold;
                min-height: 32px;
                padding: 0 16px;
                border-radius: 6px;
            }
            
            banner button:hover {
                background-color: #2563eb;
            }
            
            banner button:active {
                background-color: #1d4ed8;
            }
            """
            provider.load_from_data(css.encode('utf-8'))
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            setattr(display, '_update_banner_css_installed', True)
            logger.debug("Update banner CSS installed successfully")
        except Exception as e:
            logger.error(f"Failed to install update banner CSS: {e}")


def _on_view_logs_action_factory(window):
    """Build the ``win.view-logs`` activation handler.

    Creates a fresh ``LogViewerWindow`` parented to *window* on each call so
    closing & reopening the viewer works as expected. We import lazily so
    ``actions`` doesn't pay the cost on every app start.
    """

    def _activate(_action, _param):
        try:
            from .log_viewer import LogViewerWindow
        except Exception as exc:
            logger.error("Could not load log viewer: %s", exc)
            return
        try:
            viewer = LogViewerWindow(parent=window)
            viewer.present()
        except Exception as exc:
            logger.error("Could not open log viewer: %s", exc, exc_info=True)

    return _activate


def register_window_actions(window):
    """Register SimpleActions with the provided main window."""
    # Context menu action to force opening a new connection tab
    window.open_new_connection_action = Gio.SimpleAction.new('open-new-connection', None)
    window.open_new_connection_action.connect('activate', window.on_open_new_connection_action)
    window.add_action(window.open_new_connection_action)


    # Global action for opening new connection tab (Ctrl/⌘+Alt+N)
    window.open_new_connection_tab_action = Gio.SimpleAction.new('open-new-connection-tab', None)
    window.open_new_connection_tab_action.connect('activate', window.on_open_new_connection_tab_action)
    window.add_action(window.open_new_connection_tab_action)

    # Action for managing files on remote server (skip on macOS and Flatpak)
    if not should_hide_file_manager_options():
        window.manage_files_action = Gio.SimpleAction.new('manage-files', None)
        window.manage_files_action.connect('activate', window.on_manage_files_action)
        window.add_action(window.manage_files_action)

    if hasattr(window, 'on_duplicate_connection_action'):
        window.duplicate_connection_action = Gio.SimpleAction.new('duplicate-connection', None)
        window.duplicate_connection_action.connect('activate', window.on_duplicate_connection_action)
        window.add_action(window.duplicate_connection_action)

    window.open_in_split_view_action = Gio.SimpleAction.new('open-in-split-view', None)
    window.open_in_split_view_action.connect('activate', window.on_open_in_split_view_action)
    window.add_action(window.open_in_split_view_action)

    # Action for editing connections via context menu
    window.edit_connection_action = Gio.SimpleAction.new('edit-connection', None)
    window.edit_connection_action.connect('activate', window.on_edit_connection_action)
    window.add_action(window.edit_connection_action)

    # Action for deleting connections via context menu
    window.delete_connection_action = Gio.SimpleAction.new('delete-connection', None)
    window.delete_connection_action.connect('activate', window.on_delete_connection_action)
    window.add_action(window.delete_connection_action)

    # Action for opening connections in the system terminal when external
    # terminal support is available and not hidden via preferences.
    if not should_hide_external_terminal_options():
        window.open_in_system_terminal_action = Gio.SimpleAction.new('open-in-system-terminal', None)
        window.open_in_system_terminal_action.connect('activate', window.on_open_in_system_terminal_action)
        window.add_action(window.open_in_system_terminal_action)

    window.sort_connections_action = Gio.SimpleAction.new('sort-connections', GLib.VariantType.new('s'))
    window.sort_connections_action.connect('activate', window.on_sort_connections_action)
    window.add_action(window.sort_connections_action)

    # Action for broadcasting commands to all SSH terminals
    window.broadcast_command_action = Gio.SimpleAction.new('broadcast-command', None)
    window.broadcast_command_action.connect('activate', window.on_broadcast_command_action)
    window.add_action(window.broadcast_command_action)

    # Action for editing known hosts
    if hasattr(window, 'on_edit_known_hosts_action'):
        window.edit_known_hosts_action = Gio.SimpleAction.new('edit-known-hosts', None)
        window.edit_known_hosts_action.connect('activate', window.on_edit_known_hosts_action)
        window.add_action(window.edit_known_hosts_action)

    # Action for managing the local authorized_keys file
    if hasattr(window, 'on_manage_local_authorized_keys_action'):
        window.manage_local_authorized_keys_action = Gio.SimpleAction.new('manage-local-authorized-keys', None)
        window.manage_local_authorized_keys_action.connect('activate', window.on_manage_local_authorized_keys_action)
        window.add_action(window.manage_local_authorized_keys_action)

    # Group management actions
    window.create_group_action = Gio.SimpleAction.new('create-group', None)
    window.create_group_action.connect('activate', window.on_create_group_action)
    window.add_action(window.create_group_action)

    window.edit_group_action = Gio.SimpleAction.new('edit-group', None)
    window.edit_group_action.connect('activate', window.on_edit_group_action)
    window.add_action(window.edit_group_action)

    window.delete_group_action = Gio.SimpleAction.new('delete-group', None)
    window.delete_group_action.connect('activate', window.on_delete_group_action)
    window.add_action(window.delete_group_action)

    # Add move to ungrouped action
    window.move_to_ungrouped_action = Gio.SimpleAction.new('move-to-ungrouped', None)
    window.move_to_ungrouped_action.connect('activate', window.on_move_to_ungrouped_action)
    window.add_action(window.move_to_ungrouped_action)

    # Add move to group action
    window.move_to_group_action = Gio.SimpleAction.new('move-to-group', None)
    window.move_to_group_action.connect('activate', window.on_move_to_group_action)
    window.add_action(window.move_to_group_action)

    # Add copy to group action (keeps existing memberships)
    if hasattr(window, 'on_copy_to_group_action'):
        window.copy_to_group_action = Gio.SimpleAction.new('copy-to-group', None)
        window.copy_to_group_action.connect('activate', window.on_copy_to_group_action)
        window.add_action(window.copy_to_group_action)

    # Sidebar toggle action and accelerators
    try:
        sidebar_action = Gio.SimpleAction.new('toggle_sidebar', None)
        sidebar_action.connect('activate', window.on_toggle_sidebar_action)
        window.add_action(sidebar_action)
        app = window.get_application()
        if app:
            window._update_sidebar_accelerators()
    except Exception as e:
        logger.error(f"Failed to register sidebar toggle action: {e}")

    # Import/Export configuration actions
    if hasattr(window, 'on_export_config_action'):
        window.export_config_action = Gio.SimpleAction.new('export-config', None)
        window.export_config_action.connect('activate', window.on_export_config_action)
        window.add_action(window.export_config_action)

    if hasattr(window, 'on_import_config_action'):
        window.import_config_action = Gio.SimpleAction.new('import-config', None)
        window.import_config_action.connect('activate', window.on_import_config_action)
        window.add_action(window.import_config_action)

    # Session save/open actions
    if hasattr(window, 'on_save_session_action'):
        window.save_session_action = Gio.SimpleAction.new('save-session', None)
        window.save_session_action.connect('activate', window.on_save_session_action)
        window.add_action(window.save_session_action)

    if hasattr(window, 'on_open_session_action'):
        window.open_session_action = Gio.SimpleAction.new('open-session', None)
        window.open_session_action.connect('activate', window.on_open_session_action)
        window.add_action(window.open_session_action)

    if hasattr(window, 'on_manage_sessions_action'):
        window.manage_sessions_action = Gio.SimpleAction.new('manage-sessions', None)
        window.manage_sessions_action.connect('activate', window.on_manage_sessions_action)
        window.add_action(window.manage_sessions_action)
    
    # Check for updates action
    if hasattr(window, 'on_check_for_updates_action'):
        window.check_for_updates_action = Gio.SimpleAction.new('check-for-updates', None)
        window.check_for_updates_action.connect('activate', window.on_check_for_updates_action)
        window.add_action(window.check_for_updates_action)

    # View Logs action — opens the log viewer dialog for bug-report sharing.
    window.view_logs_action = Gio.SimpleAction.new('view-logs', None)
    window.view_logs_action.connect('activate', _on_view_logs_action_factory(window))
    window.add_action(window.view_logs_action)

    # Report a Problem — copies a diagnostic bundle (incl. crash report) to the
    # clipboard and opens the GitHub new-issue page.
    window.report_problem_action = Gio.SimpleAction.new('report-problem', None)
    window.report_problem_action.connect('activate', window.on_report_problem_action)
    window.add_action(window.report_problem_action)

    # Export Diagnostics — save a ZIP of logs + system info + redacted config.
    window.export_diagnostics_action = Gio.SimpleAction.new('export-diagnostics', None)
    window.export_diagnostics_action.connect('activate', window.on_export_diagnostics_action)
    window.add_action(window.export_diagnostics_action)

    # Application theme (header bar menu)
    if hasattr(window, '_apply_app_theme'):
        theme_action = Gio.SimpleAction.new('set-app-theme', GLib.VariantType.new('s'))
        theme_action.connect(
            'activate',
            lambda _action, param: window._apply_app_theme(
                param.get_string() if param else 'default'
            ),
        )
        window.add_action(theme_action)

    # Command blocks panel toggle
    if hasattr(window, '_toggle_command_blocks_panel'):
        cb_action = Gio.SimpleAction.new('toggle-command-blocks', None)
        cb_action.connect('activate', lambda a, p: window._toggle_command_blocks_panel())
        window.add_action(cb_action)
        app = window.get_application()
        if app:
            app.set_accels_for_action('win.toggle-command-blocks', ['<primary><alt>s'])
            if hasattr(app, '_action_order') and 'toggle-command-blocks' not in app._action_order:
                app._action_order.append('toggle-command-blocks')
                app._default_shortcuts['toggle-command-blocks'] = ['<primary><alt>s']
