"""Manage-files (file manager) flow for MainWindow.

Extracted verbatim from window.py as a mixin (matching WindowActions and the
other Window*Mixin modules) to shrink the window.py god-object. MainWindow
inherits this; methods keep their signatures and `self.` state access, so this
is a pure code move with no behavior change.

Covers the daemon-backed manage-files entry action, the placeholder tab while
the backend spawns, and file-manager tab registration. The synchronous
teardown of file-manager embeds stays in window.py for now (it is shared with
tab close/shutdown).
"""

import logging
from typing import Optional

from gi.repository import GLib, Gtk, Pango
from gettext import gettext as _

from .plugins.api import Capability
from .plugins.registry import capabilities_for
from .connection_display import get_connection_alias, get_connection_host
from .file_manager_integration import (
    create_internal_file_manager_tab,
    has_internal_file_manager,
    should_hide_file_manager_options,
)

logger = logging.getLogger(__name__)

# Match window.py's private aliases so the moved methods read unchanged.
_get_connection_host = get_connection_host
_get_connection_alias = get_connection_alias


class WindowFileManagerMixin:
    """Open/embed the SFTP file manager and manage its placeholder tabs."""

    def on_manage_files_action(self, action, param=None):
        """Handle manage files action from context menu"""
        if hasattr(self, '_context_menu_connection') and self._context_menu_connection:
            connection = self._context_menu_connection
            try:
                self._open_manage_files_for_connection(connection)
            except Exception as e:
                logger.error(f"Error opening file manager: {e}")

    def open_file_manager_from_menu(self, action=None, param=None):
        """Main-menu entry: open files for the selected connection, or — with
        no selection — open the file manager with a host picker in the remote
        pane (fallback: a picker popover, then the normal open flow)."""
        if should_hide_file_manager_options():
            return
        try:
            row = self.connection_list.get_selected_row()
            connection = getattr(row, 'connection', None) if row else None
        except Exception:
            connection = None
        if connection is not None:
            self._open_manage_files_for_connection(connection)
            return

        use_internal = has_internal_file_manager()
        if use_internal:
            self._open_file_manager_with_picker()
        else:
            from .host_picker import show_host_picker
            show_host_picker(self, self.menu_button,
                             self._open_manage_files_for_connection)

    def _open_file_manager_with_picker(self):
        """Open the embedded file manager with no server; the remote pane
        shows the shared host picker until one is chosen."""
        placeholder_info = self._create_file_manager_placeholder_tab(_('Files'), '')

        def _create_embedded():
            if not self._placeholder_is_open(placeholder_info):
                logger.debug("Host picker placeholder tab closed before creation; aborting.")
                return False

            try:
                widget, controller = create_internal_file_manager_tab(
                    user='',
                    host='',
                    port=None,
                    nickname='',
                    parent_window=self,
                    connection=None,
                    connection_manager=self.connection_manager,
                )
            except Exception as exc:
                logger.error("File manager with host picker failed: %s", exc, exc_info=True)
                self._handle_file_manager_placeholder_error(
                    placeholder_info, _('Files'), str(exc)
                )
                return False

            page = self._register_file_manager_tab(
                widget,
                controller,
                None,
                None,
                page=placeholder_info.get('page') if placeholder_info else None,
                container=placeholder_info.get('container') if placeholder_info else None,
            )
            if page is not None:
                page.set_title(_('Files'))

                def _on_host_picked(connection):
                    name = (getattr(connection, 'nickname', None)
                            or _get_connection_host(connection)
                            or _('Remote Host'))
                    page.set_title(_('{name} Files').format(name=name))

                controller._host_picked_callback = _on_host_picked
            return False

        if GLib.timeout_add(250, _create_embedded):
            return

    def _open_builtin_file_manager(self, connection=None):
        """Open the embedded SFTP manager, bypassing the external preference."""
        if connection is None:
            if not self.connection_manager.get_connections():
                self.get_application().activate_action('new-connection')
                return
            self._open_file_manager_with_picker()
            return
        if Capability.FILE_TRANSFER not in capabilities_for(connection):
            logger.debug("Built-in files unavailable for protocol %r",
                         getattr(connection, 'protocol', 'ssh'))
            return
        self._open_manage_files_now_for_connection(connection, force_builtin=True)

    def _open_manage_files_for_connection(self, connection):
        """Open files for the supplied connection through daemon SFTP."""
        if Capability.FILE_TRANSFER not in capabilities_for(connection):
            logger.debug("Manage Files unavailable: protocol %r has no file transfer",
                         getattr(connection, 'protocol', 'ssh'))
            return
        self._open_manage_files_now_for_connection(connection)

    def _open_manage_files_now_for_connection(self, connection, force_builtin=False):
        """Actually open the file manager (no prompts, no gating)."""

        nickname = getattr(connection, 'nickname', None) or getattr(connection, 'hostname', None) or getattr(connection, 'host', None) or getattr(connection, 'username', 'Remote Host')
        host_value = _get_connection_host(connection) or _get_connection_alias(connection)
        username = getattr(connection, 'username', '') or ''
        port_value = getattr(connection, 'port', 22)
        effective_port = port_value if port_value and port_value != 22 else None

        def error_callback(error_msg):
            message = error_msg or "Failed to open file manager"
            logger.error(f"Failed to open file manager for {nickname}: {message}")
            self._show_manage_files_error(str(nickname), message)

        # Remote SFTP is always the daemon-owned file-manager backend.  The
        # old GVFS URI route reconstructed SSH/auth state in GTK and therefore
        # cannot be a fallback when the daemon is unavailable.
        use_internal = has_internal_file_manager()

        placeholder_info = None
        if use_internal and has_internal_file_manager():
            placeholder_info = self._create_file_manager_placeholder_tab(nickname, host_value)

            def _create_embedded_file_manager():
                if not self._placeholder_is_open(placeholder_info):
                    logger.debug("Placeholder tab closed before embedded file manager creation; aborting.")
                    return False
                try:
                    widget, controller = create_internal_file_manager_tab(
                        user=str(username or ''),
                        host=str(host_value or ''),
                        port=effective_port,
                        nickname=str(nickname),
                        parent_window=self,
                        connection=connection,
                        connection_manager=self.connection_manager,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error("Embedded file manager failed: %s", exc, exc_info=True)
                    self._handle_file_manager_placeholder_error(
                        placeholder_info,
                        str(nickname or host_value or _('Remote Host')),
                        str(exc) or _('Failed to open file manager'),
                    )
                else:
                    self._register_file_manager_tab(
                        widget,
                        controller,
                        nickname,
                        host_value,
                        page=placeholder_info.get('page') if placeholder_info else None,
                        container=placeholder_info.get('container') if placeholder_info else None,
                    )
                return False

            if GLib.timeout_add(250, _create_embedded_file_manager):
                return
            # If idle_add failed, fall back to immediate creation using the placeholder
            fallback_placeholder = placeholder_info
            try:
                widget, controller = create_internal_file_manager_tab(
                    user=str(username or ''),
                    host=str(host_value or ''),
                    port=effective_port,
                    nickname=str(nickname),
                    parent_window=self,
                    connection=connection,
                    connection_manager=self.connection_manager,
                )
            except Exception as exc:
                logger.error("Embedded file manager failed: %s", exc, exc_info=True)
                self._handle_file_manager_placeholder_error(
                    fallback_placeholder,
                    str(nickname or host_value or _('Remote Host')),
                    str(exc) or _('Failed to open file manager'),
                )
            else:
                self._register_file_manager_tab(
                    widget,
                    controller,
                    nickname,
                    host_value,
                    page=fallback_placeholder.get('page') if fallback_placeholder else None,
                    container=fallback_placeholder.get('container') if fallback_placeholder else None,
                )
                return

        self._show_manage_files_error(
            str(nickname),
            _("The daemon-owned SFTP file manager is unavailable."),
        )

    def _placeholder_is_open(self, placeholder_info: Optional[dict]) -> bool:
        """Return True if the placeholder page is still attached to tab_view.

        Fails closed (returns False) if placeholder_info is missing, tab_view is
        unavailable, or any error occurs while inspecting the page model.
        """
        if not placeholder_info:
            return False
        page = placeholder_info.get('page')
        if page is None:
            return False
        if not hasattr(self, 'tab_view') or self.tab_view is None:
            return False

        try:
            pages = self.tab_view.get_pages()
            if pages is None:
                return False
            if hasattr(pages, 'get_n_items') and hasattr(pages, 'get_item'):
                n_items = pages.get_n_items()
                return any(pages.get_item(i) is page for i in range(n_items))
            return any(item is page for item in pages)
        except Exception as exc:
            logger.debug("Unable to verify placeholder page status; aborting creation: %s", exc)
            return False

    def _track_internal_file_manager_window(self, window, *, widget=None):
        """Keep a reference to in-app file manager controllers to prevent GC."""

        if window in self._internal_file_manager_windows:
            return
        self._internal_file_manager_windows.append(window)

        def _cleanup(*_args):
            try:
                self._internal_file_manager_windows.remove(window)
            except ValueError:
                pass
            return False

        if widget is not None and hasattr(widget, 'connect'):
            widget.connect('destroy', lambda *_: _cleanup())
            return

        try:
            if hasattr(window, 'connect'):
                window.connect('close-request', _cleanup)
        except Exception:  # pragma: no cover - defensive
            logger.debug('Unable to attach close handler to internal file manager window')

    def _create_file_manager_placeholder_tab(self, nickname, host_value):
        """Create and show a placeholder tab while the embedded manager loads."""

        display_name = str(nickname or host_value or _('Remote Host'))
        page_title = _('{name} Files').format(name=display_name)

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        container.set_hexpand(True)
        container.set_vexpand(True)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(200)
        stack.set_hexpand(True)
        stack.set_vexpand(True)

        inner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner_box.set_halign(Gtk.Align.CENTER)
        inner_box.set_valign(Gtk.Align.CENTER)
        inner_box.set_hexpand(True)
        inner_box.set_vexpand(True)

        spinner = Gtk.Spinner()
        spinner.start()
        inner_box.append(spinner)

        status_label = Gtk.Label(label=_('Opening file manager…'))
        status_label.set_wrap(True)
        status_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        status_label.set_justify(Gtk.Justification.CENTER)
        try:
            status_label.set_xalign(0.5)
        except AttributeError:  # pragma: no cover - older GTK
            pass
        try:
            status_label.set_halign(Gtk.Align.CENTER)
        except AttributeError:  # pragma: no cover - alignment fallback
            pass
        inner_box.append(status_label)

        stack.add_named(inner_box, "placeholder")
        stack.set_visible_child_name("placeholder")
        container.append(stack)

        page = self.tab_view.append(container)
        page.set_title(page_title)
        from sshpilot import icon_utils
        try:
            page.set_icon(icon_utils.new_gicon_from_icon_name('folder-remote-symbolic'))
        except Exception:
            page.set_icon(icon_utils.new_gicon_from_icon_name('folder-symbolic'))

        self.show_tab_view()
        self.tab_view.set_selected_page(page)

        return {
            'page': page,
            'container': container,
            'stack': stack,
            'spinner': spinner,
            'label': status_label,
            'inner_box': inner_box,
        }

    def _handle_file_manager_placeholder_error(self, placeholder_info, display_name, message):
        """Update placeholder tab to reflect an error state."""

        if placeholder_info is None:
            self._show_manage_files_error(display_name, message)
            return

        spinner = placeholder_info.get('spinner')
        if spinner is not None:
            try:
                spinner.stop()
                parent = spinner.get_parent()
                if parent is not None and hasattr(parent, 'remove'):
                    parent.remove(spinner)
            except Exception:  # pragma: no cover - defensive cleanup
                pass

        label = placeholder_info.get('label')
        if label is not None:
            try:
                label.set_label(message)
                label.add_css_class('error')
            except Exception:  # pragma: no cover - defensive styling
                pass

        self._show_manage_files_error(display_name, message)

    def _replace_placeholder_tab_content(self, container, widget):
        """Replace placeholder children with the real widget."""

        if container is None or widget is None:
            return False

        try:
            widget.set_hexpand(True)
            widget.set_vexpand(True)
        except Exception:  # pragma: no cover - optional sizing
            pass

        first_child = container.get_first_child()
        if isinstance(first_child, Gtk.Stack):
            try:
                first_child.add_named(widget, "content")
                first_child.set_visible_child_name("content")
                return True
            except Exception as exc:  # pragma: no cover - defensive stack transition
                logger.debug('Failed stack transition to content: %s', exc)

        try:
            while child := container.get_first_child():
                container.remove(child)
        except Exception as exc:  # pragma: no cover - defensive cleanup
            logger.debug('Failed to clear placeholder content: %s', exc)
            return False

        try:
            container.append(widget)
        except Exception as exc:  # pragma: no cover - defensive append
            logger.debug('Failed to attach embedded file manager to placeholder: %s', exc)
            return False

        return True

    def _register_file_manager_tab(self, widget, controller, nickname, host_value, *, page=None, container=None):
        """Add an embedded file manager tab to the tab view."""

        display_name = str(nickname or host_value or _('Remote Host'))
        page_title = _('{name} Files').format(name=display_name)

        if page is not None and container is not None:
            replaced = self._replace_placeholder_tab_content(container, widget)
            if not replaced:
                page = None

        if page is None:
            page = self.tab_view.append(widget)

        page.set_title(page_title)
        from sshpilot import icon_utils
        try:
            page.set_icon(icon_utils.new_gicon_from_icon_name('folder-remote-symbolic'))
        except Exception:
            page.set_icon(icon_utils.new_gicon_from_icon_name('folder-symbolic'))
        # Note: AdwTabPage doesn't support set_tooltip_text in GTK4
        # The title already provides the necessary information

        self._track_internal_file_manager_window(controller, widget=widget)

        self.show_tab_view()
        self.tab_view.set_selected_page(page)
        return page
