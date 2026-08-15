"""SCP-in-a-VTE transfer UI.

Provides upload and download functionality via SCP running inside a VTE
terminal widget.
"""

import os
import logging
from gettext import gettext as _
from typing import List, Optional, Tuple
from pathlib import Path

from gi.repository import Gtk, Adw, GLib, Gio

from .connection_display import (
    get_connection_alias as _get_connection_alias,
    get_connection_host as _get_connection_host,
)
from .platform_utils import is_flatpak
from .shortcut_utils import install_esc_to_close
from .file_manager.portal_docs import (
    _is_valid_destination,
    _pretty_path_for_display,
    resolve_granted_folder,
    restore_granted_folder,
)

logger = logging.getLogger(__name__)


@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/scp_download_window.ui")
class ScpDownloadWindow(Adw.Window):
    """Static shell for the SCP download picker.

    The controller (:meth:`ScpWindowController._prompt_scp_download`) fills
    ``content_box`` with the dynamic remote-file browser and wires the Download
    button in Python; the Cancel button closes declaratively.
    """

    __gtype_name__ = "SshPilotScpDownloadWindow"

    window_title = Gtk.Template.Child()
    cancel_button = Gtk.Template.Child()
    download_button = Gtk.Template.Child()
    content_box = Gtk.Template.Child()

    def __init__(self, parent, subtitle=""):
        super().__init__()
        self.set_transient_for(parent)
        install_esc_to_close(self)
        self.window_title.set_subtitle(subtitle)

    @Gtk.Template.Callback()
    def _on_cancel(self, _button):
        self.close()


@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/scp_transfer_dialog.ui")
class ScpTransferDialog(Adw.Dialog):
    """Static shell for the SCP transfer/terminal dialog.

    The controller (:meth:`ScpWindowController._show_scp_terminal_window`) fills
    ``content_box`` with the progress row + embedded terminal and connects the
    ``closed`` cleanup in Python; Cancel/Close close declaratively.
    """

    __gtype_name__ = "SshPilotScpTransferDialog"

    title_label = Gtk.Template.Child()
    cancel_btn = Gtk.Template.Child()
    close_btn = Gtk.Template.Child()
    content_box = Gtk.Template.Child()

    def __init__(self, title_text=""):
        super().__init__()
        self.set_title(title_text)
        self.title_label.set_label(title_text)

    @Gtk.Template.Callback()
    def _on_close(self, _button):
        self.close()


