"""Action handlers for MainWindow and registration helper."""

import logging
import os
import random
from gi.repository import Gio, Gtk, Adw, GLib, Gdk
from gettext import gettext as _

from .accessibility import set_accessible_name
from .dialog_focus import mark_default_response_visible
from .file_manager_integration import (
    should_hide_external_terminal_options,
    should_hide_file_manager_options,
)
from .shortcut_utils import get_primary_modifier_label
from .platform_utils import is_macos
from .shortcut_utils import TOGGLE_FULLSCREEN_ACTION
from .i18n import N_, ui_language_codes as _ui_language_codes
from . import wol

HAS_NAV_SPLIT = hasattr(Adw, 'NavigationSplitView')
HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')

# Grace delay before the usage-tips banner eases into the update banner's area.
TIPS_BANNER_DELAY_SECONDS = 4

# Split view opens one pane per connection, each starting its own session. Up
# to this many is routine and opens straight away; a bigger batch — typically a
# large group opened by accident — is confirmed first (GH #1232).
SPLIT_VIEW_CONFIRM_THRESHOLD = 5

logger = logging.getLogger(__name__)


HEADERBAR_VISIBILITY_ACTIONS = (
    ('headerbar-sidebar-toggle', N_('Sidebar Toggle Button'), 'ui.headerbar_show_sidebar_toggle', False),
    ('headerbar-split-view', N_('Split View Button'), 'ui.headerbar_show_split_view', False),
    ('headerbar-commands', N_('Command Snippets Button'), 'ui.headerbar_show_commands', True),
    ('headerbar-terminal-theme', N_('Terminal Theme Button'), 'ui.headerbar_show_terminal_theme', True),
    ('headerbar-theme-menu', N_('Theme Menu'), 'ui.headerbar_show_theme_toggle', False),
    ('headerbar-local-terminal', N_('Local Terminal Button'), 'ui.headerbar_show_local_terminal', True),
)


