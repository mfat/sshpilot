"""Tab / pane lifecycle for MainWindow.

Extracted verbatim from window.py as a mixin (matching WindowActions and the
other Window*Mixin modules) to shrink the window.py god-object. MainWindow
inherits this; methods keep their signatures and `self.` state access, so this
is a pure code move with no behavior change.

Covers tab close/rename/title, the tab context menus and their handlers
(_on_tabmenu_*), file-manager embed teardown (shared with shutdown), tab
attach/detach, drag-to-convert-to-split, and the layout toggle.

SplitViewTab is imported locally inside the methods that use it (a module-level
import here would be circular: split_view -> window -> window_tabs), so those
local imports are kept as-is.
"""

import logging

from gi.repository import Gtk, Adw, Gio, GLib, Gdk, GObject
from gettext import gettext as _

from sshpilot import icon_utils
from .terminal import TerminalWidget
from .plugins.api import Capability
from .plugins.registry import capabilities_for
from .connection_display import get_connection_alias, get_connection_host
from .file_manager_integration import launch_remote_file_manager
from .file_manager_integration import (
    should_hide_external_terminal_options,
    should_hide_file_manager_options,
)

logger = logging.getLogger(__name__)

# Match window.py's private aliases so the moved methods read unchanged.
_get_connection_host = get_connection_host
_get_connection_alias = get_connection_alias


