"""Action handlers for MainWindow and registration helper."""

import logging
from typing import Optional
from gi.repository import Gio, Gtk, Adw, GLib, Gdk
from gettext import gettext as _

from .preferences import (
    should_hide_external_terminal_options,
    should_hide_file_manager_options,
)
from .shortcut_utils import get_primary_modifier_label
from .platform_utils import is_macos

HAS_NAV_SPLIT = hasattr(Adw, 'NavigationSplitView')
HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')

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
                if HAS_NAV_SPLIT or HAS_OVERLAY_SPLIT:
                    current_visible = self.split_view.get_show_sidebar()
                else:
                    sidebar_widget = self.split_view.get_start_child()
                    current_visible = sidebar_widget.get_visible() if sidebar_widget else True

                # Toggle to opposite state
                new_visible = not current_visible

                # Update sidebar visibility
                self._toggle_sidebar_visibility(new_visible)

                # Update button state if it exists (inverted logic: active = should hide)
                if hasattr(self, 'sidebar_toggle_button'):
                    self.sidebar_toggle_button.set_active(not new_visible)
        except Exception as e:
            logger.error(f"Failed to toggle sidebar via action: {e}")

    def on_open_new_connection_action(self, action, param=None):
        """Open a new tab for the selected connection via context menu."""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            if hasattr(self, '_return_to_tab_view_if_welcome'):
                self._return_to_tab_view_if_welcome()
            self.terminal_manager.connect_to_host(connection, force_new=True)
        except Exception as e:
            logger.error(f"Failed to open new connection tab: {e}")

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
        """Open a new tab for the selected connection via global shortcut (Ctrl/⌘+Alt+N)."""
        try:
            # Get the currently selected connection
            row = self.connection_list.get_selected_row()
            if row and hasattr(row, 'connection'):
                connection = row.connection
                if hasattr(self, '_return_to_tab_view_if_welcome'):
                    self._return_to_tab_view_if_welcome()
                self.terminal_manager.connect_to_host(connection, force_new=True)
            else:
                # If no connection is selected, show a message or fall back to new connection dialog
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

    def on_manage_files_action(self, action, param=None):
        """Handle manage files action from context menu"""
        if hasattr(self, '_context_menu_connection') and self._context_menu_connection:
            connection = self._context_menu_connection
            try:
                self._open_manage_files_for_connection(connection)
            except Exception as e:
                logger.error(f"Error opening file manager: {e}")
                self._show_manage_files_error(connection.nickname, str(e))

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

    def on_broadcast_command_action(self, action, param=None):
        """Handle broadcast command action - shows dialog to input command"""
        try:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Broadcast Command"),
                body=_("Enter a command to send to all open SSH terminals:"),
            )

            entry = Gtk.Entry()
            entry.set_placeholder_text(_("e.g., ls -la"))
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            dialog.set_extra_child(entry)

            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('send', _('Send'))
            dialog.set_response_appearance('send', Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response('send')
            dialog.set_close_response('cancel')

            def on_response(dialog, response):
                if response == 'send':
                    command = entry.get_text().strip()
                    if command:
                        sent_count, failed_count = self.terminal_manager.broadcast_command(command)

                        result_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Command Sent"),
                            body=_("Command sent to {} SSH terminals. {} failed.").format(sent_count, failed_count),
                        )
                        result_dialog.add_response('ok', _('OK'))
                        result_dialog.present()
                    else:
                        error_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Error"),
                            body=_("Please enter a command to send."),
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
            logger.error(f"Failed to show broadcast command dialog: {e}")
            try:
                error_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Error"),
                    body=_("Failed to open broadcast command dialog: {}").format(str(e)),
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()
            except Exception:
                pass

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

                icon = Gtk.Image.new_from_icon_name('folder-symbolic')
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