def _register_headerbar_visibility_actions(window):
    """Expose the Preferences header-bar switches as stateful menu actions."""
    window._headerbar_visibility_actions = {}
    if not hasattr(Gio.SimpleAction, 'new_stateful'):
        return

    config = getattr(window, 'config', None)

    for action_name, _label, setting_key, default in HEADERBAR_VISIBILITY_ACTIONS:
        enabled = bool(config.get_setting(setting_key, default)) if config else default
        action = Gio.SimpleAction.new_stateful(
            action_name,
            None,
            GLib.Variant.new_boolean(enabled),
        )

        def _on_change_state(current_action, value, *, key=setting_key):
            visible = value.get_boolean()
            if config:
                config.set_setting(key, visible)
            current_action.set_state(GLib.Variant.new_boolean(visible))
            if hasattr(window, 'update_headerbar_buttons'):
                window.update_headerbar_buttons()

        action.connect('change-state', _on_change_state)
        window.add_action(action)
        window._headerbar_visibility_actions[setting_key] = action


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
        """Open a tab for each selected connection (the 'open-new-connection-tab'
        action; disabled by default on Linux/Windows, assignable in Preferences)."""
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

    def _create_split_view_tab(self, connections=None, title=None):
        """Append a split-view tab and fill it with ``connections``."""
        from .split_view import SplitViewTab
        from sshpilot import icon_utils

        svt = SplitViewTab(self)
        page = self.tab_view.append(svt)
        page.set_title(title or _("Split View"))
        page.set_icon(icon_utils.new_gicon_from_icon_name('view-dual-symbolic'))
        svt._tab_page = page
        if connections:
            svt.populate(connections)
        self.show_tab_view()
        self.tab_view.set_selected_page(page)
        return svt

    def _open_connection_batch_now(self, connections, title, prefer):
        if prefer == 'tabs':
            self._open_new_connection_tabs(connections)
        else:
            self._create_split_view_tab(connections, title)

    def _on_connection_batch_response(self, dialog, response_id, connections, title):
        try:
            if response_id == 'split':
                self._open_connection_batch_now(connections, title, 'split')
            elif response_id in ('tabs', 'continue'):
                self._open_connection_batch_now(connections, title, 'tabs')
        except Exception as exc:
            logger.error("Failed to open connections: %s", exc)
        dialog.close()

    def _open_connection_batch(self, connections, title=None, prefer='split'):
        """Open ``connections``, confirming a large batch first.

        ``prefer`` is what the caller asked for: 'tabs' (a tab each) just
        confirms, while 'split' (one split-view tab) also offers separate tabs
        as the gentler way out. Opening a whole group is a single click away,
        so a group with dozens of hosts could start that many sessions with no
        warning (GH #1232). Small batches stay friction-free; past the
        threshold we ask.
        """
        if not connections:
            return
        if len(connections) <= SPLIT_VIEW_CONFIRM_THRESHOLD:
            self._open_connection_batch_now(connections, title, prefer)
            return

        if prefer == 'tabs':
            body = _("This will open {n} new connection tabs.")
        else:
            body = _("This will start {n} connections in a single tab.")
        dialog = Adw.AlertDialog(
            heading=_("Open connections?"),
            body=body.format(n=len(connections)),
        )
        dialog.add_response('cancel', _("Cancel"))
        if prefer == 'tabs':
            default = 'continue'
            dialog.add_response('continue', _("Continue"))
        else:
            default = 'split'
            dialog.add_response('split', _("Split View"))
            dialog.add_response('tabs', _("Separate Tabs"))
        dialog.set_response_appearance(default, Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response(default)
        dialog.set_close_response('cancel')
        dialog.connect(
            'response',
            self._on_connection_batch_response,
            connections,
            title,
        )
        dialog.present(self)
        mark_default_response_visible(self)

    def _connections_for_context_group(self):
        """Connections of the group row the menu or gesture last targeted."""
        group_row = getattr(self, '_context_menu_group_row', None)
        if group_row is None:
            return []
        group_info = getattr(group_row, 'group_info', None) or {}
        connections = []
        for nick in group_info.get('connections', []):
            conn = self.connection_manager.find_connection_by_nickname(nick)
            if conn is not None:
                connections.append(conn)
        return connections

    def on_new_split_view_tab(self, action, param=None):
        """Open a new empty split-view tab."""
        try:
            self._create_split_view_tab()
        except Exception as exc:
            logger.error("Failed to open new split view tab: %s", exc)

    def on_open_in_split_view_action(self, action, param=None):
        """Open the selected connection(s) in a new split-view tab."""
        try:
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

            self._open_connection_batch(connections)
        except Exception as exc:
            logger.error("Failed to open connections in split view: %s", exc)

    def on_open_group_in_split_view_action(self, action, param=None):
        """Open all connections in a group as a new split-view tab."""
        try:
            connections = self._connections_for_context_group()
            group_row = getattr(self, '_context_menu_group_row', None)
            name = (getattr(group_row, 'group_info', None) or {}).get('name', '')
            title = _("Split View — {name}").format(name=name) if name else None
            self._open_connection_batch(connections, title)
        except Exception as exc:
            logger.error("Failed to open group in split view: %s", exc)

    def on_open_group_in_tabs_action(self, action, param=None):
        """Open every connection in a group as its own tab."""
        try:
            self._open_connection_batch(
                self._connections_for_context_group(), prefer='tabs')
        except Exception as exc:
            logger.error("Failed to open group in tabs: %s", exc)

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

            if getattr(self, 'key_manager', None) is None:
                self._show_key_service_unavailable()
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
                    body=_("Could not open the Copy Key window.\n\n{error}").format(error=str(e))
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()
            except Exception:
                pass

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
                    meta = self.connection_manager.get_metadata(nickname)
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

    def on_delete_group_action(self, action, param=None):
        """Handle delete group action."""
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

            all_connections = self.connection_manager.get_connections()
            connections_dict = {conn.nickname: conn for conn in all_connections}

            actual_connections = [
                c
                for c in group_info.get('connections', [])
                if c in connections_dict
            ]
            connection_count = len(actual_connections)

            controller = getattr(self.group_manager, 'controller', None)
            if controller is None:
                self._simple_dialog(
                    _("Service unavailable"),
                    _("Connect to the sshPilot daemon before deleting groups."),
                )
                return

            def _run_delete(sequence):
                """Run a controller sequence for the delete operation."""
                controller.run_sequence(
                    sequence,
                    on_success=lambda _r: self.rebuild_connection_list(),
                    on_error=lambda e: self._simple_dialog(
                        _("Error"),
                        _("Failed to delete group: {error}").format(
                            error=str(e),
                        ),
                    ),
                )

            if connection_count > 0:
                dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Delete Group"),
                    body=_(
                        "The group '{name}' contains {count} connection(s).\n\n"
                        "What would you like to do with the connections?"
                    ).format(name=group_info['name'], count=connection_count),
                )

                dialog.add_response('cancel', _('Cancel'))
                dialog.add_response('move', _('Move to Parent/Ungrouped'))
                dialog.add_response('delete_all', _('Delete All Connections'))
                dialog.set_response_appearance(
                    'delete_all', Adw.ResponseAppearance.DESTRUCTIVE,
                )
                dialog.set_default_response('move')

                def on_response_with_connections(_dialog, response):
                    if response == 'move':
                        _run_delete([
                            lambda _prev: controller.client.delete_group(group_id),
                        ])
                    elif response == 'delete_all':
                        # Delete each connection, then delete the group.
                        from sshpilot.api.models.connections import (
                            ConnectionId, DeleteConnectionRequest,
                        )
                        steps = []
                        for nickname in actual_connections:
                            steps.append(
                                lambda _prev, nick=nickname: (
                                    controller.client.delete_connection(
                                        DeleteConnectionRequest(
                                            connection_id=ConnectionId(nick),
                                        )
                                    )
                                )
                            )
                        steps.append(
                            lambda _prev: controller.client.delete_group(group_id),
                        )
                        _run_delete(steps)
                    _dialog.destroy()

                dialog.connect('response', on_response_with_connections)
                dialog.present()
            else:
                dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Delete Group"),
                    body=_("Are you sure you want to delete the empty group '{name}'?").format(
                        name=group_info['name'],
                    ),
                )

                dialog.add_response('cancel', _('Cancel'))
                dialog.add_response('delete', _('Delete'))
                dialog.set_response_appearance(
                    'delete', Adw.ResponseAppearance.DESTRUCTIVE,
                )
                dialog.set_default_response('cancel')

                def on_response_empty_group(_dialog, response):
                    if response == 'delete':
                        _run_delete([
                            lambda _prev: controller.client.delete_group(group_id),
                        ])
                    _dialog.destroy()

                dialog.connect('response', on_response_empty_group)
                dialog.present()

        except Exception as e:
            logger.error(f"Failed to show delete group dialog: {e}")

    def on_move_group_to_root_action(self, action, param=None):
        """Move a nested subgroup out to the top level (ungroup it from its parent)."""
        try:
            selected_row = getattr(self, '_context_menu_group_row', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
            if not selected_row or not hasattr(selected_row, 'group_id'):
                return

            group_id = selected_row.group_id
            group_info = self.group_manager.groups.get(group_id)
            if not group_info or not group_info.get('parent_id'):
                return

            controller = getattr(self.group_manager, 'controller', None)
            if controller is None:
                self._simple_dialog(
                    _("Service unavailable"),
                    _("Connect to the sshPilot daemon before moving groups."),
                )
                return

            from .sidebar import _submit_group_dnd_place, _sidebar_projection_generation

            index = len(self.group_manager.get_ordered_siblings(None))
            _submit_group_dnd_place(
                self,
                group_id,
                None,
                index,
                expected_generation=_sidebar_projection_generation(self),
            )
        except Exception as e:
            logger.error(f"Failed to move group to top level: {e}")

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
        set_accessible_name(entry, _("Session name"))
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
                    body=_('A session named "{name}" already exists. Overwrite it?').format(name=name),
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
            body=_('Replace the current tabs with session "{name}", or add it to the current tabs?').format(name=name),
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
            body=_('Enter a new name for "{name}".').format(name=name),
        )
        entry = Gtk.Entry()
        entry.set_text(name)
        set_accessible_name(entry, _("Session name"))
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
            body=_('The session "{name}" will be permanently deleted.').format(name=name),
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
                toast = Adw.Toast.new(_("You're running the latest version"))
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
        every install, so it ships everywhere. Language-specific
        ``tips.<lang>.md`` files win when present (translated as data, not
        gettext). ``{primary}`` becomes Ctrl/Strg/⌘ for the platform, and
        ``[file-manager]`` / ``[external-terminal]`` tips are dropped when those
        features are hidden. Returns an empty list when no tip file is readable.
        """
        from .tips import load_window_tips

        here = os.path.dirname(os.path.abspath(__file__))
        return load_window_tips(
            os.path.join(here, 'resources'),
            _ui_language_codes(),
            include_file_manager=not should_hide_file_manager_options(),
            include_external_terminal=not should_hide_external_terminal_options(),
        )

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
            self.tips_label.set_label(_("\N{ELECTRIC LIGHT BULB} {tip}").format(tip=tip))
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


def register_fullscreen_action(window):
    """Add ``win.toggle-fullscreen``, the one target every fullscreen
    accelerator and the header-bar button resolve to.

    Its accelerators come from the shortcut registry (see
    ``register_window_shortcut`` in main.py), so there is no second,
    hard-coded key path that could fire for the same event.
    """
    try:
        action = Gio.SimpleAction.new(TOGGLE_FULLSCREEN_ACTION, None)
        action.connect('activate', lambda *_args: window.toggle_fullscreen())
        window.add_action(action)
        window.toggle_fullscreen_action = action
    except Exception as e:
        logger.error(f"Failed to register fullscreen toggle action: {e}")


def register_window_actions(window):
    """Register SimpleActions with the provided main window."""
    _register_headerbar_visibility_actions(window)

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

        # Main-menu variant: uses the selected connection, or opens the file
        # manager with a host picker in the remote pane when none is selected.
        window.open_file_manager_action = Gio.SimpleAction.new('open-file-manager', None)
        window.open_file_manager_action.connect('activate', window.open_file_manager_from_menu)
        window.add_action(window.open_file_manager_action)

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

    register_fullscreen_action(window)

    # Main-menu command matching the title-bar split-view button.
    if hasattr(window, 'on_open_split_view_clicked'):
        window.new_split_view_action = Gio.SimpleAction.new('new-split-view', None)
        window.new_split_view_action.connect(
            'activate',
            lambda *_args: window.on_open_split_view_clicked(None),
        )
        window.add_action(window.new_split_view_action)

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
        saved_theme = str(window.config.get_setting('app-theme', 'default'))
        if saved_theme not in {'default', 'light', 'dark'}:
            saved_theme = 'default'
        theme_action = Gio.SimpleAction.new_stateful(
            'set-app-theme',
            GLib.VariantType.new('s'),
            GLib.Variant('s', saved_theme),
        )
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
        if app and hasattr(app, 'register_window_shortcut'):
            # Disabled by default (Ctrl+Alt+S clashed with AltGr layouts / WM
            # shortcuts). Stays listed/assignable in the editor and respects a
            # user override via the normal apply path.
            app.register_window_shortcut('toggle-command-blocks', [])
        elif app:
            if hasattr(app, '_action_order') and 'toggle-command-blocks' not in app._action_order:
                app._action_order.append('toggle-command-blocks')
                app._default_shortcuts['toggle-command-blocks'] = []