class ScpWindowController:
    """SCP-in-a-terminal-window feature, extracted from MainWindow.

    Collaborator of MainWindow (terminal_manager-style): borrows config,
    connection_manager, toast_overlay and connection_list from ``self.window``.
    """

    def __init__(self, window):
        self.window = window
        self._scp_strip_askpass = False
        self._scp_askpass_helpers = []

    def _scp_download_default_dir(self) -> str:
        """Default local destination for SCP downloads (~/Downloads, else home)."""
        try:
            downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
            if downloads:
                return downloads
        except Exception:
            pass
        return str(Path.home())

    def on_scp_button_clicked(self, button):
        """Prompt the user to choose between uploading or downloading with scp."""
        selected_row = self.window.connection_list.get_selected_row()
        connection = getattr(selected_row, 'connection', None) if selected_row else None
        if connection is not None:
            self.open_for_connection(connection)

    def open_for_connection(self, connection):
        """Open the SCP transfer chooser for a connection.

        Mirrors the ssh-copy-id window: a styled ``Adw.Window`` with a header
        bar and a searchable server picker (so the target can be changed here),
        plus the Upload/Download cards.
        """
        try:
            from sshpilot import icon_utils
            from .host_picker import show_host_picker

            state = {'conn': connection}

            win = Adw.Window()
            win.set_transient_for(self.window)
            win.set_modal(True)
            win.set_title(_('Legacy: Transfer files with scp'))
            win.set_default_size(480, -1)
            try:
                win.set_resizable(False)
            except Exception:
                pass
            install_esc_to_close(win)
            # Register with the app so routed askpass prompts can find this modal
            # window as their parent (a bare Adw.Window is absent from
            # Gtk.Application.get_windows()).
            try:
                app = self.window.get_application()
                if app is not None:
                    win.set_application(app)
            except Exception:
                pass

            toolbar = Adw.ToolbarView()
            win.set_content(toolbar)

            header = Adw.HeaderBar()
            cancel_button = Gtk.Button(label=_('Cancel'))
            cancel_button.connect('clicked', lambda *_: win.close())
            header.pack_start(cancel_button)
            toolbar.add_top_bar(header)

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            content.set_margin_top(18)
            content.set_margin_bottom(18)
            content.set_margin_start(18)
            content.set_margin_end(18)
            toolbar.set_content(content)

            # ---- Server picker (mirrors SshCopyIdWindow) ----
            server_group = Adw.PreferencesGroup()
            server_row = Adw.ActionRow(title=_('Server'))
            server_row.set_activatable(True)
            server_label = Gtk.Label()
            server_label.add_css_class('dim-label')
            server_label.set_valign(Gtk.Align.CENTER)
            server_row.add_suffix(server_label)
            pick_btn = Gtk.Button()
            pick_btn.set_icon_name('pan-down-symbolic')
            pick_btn.set_tooltip_text(_('Pick from inventory'))
            pick_btn.add_css_class('flat')
            pick_btn.set_valign(Gtk.Align.CENTER)
            server_row.add_suffix(pick_btn)
            server_group.add(server_row)
            content.append(server_group)

            def _set_server(conn) -> None:
                state['conn'] = conn
                if conn is None:
                    server_label.set_label('')
                    server_label.set_visible(False)
                    server_row.set_subtitle(_('Choose a server…'))
                else:
                    nick = getattr(conn, 'nickname', '') or _get_connection_alias(conn) or ''
                    host = _get_connection_host(conn) or _get_connection_alias(conn) or ''
                    user = getattr(conn, 'username', '')
                    server_label.set_label(nick)
                    server_label.set_visible(bool(nick))
                    server_row.set_subtitle(f"{user}@{host}" if user and host else host)
                cards.set_sensitive(conn is not None)

            def _open_server_picker(*_a) -> None:
                popover = show_host_picker(
                    self.window, server_row, _set_server,
                    connections=self.window.connection_manager.get_connections(),
                )
                # Anchored to the row: stretch to its full width.
                if popover is not None:
                    width = server_row.get_width()
                    if width > 0:
                        popover.set_size_request(width, -1)

            pick_btn.connect('clicked', _open_server_picker)
            server_row.connect('activated', lambda _r: _open_server_picker())

            def _choose(action: str) -> None:
                conn = state['conn']
                win.close()
                if conn is None:
                    return
                if action == 'upload':
                    self._start_scp_upload_flow(conn)
                elif action == 'download':
                    self._prompt_scp_download(conn)

            def _choice_card(title: str, tooltip: str, icon_name: str, action: str):
                content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                content.set_halign(Gtk.Align.CENTER)
                content.set_valign(Gtk.Align.CENTER)
                content.set_hexpand(True)
                content.set_vexpand(True)
                content.set_margin_top(18)
                content.set_margin_bottom(18)
                content.set_margin_start(12)
                content.set_margin_end(12)

                icon = icon_utils.new_image_from_icon_name(icon_name, size=32)
                icon.set_halign(Gtk.Align.CENTER)
                content.append(icon)

                label = Gtk.Label(label=title)
                label.add_css_class('heading')
                label.set_halign(Gtk.Align.CENTER)
                content.append(label)

                button = Gtk.Button()
                button.set_child(content)
                button.add_css_class('card')
                button.set_hexpand(True)
                button.set_vexpand(True)
                # Same footprint for both cards regardless of label length.
                button.set_size_request(148, 120)
                button.set_tooltip_text(tooltip)
                button.connect('clicked', lambda *_: _choose(action))
                return button

            cards = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            cards.set_hexpand(True)
            cards.set_homogeneous(True)
            cards.append(
                _choice_card(
                    _('Upload'),
                    _('Choose local files and send them with scp'),
                    'arrow1-up-symbolic',
                    'upload',
                )
            )
            cards.append(
                _choice_card(
                    _('Download'),
                    _('Browse remote paths and save them locally'),
                    'arrow1-down-symbolic',
                    'download',
                )
            )

            content.append(cards)

            _set_server(connection)
            win.present()
        except Exception as e:
            logger.error(f'SCP transfer chooser failed: {e}')

    def _start_scp_upload_flow(self, connection):
        """Kick off the upload flow using a portal-aware file chooser."""
        try:
            file_dialog = Gtk.FileDialog(title=_('Select files to upload'))
            file_dialog.open_multiple(
                self.window,
                None,
                lambda fd, res: self._on_upload_files_chosen(fd, res, connection),
            )
        except Exception as e:
            logger.error(f'Upload dialog failed: {e}')

    def _prompt_scp_download(self, connection, _sftp_service_id=None):
        """Show a typed daemon-backed remote browser for SCP downloads."""
        from .api import Capability
        from .api.models import ConnectionId
        from .remote_path_utils import (
            _normalize_remote_path, _remote_parent, _remote_join,
        )
        try:
            client = getattr(self.window, "client", None)
            bridge = getattr(self.window, "client_bridge", None)
            if client is None or bridge is None:
                self._show_transfer_error("The daemon transfer service is unavailable.")
                return
            capabilities = client.get_capabilities()
            if not capabilities.supports(Capability.SFTP_READ):
                self._show_transfer_error("Remote browsing requires the daemon SFTP service.")
                return
            display_name = (
                getattr(connection, "display_name", None)
                or getattr(connection, "nickname", "")
                or getattr(connection, "host", "")
            )
            sftp_service_id = _sftp_service_id
            browser_closed = {"value": False}
            request_generation = {"value": 0}
            default_download_dir = self._scp_download_default_dir()
            dialog = ScpDownloadWindow(self.window, subtitle=display_name)
            # Prompts raised after the browser appears (e.g. a later
            # re-verification on the same SFTP service) must stack above the
            # browser dialog, not the main window.
            dialogs_holder = getattr(self, "_sftp_browser_dialogs_holder", None)
            if dialogs_holder is not None:
                dialogs = dialogs_holder["value"]
                if dialogs is not None:
                    try:
                        dialogs.set_parent(dialog)
                    except Exception:
                        logger.debug(
                            "SCP browser interaction presenter re-parent failed",
                            exc_info=True,
                        )
            # Register with the app so routed askpass prompts can find this
            # modal window as their parent (a bare Adw.Window is absent from
            # Gtk.Application.get_windows() and get_active_window()).
            try:
                app = self.window.get_application()
                if app is not None:
                    dialog.set_application(app)
            except Exception:
                pass

            # Attach the interaction presenter before opening the SFTP service
            # so authentication prompts are visible even while this browser is
            # in its initial Connecting state.
            if dialogs_holder is None:
                dialogs_holder = {"value": None}
                try:
                    from .daemon_interaction_dialogs import DaemonInteractionDialogs

                    dialogs_holder["value"] = DaemonInteractionDialogs(
                        client, bridge, self.window
                    )
                except Exception:
                    logger.debug(
                        "SCP browser interaction presenter unavailable", exc_info=True
                    )
                self._sftp_browser_dialogs_holder = dialogs_holder

            def _on_browser_state_changed(summary):
                if browser_closed["value"]:
                    return
                dialogs = dialogs_holder["value"]
                if dialogs is None:
                    return
                try:
                    from .api.models.common import SessionId

                    dialogs.set_session(SessionId(str(summary.id)))
                except Exception:
                    logger.debug(
                        "SCP browser interaction presenter bind failed",
                        exc_info=True,
                    )

            def _on_browser_ready(summary):
                nonlocal sftp_service_id
                if browser_closed["value"]:
                    return
                sftp_service_id = summary.id
                _load_remote()

            def _on_browser_error(error):
                if browser_closed["value"]:
                    return
                self._close_sftp_browser_controller()
                status_label.set_text(str(error))
                refresh_button.set_sensitive(False)
                list_box.set_sensitive(False)
                download_button.set_sensitive(False)

            def _on_dialog_close(*_args):
                browser_closed["value"] = True
                request_generation["value"] += 1
                self._close_sftp_browser_controller()

            dialog.connect("close-request", _on_dialog_close)

            from sshpilot import icon_utils

            # Static shell (header, WindowTitle, Cancel/Download buttons) is in the
            # template; alias its children to the local names this method uses.
            content_box = dialog.content_box
            download_button = dialog.download_button

            paths_group = Adw.PreferencesGroup()
            paths_group.set_title(_('Locations'))
            paths_group.add_css_class('boxed-list')

            remote_row = Adw.EntryRow(title=_('Remote directory'))
            remote_row.set_text('~')
            try:
                remote_editable = remote_row.get_editable()
                if remote_editable and hasattr(remote_editable, 'set_placeholder_text'):
                    remote_editable.set_placeholder_text(_('Example: ~/ or /var/tmp'))
            except Exception:
                pass

            refresh_button = icon_utils.new_button_from_icon_name('view-refresh-symbolic')
            refresh_button.set_tooltip_text(_('Refresh remote listing'))
            refresh_button.add_css_class('flat')
            remote_row.add_suffix(refresh_button)
            remote_row.set_show_apply_button(False)
            paths_group.add(remote_row)

            paths_wrapper = Adw.Clamp()
            paths_wrapper.set_maximum_size(560)
            paths_wrapper.set_child(paths_group)
            content_box.append(paths_wrapper)

            files_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            files_heading = Gtk.Label(label=_('Remote files'))
            files_heading.set_halign(Gtk.Align.START)
            files_heading.add_css_class('heading')
            files_box.append(files_heading)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_min_content_height(220)
            scroller.set_vexpand(True)
            scroller.set_hexpand(True)

            list_box = Gtk.ListBox()
            list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
            list_box.add_css_class('boxed-list')
            try:
                list_box.set_activate_on_single_click(False)
            except Exception:
                pass
            list_box.set_hexpand(True)
            list_box.set_vexpand(True)
            scroller.set_child(list_box)
            files_box.append(scroller)

            files_wrapper = Adw.Clamp()
            files_wrapper.set_maximum_size(560)
            files_wrapper.set_child(files_box)
            content_box.append(files_wrapper)

            status_label = Gtk.Label()
            status_label.set_halign(Gtk.Align.START)
            status_label.set_wrap(True)
            status_label.add_css_class('dim-label')
            status_label.add_css_class('caption')
            content_box.append(status_label)

            destination_group = Adw.PreferencesGroup()
            destination_group.set_title(_('Destination'))
            destination_group.add_css_class('boxed-list')

            # Under Flatpak the destination is a portal-granted folder shown as a
            # read-only label — an ActionRow, which has no edit affordance, so it
            # doesn't masquerade as a typeable field; the "Choose download folder"
            # button sets it. Outside Flatpak it is an editable EntryRow the user
            # can type a path into.
            if is_flatpak():
                local_row = Adw.ActionRow(title=_('Local destination'))
                try:
                    local_row.set_subtitle_selectable(True)
                except Exception:
                    pass
            else:
                local_row = Adw.EntryRow(title=_('Local destination'))

            def _set_local_text(text: str) -> None:
                if isinstance(local_row, Adw.EntryRow):
                    local_row.set_text(text)
                else:
                    local_row.set_subtitle(text)

            def _get_local_text() -> str:
                if isinstance(local_row, Adw.EntryRow):
                    return local_row.get_text()
                return local_row.get_subtitle() or ''

            # ``resolved_destination`` holds the portal-mounted path (writable in
            # the sandbox) plus a clean display string for status/toast messages.
            # Outside Flatpak ``path`` stays None and the entry text is used.
            resolved_destination = {'path': None, 'display': None}

            def _set_flatpak_path_bar_visible(visible: bool) -> None:
                local_row.set_visible(visible)
                destination_group.set_visible(visible)

            if is_flatpak():
                # Auto-restore the last granted folder so the user need not pick a
                # destination every session (parity with the file manager).
                try:
                    restored = restore_granted_folder()
                except Exception as exc:
                    logger.debug(f'Could not restore granted folder: {exc}')
                    restored = None
                if restored:
                    resolved_destination['path'] = restored['path']
                    resolved_destination['display'] = restored['display']
                    _set_local_text(restored['display'])
                    local_row.set_sensitive(True)
                    _set_flatpak_path_bar_visible(True)
                else:
                    # No saved grant: hide the path bar until a folder is chosen.
                    _set_local_text('')
                    local_row.set_sensitive(False)
                    _set_flatpak_path_bar_visible(False)
            else:
                _set_local_text(str(default_download_dir))
                try:
                    local_editable = local_row.get_editable()
                    if local_editable and hasattr(local_editable, 'set_placeholder_text'):
                        local_editable.set_placeholder_text(_('Example: ~/Downloads'))
                except Exception:
                    pass

            if isinstance(local_row, Adw.EntryRow):
                local_row.set_show_apply_button(False)
            destination_group.add(local_row)

            # Outside Flatpak: a flat folder-icon suffix opens the picker. Under
            # Flatpak the row is a read-only label, so the picker is triggered by a
            # separate, always-enabled "Choose download folder" button below it.
            request_access_button = None
            if is_flatpak():
                request_access_button = Gtk.Button(label=_('Choose download folder'))
                request_access_button.set_halign(Gtk.Align.CENTER)
                request_access_button.add_css_class('pill')
                request_access_button.add_css_class('suggested-action')
                request_access_button.set_margin_top(6)
                destination_group_box = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL, spacing=0
                )
                destination_group_box.append(destination_group)
                destination_group_box.append(request_access_button)
                destination_child = destination_group_box
            else:
                picker_button = icon_utils.new_button_from_icon_name('folder-symbolic')
                picker_button.set_tooltip_text(_('Choose destination folder'))
                picker_button.add_css_class('flat')
                local_row.add_suffix(picker_button)
                destination_child = destination_group

            destination_wrapper = Adw.Clamp()
            destination_wrapper.set_maximum_size(560)
            destination_wrapper.set_child(destination_child)
            content_box.append(destination_wrapper)

            def _open_destination_picker():
                file_dialog = Gtk.FileDialog(title=_('Select destination folder'))
                # Under Flatpak the row shows a display string (e.g. a basename),
                # not a real path; use the previously resolved portal path as the
                # initial folder instead. Outside Flatpak the row text is a path.
                initial_candidate = resolved_destination.get('path') or _get_local_text().strip()
                if initial_candidate:
                    try:
                        expanded = os.path.expanduser(initial_candidate)
                        if os.path.isdir(expanded):
                            file_dialog.set_initial_folder(Gio.File.new_for_path(expanded))
                    except Exception:
                        pass

                def _on_destination_chosen(dialog: Gtk.FileDialog, result):
                    nonlocal default_download_dir
                    try:
                        folder = dialog.select_folder_finish(result)
                    except GLib.Error as err:
                        dialog_error = getattr(Gtk, 'DialogError', None)
                        if dialog_error is not None and err.matches(dialog_error, dialog_error.DISMISSED):
                            return
                        if err.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED):
                            return
                        logger.error(f'Destination chooser failed: {err.message}')
                        status_label.set_text(
                            _('Could not select destination: {error}').format(error=err.message)
                        )
                        return

                    if not folder:
                        return

                    path = folder.get_path()
                    if not path:
                        return

                    if is_flatpak():
                        # Grant persistent access via the Document portal and use
                        # the portal-mounted path (writable in the sandbox) as the
                        # scp destination — mirrors the file manager's folder
                        # picker. The raw host path is not reachable by scp.
                        granted = resolve_granted_folder(folder)
                        if granted:
                            resolved_destination['path'] = granted['path']
                            resolved_destination['display'] = granted['display']
                            # Show the full real path, not the portal mount basename.
                            _set_local_text(granted['display'])
                            local_row.set_sensitive(True)
                            _set_flatpak_path_bar_visible(True)
                        else:
                            status_label.set_text(
                                _('Could not get write access to the selected folder.')
                            )
                        return

                    default_download_dir = path
                    resolved_destination['path'] = None
                    resolved_destination['display'] = None
                    _set_local_text(path)

                try:
                    file_dialog.select_folder(dialog, None, _on_destination_chosen)
                except Exception as err:
                    logger.error(f'Failed to present destination chooser: {err}')
                    status_label.set_text(
                        _('Could not open destination chooser: {error}').format(error=str(err))
                    )

            if request_access_button is not None:
                request_access_button.connect('clicked', lambda *_: _open_destination_picker())
            else:
                picker_button.connect('clicked', lambda *_: _open_destination_picker())

            def _on_selection_changed(_list, row):
                download_button.set_sensitive(
                    not browser_closed["value"]
                    and sftp_service_id is not None
                    and row is not None
                    and getattr(row, 'remote_selectable', True)
                )

            list_box.connect('row-selected', _on_selection_changed)
            download_button.set_sensitive(False)

            def _start_download(row=None):
                selected_row = row or list_box.get_selected_row()
                if selected_row is None:
                    return
                remote_name = getattr(selected_row, 'remote_name', '')
                if not remote_name:
                    return
                if not getattr(selected_row, 'remote_selectable', True):
                    return
                if sftp_service_id is None or browser_closed["value"]:
                    return

                current_dir = _normalize_remote_path(remote_row.get_text())
                if current_dir in ('.', ''):
                    remote_path = remote_name
                else:
                    remote_path = _remote_join(current_dir, remote_name)

                # ``dest_display`` is the user-facing label (real folder path);
                # ``destination_dir`` is what scp actually writes to (a portal
                # path under Flatpak). They differ in the sandbox and must not be
                # conflated in any message.
                if resolved_destination['path']:
                    # Flatpak: scp writes to the portal-mounted path. It already
                    # exists (the portal exported the granted folder); never
                    # mkdir it, or we'd recreate an inaccessible phantom path in
                    # the sandbox tmpfs and silently lose the transfer.
                    destination_dir = Path(resolved_destination['path'])
                    dest_display = (
                        resolved_destination['display']
                        or _pretty_path_for_display(str(destination_dir))
                    )
                    # Require a real, writable portal mount. A bogus target like
                    # ``/`` would let scp "succeed" into the ephemeral sandbox root
                    # and silently lose the file.
                    if not _is_valid_destination(str(destination_dir)):
                        status_label.set_text(
                            _('Cannot access {dest}. Choose the destination folder again.').format(
                                dest=dest_display,
                            )
                        )
                        return
                elif is_flatpak():
                    # No portal-granted destination yet: the user must grant one.
                    status_label.set_text(
                        _('Click “Choose download folder” to pick a destination first.')
                    )
                    return
                else:
                    destination_text = _get_local_text().strip()
                    if not destination_text:
                        destination_text = str(default_download_dir)
                    try:
                        destination_dir = Path(destination_text).expanduser()
                    except Exception:
                        destination_dir = Path(destination_text)
                    dest_display = str(destination_dir)

                    try:
                        destination_dir.mkdir(parents=True, exist_ok=True)
                    except Exception as exc:
                        status_label.set_text(
                            _('Cannot access {dest}: {error}').format(
                                dest=dest_display,
                                error=str(exc),
                            )
                        )
                        return

                dialog.close()
                self.start_scp_transfer(
                    connection,
                    [remote_path],
                    str(destination_dir),
                    direction='download',
                )

            from .file_type_icons import get_icon_for_name

            def _make_remote_row(name: str, is_directory: bool, selectable: bool = True):
                row = Adw.ActionRow(title=name)
                if name == '..':
                    row.set_subtitle(_('Parent directory'))
                    icon_name = 'go-up-symbolic'
                elif is_directory:
                    row.set_subtitle(_('Directory'))
                    icon_name = get_icon_for_name(name, True)
                else:
                    icon_name = get_icon_for_name(name, False)
                row.add_prefix(icon_utils.new_image_from_icon_name(icon_name))
                row.set_activatable(True)
                row.remote_name = name
                row.remote_is_dir = is_directory
                row.remote_selectable = selectable
                try:
                    row.set_selectable(selectable)
                except Exception:
                    pass
                return row

            def _populate_list(
                entries: List[Tuple[str, bool]],
                directory: str,
                error_message: Optional[str],
                generation: int,
            ):
                child = list_box.get_first_child()
                while child is not None:
                    next_child = child.get_next_sibling()
                    list_box.remove(child)
                    child = next_child
                current_dir = _normalize_remote_path(directory)

                if error_message:
                    status_label.set_text(error_message)
                    list_box.set_sensitive(True)
                    download_button.set_sensitive(False)
                    return

                row_specs = []
                parent_dir = _remote_parent(current_dir)
                if parent_dir is not None:
                    row_specs.append(('..', True, False))
                row_specs.extend(
                    (entry_name, is_dir, True) for entry_name, is_dir in entries
                )

                cursor = {'value': 0}
                chunk_size = 50

                def _append_chunk():
                    if (
                        browser_closed["value"]
                        or generation != request_generation["value"]
                    ):
                        return GLib.SOURCE_REMOVE

                    start = cursor['value']
                    end = min(start + chunk_size, len(row_specs))
                    for name, is_directory, selectable in row_specs[start:end]:
                        list_box.append(
                            _make_remote_row(name, is_directory, selectable)
                        )
                    cursor['value'] = end

                    if list_box.get_selected_row() is None:
                        candidate = list_box.get_first_child()
                        while candidate is not None:
                            if getattr(candidate, 'remote_selectable', True):
                                list_box.select_row(candidate)
                                break
                            candidate = candidate.get_next_sibling()

                    if end < len(row_specs):
                        return GLib.SOURCE_CONTINUE

                    if entries:
                        status_label.set_text(_('Select an item to download.'))
                    else:
                        status_label.set_text(
                            _('No entries found for {path}.').format(path=current_dir)
                        )
                    list_box.set_sensitive(True)
                    _on_selection_changed(list_box, list_box.get_selected_row())
                    return GLib.SOURCE_REMOVE

                GLib.idle_add(_append_chunk)

            def _load_remote():
                if browser_closed["value"]:
                    return
                if sftp_service_id is None:
                    status_label.set_text(_('Connecting…'))
                    list_box.set_sensitive(False)
                    download_button.set_sensitive(False)
                    return
                directory = remote_row.get_text().strip() or "."
                request_generation["value"] += 1
                generation = request_generation["value"]
                status_label.set_text(_('Loading…'))
                refresh_button.set_sensitive(False)
                list_box.set_sensitive(False)
                download_button.set_sensitive(False)

                from .api.models import ConnectionId, ListDirectoryRequest

                try:
                    request = ListDirectoryRequest(
                        connection_id=ConnectionId(str(getattr(connection, "id", ""))),
                        service_id=sftp_service_id,
                        path=directory,
                    )
                    bridge.submit(
                        lambda: client.sftp_list_directory(request),
                        on_success=lambda result: _on_remote_loaded(
                            result, directory, generation
                        ),
                        on_error=lambda error: _on_remote_failed(error, generation),
                    )
                except (TypeError, ValueError) as error:
                    _on_remote_failed(error, generation)
                except RuntimeError as error:
                    _on_remote_failed(error, generation)

            def _on_remote_loaded(result, directory, generation):
                if (
                    browser_closed["value"]
                    or generation != request_generation["value"]
                ):
                    return
                entries = [
                    (
                        entry.name,
                        entry.file_type.value == "directory",
                    )
                    for entry in result.entries
                ]
                _populate_list(entries, directory, None, generation)
                refresh_button.set_sensitive(True)

            def _on_remote_failed(error, generation):
                if (
                    browser_closed["value"]
                    or generation != request_generation["value"]
                ):
                    return
                _populate_list(
                    [], remote_row.get_text().strip() or ".", str(error), generation
                )
                refresh_button.set_sensitive(True)
                download_button.set_sensitive(False)

            def _refresh():
                _load_remote()

            refresh_button.connect('clicked', lambda *_: _refresh())
            # ``Adw.EntryRow`` has no ``activate`` signal: ``activate`` here
            # binds the inherited ``GtkListBoxRow::activate`` (row body
            # activation), while pressing Enter inside the embedded entry
            # emits ``entry-activated``. Connecting the wrong signal left
            # Enter dead.
            remote_row.connect('entry-activated', lambda *_: _refresh())
            download_button.connect('clicked', lambda *_: _start_download())

            def _on_item_activated(_box, row):
                if row is None:
                    return
                remote_name = getattr(row, 'remote_name', '')
                if not remote_name:
                    return
                current_dir = remote_row.get_text().strip() or '.'
                if remote_name == '..':
                    parent = _remote_parent(current_dir)
                    if parent is None:
                        return
                    remote_row.set_text(parent)
                    _refresh()
                elif getattr(row, 'remote_is_dir', False):
                    new_dir = _remote_join(current_dir, remote_name)
                    remote_row.set_text(new_dir)
                    _refresh()

            list_box.connect('row-activated', _on_item_activated)

            dialog.present()
            if sftp_service_id is None:
                status_label.set_text(_('Connecting…'))
                list_box.set_sensitive(False)
                from .sftp_service_controller import DaemonSftpServiceController

                self._sftp_browser_controller = DaemonSftpServiceController(
                    client,
                    bridge,
                    ConnectionId(str(getattr(connection, "id", ""))),
                    on_ready=_on_browser_ready,
                    on_state_changed=_on_browser_state_changed,
                    on_error=_on_browser_error,
                )
                self._sftp_browser_controller.open()
            else:
                _load_remote()
        except Exception as e:
            logger.error(f'SCP download prompt failed: {e}')

    def _on_upload_files_chosen(self, dialog, result, connection):
        try:
            files_model = dialog.open_multiple_finish(result)
            if not files_model or files_model.get_n_items() == 0:
                return
            files = [files_model.get_item(i) for i in range(files_model.get_n_items())]

            prompt = Adw.MessageDialog(
                transient_for=self.window,
                modal=True,
                heading=_('Remote destination'),
                body=_('Enter a remote directory (e.g., ~/ or /var/tmp). Files will be uploaded using scp.')
            )
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            dest_row = Adw.EntryRow(title=_('Remote directory'))
            dest_row.set_text('~')
            box.append(dest_row)
            prompt.set_extra_child(box)
            prompt.add_response('cancel', _('Cancel'))
            prompt.add_response('upload', _('Upload'))
            prompt.set_default_response('upload')
            prompt.set_close_response('cancel')

            def _go(d, resp):
                d.close()
                if resp != 'upload':
                    return
                remote_dir = dest_row.get_text().strip() or '~'
                self.start_scp_transfer(
                    connection,
                    [f.get_path() for f in files],
                    remote_dir,
                    direction='upload',
                )

            prompt.connect('response', _go)
            prompt.present()
        except Exception as e:
            logger.error(f'File selection failed: {e}')

    def start_scp_transfer(self, connection, sources, destination, *, direction: str):
        """Start native SCP through the daemon transfer API."""
        from .api import Capability
        from .api.models import (
            CancelTransferRequest,
            StartScpTransferRequest,
            TransferConflictPolicy,
            TransferDirection,
            TransferState,
        )

        client = getattr(self.window, "client", None)
        bridge = getattr(self.window, "client_bridge", None)
        if client is None or bridge is None:
            self._show_transfer_error("The daemon transfer service is unavailable.")
            return
        # Attach the interaction presenter before starting the transfer so a
        # password/passphrase/host-key/FIDO prompt during the handshake is never
        # missed. It starts unbound (ignoring every interaction); once the
        # daemon returns the TransferSummary it binds to the transfer ID — the
        # one public scope the SCP backend scopes its interactions to — and
        # set_session() reconciles any prompt already created.
        dialogs_holder = {"value": None}
        try:
            if bridge is not None:
                from .daemon_interaction_dialogs import DaemonInteractionDialogs

                dialogs_holder["value"] = DaemonInteractionDialogs(
                    client, bridge, self.window
                )
        except Exception:
            logger.debug("SCP interaction presenter unavailable", exc_info=True)

        def dispose_dialogs():
            dialogs = dialogs_holder["value"]
            dialogs_holder["value"] = None
            if dialogs is not None:
                try:
                    dialogs.close()
                except Exception:
                    logger.debug("SCP interaction presenter close failed", exc_info=True)

        try:
            if not client.get_capabilities().supports(Capability.TRANSFERS_SCP):
                self._show_transfer_error(
                    "Native SCP transfers are unavailable on the daemon."
                )
                dispose_dialogs()
                return
            request = StartScpTransferRequest(
                connection_id=str(getattr(connection, "id", "") or getattr(connection, "nickname", "")),
                direction=TransferDirection(direction),
                sources=tuple(str(source) for source in sources),
                destination=str(destination),
                conflict_policy=TransferConflictPolicy.OVERWRITE,
                recursive=direction == "download" or any(
                    os.path.isdir(str(source)) for source in sources
                ),
            )
        except (TypeError, ValueError) as error:
            dispose_dialogs()
            self._show_transfer_error(str(error))
            return

        dlg = ScpTransferDialog(
            "Upload files (SCP)" if direction == "upload" else "Download files (SCP)"
        )
        content_box = dlg.content_box
        status = Gtk.Label()
        status.set_wrap(True)
        status.set_halign(Gtk.Align.START)
        content_box.append(status)
        transfer_id = {"value": None}
        closed = {"value": False}
        cancel_requested = {"value": False}
        event_subscription = {"value": None}

        def stop_observing():
            subscription = event_subscription["value"]
            event_subscription["value"] = None
            if subscription is not None:
                try:
                    subscription.unsubscribe()
                except Exception:
                    pass

        def on_transfer_summary(summary):
            if summary.id != transfer_id["value"]:
                return
            state = summary.state
            if state in {TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED}:
                stop_observing()
                # The transfer's interaction scope is done: release the
                # presenter so it never claims unrelated interactions again.
                dispose_dialogs()
                if state is TransferState.COMPLETED:
                    status.set_text("Completed")
                elif state is TransferState.CANCELLED:
                    status.set_text("Cancelled")
                else:
                    failure = getattr(summary.failure, "message", "The transfer failed")
                    status.set_text(f"Failed: {failure}")
            elif state is TransferState.STARTING:
                status.set_text("Starting…")
            elif state is TransferState.CANCELLING:
                status.set_text("Cancelling…")
            else:
                status.set_text("Uploading…" if direction == "upload" else "Downloading…")

        def ensure_observing():
            if event_subscription["value"] is not None:
                return
            subscribe = getattr(client, "subscribe_events", None)
            if not callable(subscribe):
                return
            try:
                event_subscription["value"] = subscribe(
                    lambda event: GLib.idle_add(
                        lambda: (on_transfer_summary(event.payload), False)[1]
                        if getattr(event.payload, "id", None) == transfer_id["value"]
                        else False
                    )
                )
            except Exception:
                logger.debug("SCP transfer event subscription failed", exc_info=True)

        def cancel_active_transfer():
            current = transfer_id["value"]
            if not current or cancel_requested["value"]:
                return
            cancel_requested["value"] = True
            try:
                bridge.submit(
                    lambda: client.cancel_transfer(
                        CancelTransferRequest(transfer_id=current)
                    ),
                    on_success=lambda _result: None,
                    on_error=lambda error: logger.debug(
                        "SCP cancellation failed type=%s", type(error).__name__
                    ),
                )
            except RuntimeError:
                pass

        def finish_close(*_args):
            closed["value"] = True
            stop_observing()
            dispose_dialogs()
            cancel_active_transfer()

        dlg.connect("closed", finish_close)
        dlg.present(self.window)
        status.set_text("Starting SCP transfer…")

        def on_started(summary):
            transfer_id["value"] = summary.id
            dialogs = dialogs_holder["value"]
            if dialogs is not None:
                # Bind the presenter to the transfer's public ID (the one
                # scope the daemon scopes SCP interactions to) and reconcile
                # any prompt created before the summary arrived.
                from .api.models.common import SessionId

                try:
                    dialogs.set_session(SessionId(str(summary.id)))
                    dialogs.set_parent(dlg)
                except Exception:
                    logger.debug("SCP interaction presenter bind failed", exc_info=True)
            ensure_observing()
            if closed["value"]:
                cancel_active_transfer()
                return
            on_transfer_summary(summary)

        def on_failed(error):
            dispose_dialogs()
            if not closed["value"]:
                self._show_transfer_error(str(error))
                status.set_text(f"Transfer failed: {error}")

        try:
            bridge.submit(
                lambda: client.start_scp_transfer(request),
                on_success=on_started,
                on_error=on_failed,
            )
        except RuntimeError as error:
            on_failed(error)

    def _close_sftp_browser_controller(self) -> None:
        controller = getattr(self, "_sftp_browser_controller", None)
        self._sftp_browser_controller = None
        dialogs_holder = getattr(self, "_sftp_browser_dialogs_holder", None)
        self._sftp_browser_dialogs_holder = None
        if dialogs_holder is not None:
            dialogs = dialogs_holder["value"]
            dialogs_holder["value"] = None
            if dialogs is not None:
                try:
                    dialogs.close()
                except Exception:
                    logger.debug(
                        "SCP browser interaction presenter close failed",
                        exc_info=True,
                    )
        if controller is not None:
            try:
                controller.close()
            except Exception:
                logger.debug("SCP browser SFTP controller close failed", exc_info=True)

    def _show_transfer_error(self, message: str) -> None:
        logger.error("SCP transfer unavailable: %s", message)
        try:
            self.window.show_toast(message)
        except Exception:
            pass