class WindowTabsMixin:
    """Tab/pane lifecycle, context menus, teardown, and drag-to-split."""

    def _on_tab_close_confirmed(self, dialog, response_id, tab_view, page):
        """Handle response from tab close confirmation dialog"""
        dialog.destroy()
        if response_id == 'close':
            self._close_tab(tab_view, page)
        # If cancelled, do nothing - the tab remains open
    
    def _close_tab(self, tab_view, page):
        """Close the tab and clean up resources"""
        if hasattr(page, 'get_child'):
            child = page.get_child()
            if hasattr(child, 'disconnect'):
                # Get the connection associated with this terminal using reverse map
                connection = self.terminal_to_connection.get(child)
                # Disconnect the terminal
                child.disconnect()
                # Clean up multi-tab tracking maps
                try:
                    if connection is not None:
                        # Remove from list for this connection
                        if connection in self.connection_to_terminals and child in self.connection_to_terminals[connection]:
                            self.connection_to_terminals[connection].remove(child)
                            if not self.connection_to_terminals[connection]:
                                del self.connection_to_terminals[connection]
                        # Update most-recent mapping
                        if connection in self.active_terminals and self.active_terminals[connection] is child:
                            remaining = self.connection_to_terminals.get(connection)
                            if remaining:
                                self.active_terminals[connection] = remaining[-1]
                            else:
                                del self.active_terminals[connection]
                    if child in self.terminal_to_connection:
                        del self.terminal_to_connection[child]
                except Exception:
                    pass
        
        # Close the tab page
        tab_view.close_page(page)
        
        # Update the UI based on the number of remaining tabs
        GLib.idle_add(self._update_ui_after_tab_close)
    
    def _on_tab_bar_pressed(self, gesture, n_press, x, y):
        if n_press != 2:
            return
        page = self.tab_view.get_selected_page()
        if page and not self._is_start_tab_page(page):
            self._show_tab_rename_popover(page, x, y)

    def _show_tab_rename_popover(self, page, x, y):
        entry = Gtk.Entry()
        entry.set_text(page.get_title())
        entry.set_width_chars(24)

        popover = Gtk.Popover()
        popover.set_child(entry)
        popover.set_parent(self.tab_bar)
        popover.set_has_arrow(False)

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover._committed = False

        def commit(_entry):
            if not popover._committed:
                popover._committed = True
                self._apply_tab_title(page, entry.get_text().strip())
            popover.popdown()

        def on_closed(p):
            if not p._committed:
                self._apply_tab_title(page, entry.get_text().strip())

        entry.connect('activate', commit)
        popover.connect('closed', on_closed)
        popover.popup()
        GLib.idle_add(lambda: (entry.grab_focus(), entry.select_region(0, -1), False)[-1])

    def _apply_tab_title(self, page, title):
        if title:
            page.set_title(title)
            page.custom_tab_title = title
        else:
            page.custom_tab_title = None
            terminal = page.get_child()
            if hasattr(terminal, 'connection'):
                page.set_title(terminal.connection.nickname)

    # ── tab context menu (right-click) ─────────────────────────────────────────

    # All tab-menu action names, in display order within their sections.
    _TAB_MENU_ACTIONS = (
        'tabmenu-duplicate', 'tabmenu-rename',
        'tabmenu-reconnect', 'tabmenu-manage-files',
        'tabmenu-open-system-terminal', 'tabmenu-new-local',
        'tabmenu-fm-new-window',
        'tabmenu-layout-horizontal', 'tabmenu-layout-vertical',
        'tabmenu-layout-default', 'tabmenu-layout-compact',
        'tabmenu-close', 'tabmenu-close-others', 'tabmenu-close-right',
    )

    def _build_tab_context_menus(self) -> None:
        """Build the single tab context menu model and register its actions.

        AdwTabBox builds the popover once from this model and caches it, so the
        same model serves every tab type. Per-tab differences are produced by
        enabling only the relevant actions in _on_tab_setup_menu; every item
        carries hidden-when="action-disabled" so disabled items vanish rather
        than grey out. Each win.<name> action operates on the right-clicked
        page captured in setup-menu (self._tab_menu_page).
        """
        handlers = {
            'tabmenu-duplicate': self._on_tabmenu_duplicate,
            'tabmenu-rename': self._on_tabmenu_rename,
            'tabmenu-reconnect': self._on_tabmenu_reconnect,
            'tabmenu-manage-files': self._on_tabmenu_manage_files,
            'tabmenu-open-system-terminal': self._on_tabmenu_open_system_terminal,
            'tabmenu-new-local': self._on_tabmenu_new_local,
            'tabmenu-fm-new-window': self._on_tabmenu_fm_new_window,
            'tabmenu-layout-horizontal': self._on_tabmenu_layout_horizontal,
            'tabmenu-layout-vertical': self._on_tabmenu_layout_vertical,
            'tabmenu-layout-default': self._on_tabmenu_layout_default,
            'tabmenu-layout-compact': self._on_tabmenu_layout_compact,
            'tabmenu-close': self._on_tabmenu_close,
            'tabmenu-close-others': self._on_tabmenu_close_others,
            'tabmenu-close-right': self._on_tabmenu_close_right,
        }
        for name, handler in handlers.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', handler)
            self.add_action(action)

        def _item(label, action_name):
            item = Gio.MenuItem.new(label, 'win.' + action_name)
            # Hide the item entirely when its action is disabled, so each tab
            # type shows only its applicable items.
            item.set_attribute_value(
                'hidden-when', GLib.Variant('s', 'action-disabled')
            )
            return item

        menu = Gio.Menu()

        sec1 = Gio.Menu()
        sec1.append_item(_item(_('Duplicate'), 'tabmenu-duplicate'))
        sec1.append_item(_item(_('Rename Tab…'), 'tabmenu-rename'))
        menu.append_section(None, sec1)

        sec2 = Gio.Menu()
        sec2.append_item(_item(_('Reconnect'), 'tabmenu-reconnect'))
        sec2.append_item(_item(_('Manage Files'), 'tabmenu-manage-files'))
        sec2.append_item(_item(_('Open in System Terminal'), 'tabmenu-open-system-terminal'))
        sec2.append_item(_item(_('New Local Tab'), 'tabmenu-new-local'))
        sec2.append_item(_item(_('Open in New Window'), 'tabmenu-fm-new-window'))
        menu.append_section(None, sec2)

        sec3 = Gio.Menu()
        sec3.append_item(_item(_('Side by Side'), 'tabmenu-layout-horizontal'))
        sec3.append_item(_item(_('Top / Bottom'), 'tabmenu-layout-vertical'))
        sec3.append_item(_item(_('Default layout'), 'tabmenu-layout-default'))
        sec3.append_item(_item(_('Compact layout'), 'tabmenu-layout-compact'))
        menu.append_section(None, sec3)

        sec4 = Gio.Menu()
        sec4.append_item(_item(_('Close'), 'tabmenu-close'))
        sec4.append_item(_item(_('Close Other Tabs'), 'tabmenu-close-others'))
        sec4.append_item(_item(_('Close Tabs to the Right'), 'tabmenu-close-right'))
        menu.append_section(None, sec4)

        self._tab_menu_model = menu

    def _on_tab_bar_secondary_press(self, gesture, n_press, x, y):
        """Expose or hide the tab menu model before AdwTabBox builds the menu.

        Runs in the capture phase, ahead of AdwTabBox's own right-click handler.
        The pinned Start tab gets a NULL model (no menu); every other tab gets
        the full model. Pinned AdwTab widgets carry the "pinned" CSS class.
        """
        # Remember where the menu was opened so "Rename Tab…" can anchor its
        # popover at the right-clicked tab (tab_bar coordinate space).
        self._tab_menu_xy = (x, y)
        try:
            widget = self.tab_bar.pick(x, y, Gtk.PickFlags.DEFAULT)
            on_pinned = False
            while widget is not None and widget is not self.tab_bar:
                if widget.has_css_class('pinned'):
                    on_pinned = True
                    break
                widget = widget.get_parent()
            self.tab_view.set_menu_model(
                None if on_pinned else self._tab_menu_model
            )
        except Exception:
            logger.debug("Tab menu guard failed", exc_info=True)
            self.tab_view.set_menu_model(self._tab_menu_model)

    def _on_tab_setup_menu(self, tab_view, page):
        """Enable the actions applicable to the right-clicked tab type.

        page is None when the menu closes; keep _tab_menu_page so the
        just-activated action still targets the right tab.
        """
        if page is None:
            return
        self._tab_menu_page = page

        if self._is_start_tab_page(page):
            enabled = set()  # belt-and-braces; the capture guard already hid it
        else:
            enabled = self._enabled_tab_actions(page.get_child())

        for name in self._TAB_MENU_ACTIONS:
            action = self.lookup_action(name)
            if action is not None:
                action.set_enabled(name in enabled)

    def _file_manager_embed_for_child(self, child):
        """Return the FileManagerTabEmbed in a tab's child subtree, or None.

        File-manager tabs wrap the embed inside a placeholder GtkBox, so a
        direct isinstance() check on the page child is insufficient.
        """
        from .file_manager_integration import FileManagerTabEmbed
        if child is None:
            return None
        if isinstance(child, FileManagerTabEmbed):
            return child
        w = child.get_first_child() if hasattr(child, 'get_first_child') else None
        while w is not None:
            found = self._file_manager_embed_for_child(w)
            if found is not None:
                return found
            w = w.get_next_sibling()
        return None

    def _teardown_file_manager_embed(self, embed) -> None:
        """Destroy an embedded file manager synchronously, outside GC.

        The embed carries Python 'destroy' signal handlers (its own _on_destroy
        plus the tracking lambda from _track_internal_file_manager_window) that
        form reference cycles. If the embed is finalized by the garbage
        collector, PyGObject invokes those Python handlers mid-collection, which
        segfaults. Disconnecting the handlers and disposing the controller here
        (at tab close / shutdown, outside GC) avoids that.
        """
        if embed is None:
            return
        # Idempotency: once torn down (_controller is None) there is nothing to
        # disconnect or dispose, so a second call (e.g. both on_tab_detached and
        # _teardown_all_file_manager_tabs firing) is a safe no-op.
        if getattr(embed, '_controller', None) is None:
            return
        try:
            destroy_id = GObject.signal_lookup('destroy', Gtk.Widget.__gtype__)
            GObject.signal_handlers_disconnect_matched(
                embed, GObject.SignalMatchType.ID, destroy_id, 0, None, None, None)
        except Exception:
            logger.debug('Could not disconnect FM embed destroy handlers', exc_info=True)
        self._teardown_embed_controller(embed, self._internal_file_manager_windows)

    @staticmethod
    def _teardown_embed_controller(embed, registry) -> bool:
        """Dispose an embed's file-manager controller. Pure/duck-typed: no GTK.

        Order matters and is asserted by tests: remove the controller from
        ``registry`` first, then run ``_cleanup_manager()``, then ``destroy()``,
        then null ``embed._controller`` so the embed reads as torn down.
        Idempotent — returns False (a no-op) when there is no live controller.
        """
        if embed is None or getattr(embed, '_controller', None) is None:
            return False
        controller = embed._controller
        try:
            if controller in registry:
                registry.remove(controller)
        except Exception:
            pass
        try:
            if hasattr(controller, '_cleanup_manager'):
                controller._cleanup_manager()
        except Exception:
            logger.debug('FM controller cleanup failed', exc_info=True)
        try:
            controller.destroy()
        except Exception:
            logger.debug('FM controller destroy failed', exc_info=True)
        try:
            embed._controller = None
        except Exception:
            pass
        return True

    def _teardown_all_file_manager_tabs(self) -> None:
        """Tear down every open file-manager tab synchronously (app shutdown)."""
        try:
            pages = [self.tab_view.get_nth_page(i)
                     for i in range(self.tab_view.get_n_pages())]
        except Exception:
            return
        for page in pages:
            try:
                embed = self._file_manager_embed_for_child(page.get_child())
            except Exception:
                embed = None
            if embed is not None:
                self._teardown_file_manager_embed(embed)
        # Lifecycle assertion: a tab whose embed still holds a live controller
        # after this pass is a leak that would later be finalized during GC and
        # segfault. Surface it in the log instead of failing silently.
        try:
            for page in pages:
                embed = self._file_manager_embed_for_child(page.get_child())
                if embed is not None and getattr(embed, '_controller', None) is not None:
                    logger.warning(
                        'File-manager embed survived shutdown teardown with a live '
                        'controller (%r) — potential GC-finalization crash source', embed)
        except Exception:
            logger.debug('FM teardown lifecycle check failed', exc_info=True)

    def _enabled_tab_actions(self, child) -> set:
        """Return the set of tab-menu action names to enable for child.

        Applies the same capability/preference gating as the sidebar menu for
        Manage Files, Open in System Terminal and (file manager) New Window.
        """
        from .split_view import SplitViewTab

        common = {'tabmenu-rename', 'tabmenu-close',
                  'tabmenu-close-others', 'tabmenu-close-right'}

        if isinstance(child, SplitViewTab):
            return common | {
                'tabmenu-layout-horizontal', 'tabmenu-layout-vertical',
                'tabmenu-layout-default', 'tabmenu-layout-compact',
            }

        if self._file_manager_embed_for_child(child) is not None:
            enabled = set(common)
            if not should_hide_file_manager_options():
                enabled.add('tabmenu-fm-new-window')
            return enabled

        if isinstance(child, TerminalWidget):
            try:
                is_local = child._is_local_terminal()
            except Exception:
                is_local = False
            enabled = common | {'tabmenu-duplicate'}
            if is_local:
                enabled.add('tabmenu-new-local')
                return enabled
            enabled.add('tabmenu-reconnect')
            conn = self.terminal_to_connection.get(child)
            caps = capabilities_for(conn) if conn else frozenset()
            if Capability.FILE_TRANSFER in caps and not should_hide_file_manager_options():
                enabled.add('tabmenu-manage-files')
            if getattr(conn, 'protocol', 'ssh') == 'ssh' and not should_hide_external_terminal_options():
                enabled.add('tabmenu-open-system-terminal')
            return enabled

        return set()

    # ── tab context menu action handlers ───────────────────────────────────────

    def _tab_menu_target(self):
        """Return (page, child) for the current tab menu target, or (None, None)."""
        page = getattr(self, '_tab_menu_page', None)
        if page is None:
            return None, None
        try:
            child = page.get_child()
        except Exception:
            child = None
        return page, child

    def _on_tabmenu_duplicate(self, action, param=None):
        try:
            page, child = self._tab_menu_target()
            if not isinstance(child, TerminalWidget):
                return
            if child._is_local_terminal():
                self.terminal_manager.show_local_terminal()
                return
            conn = self.terminal_to_connection.get(child)
            if conn is not None:
                self.terminal_manager.connect_to_host(conn, force_new=True)
        except Exception as exc:
            logger.error("Tab duplicate failed: %s", exc)

    def _on_tabmenu_rename(self, action, param=None):
        try:
            page, _child = self._tab_menu_target()
            if page is not None:
                self._rename_tab_page(page)
        except Exception as exc:
            logger.error("Tab rename failed: %s", exc)

    def _rename_tab_page(self, page) -> None:
        """Select the page and open the inline rename popover anchored to the tab bar."""
        try:
            self.tab_view.set_selected_page(page)
        except Exception:
            pass
        # Anchor at the right-clicked position (captured when the menu opened);
        # fall back near the start of the tab bar for keyboard-triggered menus.
        xy = getattr(self, '_tab_menu_xy', None)
        if xy is not None:
            x, y = xy
        else:
            x = max(0, min(40, self.tab_bar.get_width() // 2))
            y = self.tab_bar.get_height()
        self._show_tab_rename_popover(page, x, y)

    def _on_tabmenu_reconnect(self, action, param=None):
        try:
            page, child = self._tab_menu_target()
            if isinstance(child, TerminalWidget) and hasattr(child, '_on_reconnect_clicked'):
                child._on_reconnect_clicked()
        except Exception as exc:
            logger.error("Tab reconnect failed: %s", exc)

    def _on_tabmenu_manage_files(self, action, param=None):
        try:
            page, child = self._tab_menu_target()
            if not isinstance(child, TerminalWidget):
                return
            conn = self.terminal_to_connection.get(child)
            if conn is not None:
                self._open_manage_files_for_connection(conn)
        except Exception as exc:
            logger.error("Tab manage files failed: %s", exc)

    def _on_tabmenu_open_system_terminal(self, action, param=None):
        try:
            page, child = self._tab_menu_target()
            if not isinstance(child, TerminalWidget):
                return
            conn = self.terminal_to_connection.get(child)
            if conn is not None:
                self.open_in_system_terminal(conn)
        except Exception as exc:
            logger.error("Tab open in system terminal failed: %s", exc)

    def _on_tabmenu_new_local(self, action, param=None):
        try:
            self.terminal_manager.show_local_terminal()
        except Exception as exc:
            logger.error("Tab new local terminal failed: %s", exc)

    def _on_tabmenu_close(self, action, param=None):
        try:
            page, _child = self._tab_menu_target()
            if page is not None:
                self.tab_view.close_page(page)
        except Exception as exc:
            logger.error("Tab close failed: %s", exc)

    def _on_tabmenu_close_others(self, action, param=None):
        try:
            page, _child = self._tab_menu_target()
            if page is not None:
                self._confirm_then_bulk_close(
                    page,
                    lambda: self.tab_view.close_other_pages(page),
                    after_only=False,
                )
        except Exception as exc:
            logger.error("Tab close others failed: %s", exc)

    def _on_tabmenu_close_right(self, action, param=None):
        try:
            page, _child = self._tab_menu_target()
            if page is not None:
                self._confirm_then_bulk_close(
                    page,
                    lambda: self.tab_view.close_pages_after(page),
                    after_only=True,
                )
        except Exception as exc:
            logger.error("Tab close to the right failed: %s", exc)

    def _apply_split_layout_to_target(self, mode):
        page, child = self._tab_menu_target()
        from .split_view import SplitViewTab
        if isinstance(child, SplitViewTab):
            child.set_layout_mode(mode)

    def _on_tabmenu_layout_horizontal(self, action, param=None):
        try:
            self._apply_split_layout_to_target('horizontal')
        except Exception as exc:
            logger.error("Tab layout horizontal failed: %s", exc)

    def _on_tabmenu_layout_vertical(self, action, param=None):
        try:
            self._apply_split_layout_to_target('vertical')
        except Exception as exc:
            logger.error("Tab layout vertical failed: %s", exc)

    def _on_tabmenu_layout_default(self, action, param=None):
        try:
            page, child = self._tab_menu_target()
            from .split_view import SplitViewTab
            if isinstance(child, SplitViewTab):
                child.fill_panes_to_viewport()
        except Exception as exc:
            logger.error("Tab layout default failed: %s", exc)

    def _on_tabmenu_layout_compact(self, action, param=None):
        try:
            page, child = self._tab_menu_target()
            from .split_view import SplitViewTab
            if isinstance(child, SplitViewTab):
                child.reset_all_row_heights(0.3)
        except Exception as exc:
            logger.error("Tab layout compact failed: %s", exc)

    def _on_tabmenu_fm_new_window(self, action, param=None):
        try:
            page, child = self._tab_menu_target()
            embed = self._file_manager_embed_for_child(child)
            if embed is None:
                return
            controller = getattr(embed, '_controller', None)
            conn = getattr(controller, '_connection', None) if controller else None
            if conn is None:
                logger.debug("File manager tab has no connection for new window")
                return
            self._launch_external_file_manager(conn)
        except Exception as exc:
            logger.error("Tab file manager new window failed: %s", exc)

    def _launch_external_file_manager(self, connection) -> None:
        """Open a standalone (external) file manager window for connection.

        Same path used by the file_manager.open_externally preference.
        """
        nickname = (
            getattr(connection, 'nickname', None)
            or getattr(connection, 'hostname', None)
            or getattr(connection, 'host', None)
            or getattr(connection, 'username', 'Remote Host')
        )
        host_value = _get_connection_host(connection) or _get_connection_alias(connection)
        username = getattr(connection, 'username', '') or ''
        port_value = getattr(connection, 'port', 22)
        effective_port = port_value if port_value and port_value != 22 else None

        ssh_config = None
        if hasattr(self, 'config') and self.config is not None:
            try:
                ssh_config = self.config.get_ssh_config()
            except Exception as exc:
                logger.debug("Failed to read SSH configuration for file manager: %s", exc)
                ssh_config = None

        def error_callback(error_msg):
            message = error_msg or "Failed to open file manager"
            logger.error(f"Failed to open file manager for {nickname}: {message}")
            self._show_manage_files_error(str(nickname), message)

        success, error_msg, window = launch_remote_file_manager(
            user=str(username or ''),
            host=str(host_value or ''),
            port=effective_port,
            nickname=str(nickname),
            parent_window=self,
            error_callback=error_callback,
            connection=connection,
            connection_manager=self.connection_manager,
            ssh_config=ssh_config,
        )

        if success:
            logger.info(f"Started external file manager for {nickname}")
            if window is not None:
                self._track_internal_file_manager_window(window)
        else:
            message = error_msg or "Failed to start file manager process"
            logger.error(f"Failed to start file manager process for {nickname}: {message}")
            self._show_manage_files_error(str(nickname), message)

    def _bulk_close_target_pages(self, target_page, after_only: bool):
        """Pages that close_other_pages / close_pages_after would remove.

        get_pages() yields pages in tab order, so "after target" is every page
        seen once the target has been passed. The target itself and the pinned
        Start tab are excluded, matching libadwaita's semantics.
        """
        pages, seen_target = [], False
        for p in list(self.tab_view.get_pages()):
            if p is target_page:
                seen_target = True
                continue
            if self._is_start_tab_page(p):
                continue
            if after_only and not seen_target:
                continue
            pages.append(p)
        return pages

    def _count_sessions_in_pages(self, pages) -> int:
        """Number of live terminal sessions across the given pages."""
        from .split_view import SplitViewTab
        total = 0
        for p in pages:
            child = p.get_child() if hasattr(p, 'get_child') else None
            if isinstance(child, SplitViewTab):
                total += sum(pane.get_terminal_count() for pane in child._panes)
            elif child in self.terminal_to_connection:
                total += 1
        return total

    def _run_suppressed_close(self, close_fn):
        """Run a bulk close with the per-tab disconnect confirmation suppressed.

        The bulk close emits close-page once per page; suppressing keeps
        on_tab_close from spawning a modal dialog for each (issue #1014). The
        closes run synchronously, so the flag is safely reset afterwards.
        Sessions still tear down via TerminalWidget._on_destroy.
        """
        self._suppress_close_confirmation = True
        try:
            close_fn()
        finally:
            self._suppress_close_confirmation = False

    def _add_disconnect_opt_out(self, dialog):
        """Add the shared opt-out control to a disconnect confirmation."""
        checkbox = Gtk.CheckButton(label=_("Don't ask me again"))
        checkbox.set_halign(Gtk.Align.START)
        checkbox.set_margin_top(6)
        dialog.set_extra_child(checkbox)
        return checkbox

    def _persist_disconnect_opt_out(self, checkbox):
        """Disable future disconnect confirmations when explicitly requested."""
        if checkbox.get_active():
            self.config.set_setting('confirm-disconnect', False)

    def _on_bulk_close_response(self, dialog, response_id, close_fn, checkbox):
        """Handle the single confirmation dialog for a bulk tab close."""
        if response_id == 'close':
            self._persist_disconnect_opt_out(checkbox)
            self._run_suppressed_close(close_fn)
        dialog.destroy()

    def _confirm_then_bulk_close(self, target_page, close_fn, after_only: bool):
        """Close other / to-the-right tabs, honoring confirm-disconnect.

        When the preference is on and live sessions would be disconnected, ask
        once for the whole batch rather than once per tab. On confirm the batch
        closes with per-tab confirmation suppressed; on cancel nothing happens.
        """
        pages = self._bulk_close_target_pages(target_page, after_only)
        if not pages:
            return

        confirm = bool(
            getattr(self, 'config', None)
            and self.config.get_setting('confirm-disconnect', True)
        )
        n_sessions = self._count_sessions_in_pages(pages)

        if confirm and n_sessions > 0:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Close tabs?"),
                body=_("This will close {t} tab(s) and disconnect {n} session(s). Continue?").format(
                    t=len(pages), n=n_sessions
                ),
            )
            dialog.add_response('cancel', _("Cancel"))
            dialog.add_response('close', _("Close"))
            dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close')
            dialog.set_close_response('cancel')
            checkbox = self._add_disconnect_opt_out(dialog)
            dialog.connect(
                'response',
                self._on_bulk_close_response,
                close_fn,
                checkbox,
            )
            dialog.present()
        else:
            # Toggle off, or nothing with a live session to disconnect: close
            # directly. With the toggle off, on_tab_close disconnects each tab
            # without a dialog.
            close_fn()

    def on_tab_close(self, tab_view, page):
        """Handle tab close - THE KEY FIX: Never call close_page ourselves"""
        if self._is_start_tab_page(page):
            return True

        # If we are closing pages programmatically (e.g., after deleting a
        # connection), suppress the confirmation dialog and allow the default
        # close behavior to proceed.
        if getattr(self, '_suppress_close_confirmation', False):
            return False

        # SplitViewTab: clean up all embedded terminals, then allow immediate close.
        # When confirm-disconnect is enabled and there are active terminals, ask first.
        if hasattr(page, 'get_child'):
            child = page.get_child()
            from .split_view import SplitViewTab
            if isinstance(child, SplitViewTab):
                n_terminals = sum(p.get_terminal_count() for p in child._panes)
                confirm_disconnect = getattr(self, 'config', None) and self.config.get_setting('confirm-disconnect', True)
                if confirm_disconnect and n_terminals > 0:
                    self._pending_close_split_tab_view = tab_view
                    self._pending_close_split_page = page
                    self._pending_close_split_child = child
                    dialog = Adw.MessageDialog(
                        transient_for=self,
                        modal=True,
                        heading=_("Close split view?"),
                        body=_("This will disconnect {n} terminal session(s). Continue?").format(n=n_terminals),
                    )
                    dialog.add_response('cancel', _("Cancel"))
                    dialog.add_response('close', _("Close"))
                    dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
                    dialog.set_default_response('close')
                    dialog.set_close_response('cancel')
                    checkbox = self._add_disconnect_opt_out(dialog)
                    dialog.connect(
                        'response',
                        self._on_split_tab_close_response,
                        checkbox,
                    )
                    dialog.present()
                    return True  # Prevent immediate close; dialog handles it
                child.cleanup_all()
                return False

        # Get the connection for this tab
        connection = None
        terminal = None
        if hasattr(page, 'get_child'):
            child = page.get_child()
            if hasattr(child, 'disconnect'):
                terminal = child
                connection = self.terminal_to_connection.get(child)
        
        if not connection:
            # Non-terminal tabs (plugins, file manager, …) close immediately.
            return False
        
        # Check if confirmation is required
        confirm_disconnect = self.config.get_setting('confirm-disconnect', True)
        
        if confirm_disconnect:
            # Store tab view and page as instance variables
            self._pending_close_tab_view = tab_view
            self._pending_close_page = page
            self._pending_close_connection = connection
            self._pending_close_terminal = terminal
            
            # Show confirmation dialog
            host_value = _get_connection_host(connection) or _get_connection_alias(connection)
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Close connection to {}").format(connection.nickname or host_value),
                body=_("Are you sure you want to close this connection?")
            )
            dialog.add_response('cancel', _("Cancel"))
            dialog.add_response('close', _("Close"))
            dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close')
            dialog.set_close_response('cancel')
            checkbox = self._add_disconnect_opt_out(dialog)
            
            # Connect to response signal before showing the dialog
            dialog.connect('response', self._on_tab_close_response, checkbox)
            dialog.present()
            
            # Prevent the default close behavior while we show confirmation
            return True
        else:
            # Explicitly disconnect so the SSH process (including any port
            # forwarding) is killed immediately rather than relying on the
            # widget destroy signal, which can be deferred indefinitely.
            if terminal and hasattr(terminal, 'disconnect'):
                terminal.disconnect()
            return False

    def _on_tab_close_response(self, dialog, response_id, checkbox):
        """Handle the response from the close confirmation dialog."""
        # Retrieve the pending tab info
        tab_view = self._pending_close_tab_view
        page = self._pending_close_page
        terminal = self._pending_close_terminal

        if response_id == 'close':
            self._persist_disconnect_opt_out(checkbox)
            # User confirmed, disconnect the terminal. The tab will be removed
            # by the AdwTabView once we finish the close operation.
            if terminal and hasattr(terminal, 'disconnect'):
                terminal.disconnect()
            # Now, tell the tab view to finish closing the page.
            tab_view.close_page_finish(page, True)
            
            # Update tab button visibility after closing
            self._update_tab_button_visibility()
            
            # Check if this was the last user tab
            if not self.has_user_tabs():
                self.show_start_tab()
        else:
            # User cancelled, so we reject the close request.
            # This is the critical step that makes the close button work again.
            tab_view.close_page_finish(page, False)

        dialog.destroy()
        # Clear pending state to avoid memory leaks
        self._pending_close_tab_view = None
        self._pending_close_page = None
        self._pending_close_connection = None
        self._pending_close_terminal = None

    def _on_split_tab_close_response(self, dialog, response_id, checkbox):
        """Handle the confirmation dialog for closing an entire SplitViewTab."""
        tab_view = getattr(self, '_pending_close_split_tab_view', None)
        page = getattr(self, '_pending_close_split_page', None)
        child = getattr(self, '_pending_close_split_child', None)
        if response_id == 'close':
            self._persist_disconnect_opt_out(checkbox)
            if child is not None:
                child.cleanup_all()
            if tab_view is not None and page is not None:
                tab_view.close_page_finish(page, True)
            self._update_tab_button_visibility()
            if tab_view is not None and not self.has_user_tabs():
                self.show_start_tab()
        else:
            if tab_view is not None and page is not None:
                tab_view.close_page_finish(page, False)
        dialog.destroy()
        self._pending_close_split_tab_view = None
        self._pending_close_split_page = None
        self._pending_close_split_child = None

    def on_tab_attached(self, tab_view, page, position):
        """Handle tab attached"""
        self._update_tab_button_visibility()
        # Register a drop target on TerminalWidget pages so dragging a
        # connection onto a terminal converts that tab into a split-view tab.
        try:
            from .terminal import TerminalWidget
            child = page.get_child() if page else None
            if isinstance(child, TerminalWidget):
                self._register_convert_to_split_drop(child, page)
        except Exception as exc:
            logger.debug("Could not register convert-to-split drop: %s", exc)
        self._update_layout_toggle_state()

        # Sidebar behavior: a session (SSH or local) just opened. Hide after a
        # short delay so the terminal settles, then the sidebar slides away.
        try:
            if self.config.get_setting('ui.sidebar_hide_on_terminal_open', False):
                self._cancel_pending_sidebar_hide()
                self._sidebar_hide_timer_id = GLib.timeout_add(
                    350, self._hide_sidebar_after_terminal
                )
        except Exception:
            logger.debug("sidebar hide-on-terminal-open failed", exc_info=True)

    def _register_convert_to_split_drop(self, terminal, page) -> None:
        """Attach a drop target to terminal so dragging a connection converts the tab to split view."""
        from gi.repository import Gtk, Gdk, GObject
        dt = Gtk.DropTarget.new(type=GObject.TYPE_PYOBJECT, actions=Gdk.DragAction.MOVE)

        def _on_drop(_target, value, _x, _y):
            try:
                if hasattr(value, 'get_value'):
                    value = value.get_value()
                if not isinstance(value, dict):
                    return False

                drag_type = value.get("type")

                if drag_type == "command_block":
                    cmd_dict = {
                        'id': value.get('command_id'),
                        'name': value.get('name', ''),
                        'command': value.get('command', ''),
                        'has_placeholders': value.get('has_placeholders', False),
                    }
                    panel = getattr(self, 'command_blocks_panel', None)
                    if panel is not None:
                        panel._send_command_to_terminal(cmd_dict)
                    else:
                        data = (cmd_dict['command'] + '\n').encode('utf-8')
                        try:
                            if hasattr(terminal, 'backend') and terminal.backend:
                                terminal.backend.feed_child(data)
                            elif hasattr(terminal, 'vte') and terminal.vte:
                                terminal.vte.feed_child(data)
                        except Exception:
                            pass
                    return True

                if drag_type not in ("connection", "group"):
                    return False

                # Only convert if the terminal is still in the main tab_view
                source_page = self._page_for_child(terminal)
                if source_page is None:
                    return False

                tab_title = _("Split View")

                if drag_type == "connection":
                    nicknames = value.get("connection_nicknames") or []
                    if not nicknames and value.get("connection_nickname"):
                        nicknames = [value["connection_nickname"]]
                    if not nicknames:
                        return False
                    connections = []
                    for nick in nicknames:
                        conn = self.connection_manager.find_connection_by_nickname(nick)
                        if conn is not None:
                            connections.append(conn)
                else:  # group
                    group_id = value.get("group_id")
                    group_info = self.group_manager.groups.get(group_id) if group_id else None
                    if not group_info:
                        return False
                    group_name = group_info.get("name", "")
                    if group_name:
                        tab_title = _("Split View — {name}").format(name=group_name)
                    connections = []
                    for nick in group_info.get("connections", []):
                        conn = self.connection_manager.find_connection_by_nickname(nick)
                        if conn is not None:
                            connections.append(conn)

                if not connections:
                    return False

                from .split_view import SplitViewTab

                svt = SplitViewTab(self)

                # Move the existing terminal out of the main tab_view into pane 0
                title = source_page.get_title() or _("Terminal")
                self._suppress_close_confirmation = True
                self._moving_tab_to_pane = True
                try:
                    self.tab_view.close_page(source_page)
                finally:
                    self._suppress_close_confirmation = False
                    self._moving_tab_to_pane = False
                # Defer reparent so tab_view fully completes its close sequence.
                # Pane 1 is filled synchronously below, so the proportional
                # paned settles before pane 0 gets its (deferred) terminal — that
                # leaves the divider at the empty placeholder's natural size, not
                # 50/50. Re-equalize once the terminal is embedded so the panes
                # match every other split scenario.
                def _embed_terminal_in_pane0():
                    svt._panes[0].add_terminal(terminal, title)
                    # _on_default_clicked resets horizontal splits to 50/50 and
                    # applies the default sizing for the pane count (fill for ≤2,
                    # 50% rows for more) — same as a freshly-opened split.
                    GLib.idle_add(lambda: (svt._on_default_clicked(), False)[1])
                    return False
                GLib.idle_add(_embed_terminal_in_pane0)

                # Add each dropped connection to pane 1 (and extra panes beyond)
                for i, conn in enumerate(connections):
                    if i == 0:
                        svt._panes[1].add_connection(conn)
                    else:
                        svt.add_pane().add_connection(conn)

                # Append the split-view tab to the main tab_view
                new_page = self.tab_view.append(svt)
                new_page.set_title(tab_title)
                try:
                    new_page.set_icon(
                        icon_utils.new_gicon_from_icon_name('view-dual-symbolic')
                    )
                except Exception:
                    pass
                svt._tab_page = new_page
                self.show_tab_view()
                self.tab_view.set_selected_page(new_page)
                return True
            except Exception as exc:
                logger.error("Convert-to-split drop failed: %s", exc)
                return False

        dt.connect("drop", _on_drop)
        dt.connect("enter", lambda _t, _x, _y: Gdk.DragAction.MOVE)
        terminal.add_controller(dt)
        # Remember it so SplitPane.add_terminal can detach it when this terminal
        # is embedded into a pane (otherwise it keeps intercepting connection
        # drops over the terminal area, so only the tab bar would accept them).
        terminal._convert_to_split_dt = dt

    # ── layout toggle state / apply ───────────────────────────────────────────

    def _update_layout_toggle_state(self) -> None:
        """Sync tab-bar H/V toggles with the selected tab."""
        if not hasattr(self, '_layout_h_btn'):
            return
        try:
            page = self.tab_view.get_selected_page()
            child = page.get_child() if page else None
            from .split_view import SplitViewTab
            is_terminal_tab = isinstance(child, (TerminalWidget, SplitViewTab))
            self._layout_h_btn.set_visible(is_terminal_tab)
            self._layout_v_btn.set_visible(is_terminal_tab)
            self._layout_toggle_updating[0] = True
            try:
                if isinstance(child, SplitViewTab):
                    mode = child.get_layout_mode()
                    self._layout_h_btn.set_active(mode == 'horizontal')
                    self._layout_v_btn.set_active(mode == 'vertical')
                else:
                    self._layout_h_btn.set_active(False)
                    self._layout_v_btn.set_active(False)
            finally:
                self._layout_toggle_updating[0] = False
        except Exception as exc:
            logger.debug("Failed to update layout toggle state: %s", exc)

    def _apply_tab_layout_mode(self, mode: str) -> None:
        """Apply H or V layout to the current tab (converts regular tab if needed)."""
        try:
            page = self.tab_view.get_selected_page()
            if page is None:
                return
            child = page.get_child()
            from .split_view import SplitViewTab
            if isinstance(child, SplitViewTab):
                child.set_layout_mode(mode)
            elif isinstance(child, TerminalWidget):
                self._convert_terminal_tab_to_split(page, child, mode)
        except Exception as exc:
            logger.error("Failed to apply tab layout mode: %s", exc)

    def _convert_terminal_tab_to_split(self, source_page, terminal, mode: str) -> None:
        """Convert a regular terminal tab into a split-view tab."""
        from .split_view import SplitViewTab

        svt = SplitViewTab(self)
        svt.set_layout_mode(mode)

        title = source_page.get_title() or _("Terminal")
        self._suppress_close_confirmation = True
        self._moving_tab_to_pane = True
        try:
            self.tab_view.close_page(source_page)
        finally:
            self._suppress_close_confirmation = False
            self._moving_tab_to_pane = False

        # Defer reparent so tab_view fully completes its close sequence first
        GLib.idle_add(svt._panes[0].add_terminal, terminal, title)

        new_page = self.tab_view.append(svt)
        new_page.set_title(_("Split View"))
        try:
            new_page.set_icon(
                icon_utils.new_gicon_from_icon_name('view-dual-symbolic')
            )
        except Exception:
            pass
        svt._tab_page = new_page
        self.show_tab_view()
        self.tab_view.set_selected_page(new_page)

    # ── tab button visibility ─────────────────────────────────────────────────

    def _update_tab_button_visibility(self):
        """Hide tab bar and overview button when only the Start tab is open."""
        try:
            show_tabs = self.has_user_tabs()
            if hasattr(self, 'tab_button'):
                self.tab_button.set_visible(show_tabs)
            if hasattr(self, 'tab_bar'):
                self.tab_bar.set_visible(show_tabs)
        except Exception as e:
            logger.error(f"Failed to update tab button visibility: {e}")

    def on_tab_detached(self, tab_view, page, position):
        """Handle tab detached"""
        # Skip dict cleanup when a terminal is being moved into a split pane
        # (the terminal stays live; its tracking entries remain valid).
        if getattr(self, '_moving_tab_to_pane', False):
            self._update_tab_button_visibility()
            if tab_view.get_n_pages() <= 1:
                self.show_start_tab()
            return

        # Tear down an embedded file manager synchronously (outside GC) so its
        # Python 'destroy' handlers never run during a garbage collection,
        # which segfaults. Covers ×, Close, Close Other Tabs, Close to Right.
        try:
            embed = (
                self._file_manager_embed_for_child(page.get_child())
                if page is not None and hasattr(page, 'get_child')
                else None
            )
            if embed is not None:
                self._teardown_file_manager_embed(embed)
        except Exception:
            logger.debug('File manager tab teardown on detach failed', exc_info=True)

        # Cleanup terminal-to-connection maps when a page is detached
        detached_connection = None
        try:
            if hasattr(page, 'get_child'):
                child = page.get_child()
                if child in self.terminal_to_connection:
                    connection = self.terminal_to_connection.get(child)
                    detached_connection = connection
                    # Remove reverse map
                    del self.terminal_to_connection[child]
                    # Remove from per-connection list
                    if connection in self.connection_to_terminals and child in self.connection_to_terminals[connection]:
                        self.connection_to_terminals[connection].remove(child)
                        if not self.connection_to_terminals[connection]:
                            del self.connection_to_terminals[connection]
                    # Update most recent mapping if needed
                    if connection in self.active_terminals and self.active_terminals[connection] is child:
                        remaining = self.connection_to_terminals.get(connection)
                        if remaining:
                            self.active_terminals[connection] = remaining[-1]
                        else:
                            del self.active_terminals[connection]
        except Exception:
            pass

        # Update tab button visibility
        self._update_tab_button_visibility()
        self._update_layout_toggle_state()

        # Select Start when the last user tab closes
        if not self.has_user_tabs():
            self.show_start_tab()

        # Recompute the affected connection's state now that the terminal has
        # been removed from the maps. With no terminals left this resolves to
        # UNKNOWN, hiding the sidebar status icon instead of leaving a stale red
        # "Disconnected" indicator after an intentional close.
        if detached_connection is not None:
            try:
                self._recompute_connection_state(detached_connection)
            except Exception:
                pass

    def on_open_split_view_clicked(self, button):
        """Open a new empty split-view tab."""
        try:
            from .split_view import SplitViewTab
            svt = SplitViewTab(self)
            page = self.tab_view.append(svt)
            page.set_title(_("Split View"))
            page.set_icon(icon_utils.new_gicon_from_icon_name('view-dual-symbolic'))
            svt._tab_page = page
            self.show_tab_view()
            self.tab_view.set_selected_page(page)
        except Exception as exc:
            logger.error("Failed to open split view: %s", exc)

    def on_local_terminal_button_clicked(self, button):
        """Handle local terminal button click"""
        try:
            logger.info("Local terminal button clicked")
            self.terminal_manager.show_local_terminal()
        except Exception as e:
            logger.error(f"Failed to open local terminal: {e}")

    def on_tab_button_clicked(self, button):
        """Toggle the tab overview."""
        try:
            is_open = self.tab_overview.get_open()
            self.tab_overview.set_open(not is_open)
        except Exception as e:
            logger.error(f"Failed to toggle tab overview: {e}")
