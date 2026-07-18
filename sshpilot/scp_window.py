import os
import logging
import threading
from gettext import gettext as _
from dataclasses import dataclass
from typing import List, Optional, Tuple
from pathlib import Path

import gi
try:
    gi.require_version('Vte', '3.91')
    from gi.repository import Vte
except Exception:
    Vte = None
from gi.repository import Gtk, Adw, GLib, Gio

from .command_progress_dialog import (
    build_progress_status_row,
    build_terminal_disclosure,
    normalize_child_exit_status,
    read_terminal_text,
    terminal_awaiting_input,
    wrap_dialog_terminal,
)
from .terminal import TerminalWidget
from .config import Config  # noqa: F401  # exposed for tests that patch scp_window.Config
from .connection_display import (
    get_connection_alias as _get_connection_alias,
    get_connection_host as _get_connection_host,
)
from .scp_utils import (
    _build_scp_argv_prefix,
    assemble_scp_transfer_args,
    classify_sftp_error,
    insert_legacy_scp_flag,
)
from .platform_utils import is_flatpak
from .file_manager.portal_docs import (
    _is_valid_destination,
    _pretty_path_for_display,
    resolve_granted_folder,
    restore_granted_folder,
)

logger = logging.getLogger(__name__)


@dataclass
class SCPConnectionProfile:
    alias: str
    hostname: str
    host: str
    username: str
    port: int
    ssh_options: List[str]
    saved_password: Optional[str]
    saved_passphrase: Optional[str]
    prefer_password: bool
    combined_auth: bool
    use_publickey_with_password: bool
    key_mode: int
    keyfile: str
    keyfile_ok: bool
    keyfile_expanded: str
    identity_agent_disabled: bool = False


class ScpWindowController:
    """SCP-in-a-terminal-window feature, extracted from MainWindow.

    Collaborator of MainWindow (terminal_manager-style): borrows config,
    connection_manager, toast_overlay and connection_list from ``self.window``.
    """

    def __init__(self, window):
        self.window = window
        self._scp_auth = None
        self._scp_strip_askpass = False
        self._scp_askpass_helpers = []

    def on_scp_button_clicked(self, button):
        """Prompt the user to choose between uploading or downloading with scp."""
        try:
            selected_row = self.window.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return

            chooser = Adw.MessageDialog(
                transient_for=self.window,
                modal=True,
                heading=_('Transfer files with scp'),
                body=_('Choose whether you want to upload local files to the server or download remote paths to your computer.')
            )
            chooser.add_response('cancel', _('Cancel'))
            chooser.add_response('upload', _('Upload to server…'))
            chooser.add_response('download', _('Download from server…'))
            chooser.set_default_response('upload')
            chooser.set_close_response('cancel')

            def _on_choice(dlg, response):
                dlg.close()
                if response == 'upload':
                    self._start_scp_upload_flow(connection)
                elif response == 'download':
                    self._prompt_scp_download(connection)

            chooser.connect('response', _on_choice)
            chooser.present()
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

    def _append_scp_option_pair(self, options: List[str], flag: str, value: Optional[str]) -> None:
        """Append a flag/value pair to ``options`` if it is not already present."""
        if not value:
            return

        option_value = str(value)
        if flag == '-F':
            expanded = os.path.abspath(os.path.expanduser(option_value))
            if not os.path.exists(expanded):
                return
            option_value = expanded

        for idx in range(len(options) - 1):
            if options[idx] == flag and options[idx + 1] == option_value:
                return

        options.extend([flag, option_value])

    def _extend_scp_options_from_connection(self, connection, options: List[str]) -> None:
        """Augment ``options`` with connection-specific SSH arguments."""
        try:
            config_path = getattr(connection, 'config_root', '') or ''
        except Exception:
            config_path = ''
        if not config_path and hasattr(self.window, 'connection_manager') and getattr(self.window.connection_manager, 'ssh_config_path', ''):
            config_path = getattr(self.window.connection_manager, 'ssh_config_path', '')
        if config_path:
            self._append_scp_option_pair(options, '-F', config_path)

        proxy_jump = []
        try:
            proxy_jump = list(getattr(connection, 'proxy_jump', []) or [])
        except Exception:
            proxy_jump = []
        if proxy_jump:
            hop_chain = ','.join(str(h).strip() for h in proxy_jump if str(h).strip())
            if hop_chain:
                self._append_scp_option_pair(options, '-o', f'ProxyJump={hop_chain}')

        proxy_command = ''
        try:
            proxy_command = str(getattr(connection, 'proxy_command', '') or '').strip()
        except Exception:
            proxy_command = ''
        if proxy_command:
            self._append_scp_option_pair(options, '-o', f'ProxyCommand={proxy_command}')

        if getattr(connection, 'forward_agent', False):
            self._append_scp_option_pair(options, '-o', 'ForwardAgent=yes')

        certificate_path = ''
        try:
            certificate_path = str(getattr(connection, 'certificate', '') or '').strip()
        except Exception:
            certificate_path = ''
        if certificate_path:
            expanded_cert = os.path.expanduser(certificate_path)
            if os.path.isfile(expanded_cert):
                self._append_scp_option_pair(options, '-o', f'CertificateFile={expanded_cert}')

        extra_cfg = ''
        try:
            extra_cfg = str(getattr(connection, 'extra_ssh_config', '') or '')
        except Exception:
            extra_cfg = ''
        if extra_cfg:
            for line in extra_cfg.split('\n'):
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                self._append_scp_option_pair(options, '-o', stripped)

    def _build_scp_connection_profile(self, connection) -> SCPConnectionProfile:
        alias_value = _get_connection_alias(connection)
        hostname_value = _get_connection_host(connection)
        host_value = alias_value or hostname_value
        if not host_value:
            raise ValueError(_('No host information is available for this connection.'))

        username = getattr(connection, 'username', '') or ''

        try:
            port = int(getattr(connection, 'port', 22) or 22)
        except Exception:
            port = 22

        # Update identity agent state from SSH config before using it
        if hasattr(connection, 'get_resolved_identities'):
            try:
                connection.get_resolved_identities()
            except Exception:
                pass

        keyfile = getattr(connection, 'keyfile', '') or ''
        try:
            key_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
        except Exception:
            key_mode = 0

        expanded_keyfile = keyfile
        if keyfile:
            expanded_keyfile = os.path.expanduser(keyfile)
            if not os.path.isabs(keyfile):
                try:
                    expanded_keyfile = os.path.realpath(expanded_keyfile)
                except Exception:
                    expanded_keyfile = os.path.expanduser(keyfile)
        try:
            keyfile_ok = bool(expanded_keyfile) and os.path.isfile(expanded_keyfile)
        except Exception:
            keyfile_ok = False

        try:
            auth_method = int(getattr(connection, 'auth_method', 0) or 0)
        except Exception:
            auth_method = 0
        prefer_password = (auth_method == 1)

        saved_password: Optional[str] = None
        saved_passphrase: Optional[str] = None
        combined_auth = False
        connection_manager = getattr(self.window, 'connection_manager', None)
        if connection_manager:
            try:
                saved_password = connection_manager.get_connection_password(connection)
            except Exception:
                saved_password = None

            if keyfile_ok and key_mode in (1, 2):
                try:
                    saved_passphrase = connection_manager.get_key_passphrase(expanded_keyfile)
                except Exception:
                    saved_passphrase = None
                if not saved_passphrase and keyfile and keyfile != expanded_keyfile:
                    try:
                        saved_passphrase = connection_manager.get_key_passphrase(keyfile)
                    except Exception:
                        saved_passphrase = None

        has_saved_password = bool(saved_password)
        combined_auth = (auth_method == 0 and has_saved_password)
        use_publickey_with_password = combined_auth and not getattr(connection, 'pubkey_auth_no', False)

        # Only auth-specific and connection-attribute options live here; the
        # shared option builder (_build_scp_argv_prefix / _build_base_ssh_command)
        # supplies app-level overrides, strict-host policy, port and the
        # explicit keyfile, so they must not be duplicated in this list.
        ssh_options: List[str] = []
        if getattr(connection, 'pubkey_auth_no', False):
            ssh_options += ['-o', 'PubkeyAuthentication=no']

        self._extend_scp_options_from_connection(connection, ssh_options)

        if prefer_password:
            ssh_options += ['-o', 'PreferredAuthentications=keyboard-interactive,password']
        elif combined_auth:
            ssh_options += [
                '-o',
                'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password',
            ]

        # Check both the connection attribute and the SSH options
        identity_agent_disabled = bool(
            getattr(connection, 'identity_agent_disabled', False)
        )
        
        # Also check if 'identityagent none' is in the SSH options
        if not identity_agent_disabled and ssh_options:
            ssh_opts_str = ' '.join(ssh_options).lower()
            if 'identityagent none' in ssh_opts_str or 'identityagent=none' in ssh_opts_str:
                identity_agent_disabled = True
                logger.debug("SCP: Detected 'identityagent none' in SSH options")

        return SCPConnectionProfile(
            alias=alias_value or '',
            hostname=hostname_value or '',
            host=host_value,
            username=username,
            port=port,
            ssh_options=ssh_options,
            saved_password=saved_password,
            saved_passphrase=saved_passphrase,
            prefer_password=prefer_password,
            combined_auth=combined_auth,
            use_publickey_with_password=use_publickey_with_password,
            key_mode=key_mode,
            keyfile=keyfile,
            keyfile_ok=keyfile_ok,
            keyfile_expanded=expanded_keyfile if keyfile_ok else '',
            identity_agent_disabled=identity_agent_disabled,
        )

    def _prompt_scp_download(self, connection):
        """Show a simple file picker that downloads selected remote files via scp."""
        from .window import _show_password_passphrase_dialog
        from .scp_utils import list_remote_files
        from .remote_path_utils import (
            _normalize_remote_path, _remote_parent, _remote_join,
        )
        try:
            try:
                profile = self._build_scp_connection_profile(connection)
            except ValueError:
                msg = Adw.MessageDialog(
                    transient_for=self.window,
                    modal=True,
                    heading=_('Download unavailable'),
                    body=_('No host information is available for this connection.'),
                )
                msg.add_response('ok', _('OK'))
                msg.set_default_response('ok')
                msg.set_close_response('ok')
                msg.present()
                return

            host_value = profile.host
            username = profile.username

            saved_password = profile.saved_password
            # Session-level password that can be updated via prompts
            session_password = saved_password

            if hasattr(self.window, 'connection_manager') and self.window.connection_manager:
                try:
                    if (
                        profile.key_mode in (1, 2)
                        and profile.keyfile_ok
                        and profile.keyfile_expanded
                    ):
                        if profile.identity_agent_disabled:
                            logger.debug(
                                "SCP: IdentityAgent disabled; skipping key preload"
                            )
                        else:
                            self.window.connection_manager.prepare_key_for_connection(
                                profile.keyfile_expanded
                            )
                except Exception:
                    pass

            # Get display name for password prompts
            display_name = profile.alias or f"{username}@{host_value}"

            try:
                default_download_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
            except Exception:
                default_download_dir = None
            if not default_download_dir:
                try:
                    default_download_dir = str(Path.home() / 'Downloads')
                except Exception:
                    default_download_dir = GLib.get_home_dir() or os.path.expanduser('~')

            dialog = Adw.Window()
            dialog.set_transient_for(self.window)
            dialog.set_modal(True)
            # Register with the app so routed askpass prompts can find this
            # modal window as their parent (a bare Adw.Window is absent from
            # Gtk.Application.get_windows() and get_active_window()).
            try:
                app = self.window.get_application()
                if app is not None:
                    dialog.set_application(app)
            except Exception:
                pass
            try:
                dialog.set_default_size(480, 420)
            except Exception:
                pass
            try:
                dialog.set_title(_('Download files from server'))
            except Exception:
                pass
            
            # Prompt for password/passphrase if needed (similar to SCP upload flow)
            # Check if password is needed but not available
            if profile.prefer_password and not session_password:
                password = _show_password_passphrase_dialog(
                    dialog,
                    prompt_type="password",
                    display_name=display_name,
                    host=host_value,
                    username=username,
                    connection_manager=self.window.connection_manager if hasattr(self.window, 'connection_manager') else None,
                )
                if not password:
                    # User cancelled - close dialog and return
                    dialog.close()
                    return
                session_password = password
                # Password storage is handled in the dialog if checkbox was checked
                logger.debug("SCP Download: Using prompted password for session")
            
            # Don't pre-prompt for passphrase - let SSH_ASKPASS handle it
            # The askpass script will show a GUI dialog if no passphrase is found in storage
            # This matches the standard SSH_ASKPASS behavior
            logger.debug("SCP Download: Passphrase will be handled by SSH_ASKPASS if needed")

            header = Adw.HeaderBar()
            title_label = Gtk.Label(label=_('Download files'))
            title_label.set_halign(Gtk.Align.START)
            try:
                title_label.add_css_class('title-2')
            except Exception:
                pass
            header.set_title_widget(title_label)

            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            try:
                content_box.set_margin_top(16)
                content_box.set_margin_bottom(16)
                content_box.set_margin_start(16)
                content_box.set_margin_end(16)
            except Exception:
                pass

            paths_group = Adw.PreferencesGroup()
            paths_group.set_title(_('Locations'))

            remote_row = Adw.EntryRow(title=_('Remote directory'))
            remote_row.set_text('~')
            try:
                remote_editable = remote_row.get_editable()
                if remote_editable and hasattr(remote_editable, 'set_placeholder_text'):
                    remote_editable.set_placeholder_text(_('Example: ~/ or /var/tmp'))
            except Exception:
                pass

            from sshpilot import icon_utils
            refresh_button = icon_utils.new_button_from_icon_name('view-refresh-symbolic')
            refresh_button.set_tooltip_text(_('Refresh remote listing'))
            refresh_button.add_css_class('flat')
            remote_row.add_suffix(refresh_button)
            remote_row.set_show_apply_button(False)
            paths_group.add(remote_row)


            paths_wrapper = Adw.Clamp()
            paths_wrapper.set_child(paths_group)
            content_box.append(paths_wrapper)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scroller.set_min_content_height(220)

            list_box = Gtk.ListBox()
            list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
            list_box.set_hexpand(True)
            list_box.set_vexpand(True)
            try:
                list_box.set_activate_on_single_click(False)
            except Exception:
                pass
            scroller.set_child(list_box)
            content_box.append(scroller)

            status_label = Gtk.Label()
            status_label.set_halign(Gtk.Align.START)
            status_label.set_wrap(True)
            content_box.append(status_label)

            destination_group = Adw.PreferencesGroup()
            destination_group.set_title(_('Destination'))

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

            button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            button_box.set_halign(Gtk.Align.END)

            cancel_button = Gtk.Button(label=_('Cancel'))
            button_box.append(cancel_button)

            download_button = Gtk.Button(label=_('Download'))
            download_button.set_sensitive(False)
            try:
                download_button.add_css_class('suggested-action')
            except Exception:
                pass
            button_box.append(download_button)

            content_box.append(button_box)

            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root_box.append(header)
            root_box.append(content_box)
            dialog.set_content(root_box)

            def _clear_list():
                child = list_box.get_first_child()
                while child is not None:
                    next_child = child.get_next_sibling()
                    list_box.remove(child)
                    child = next_child

            def _on_rows_changed(_list, row):
                if not row:
                    download_button.set_sensitive(False)
                    return
                download_button.set_sensitive(getattr(row, 'remote_selectable', True))

            list_box.connect('row-selected', _on_rows_changed)

            def _start_download(row: Optional[Gtk.ListBoxRow] = None):
                selected_row = row or list_box.get_selected_row()
                if not selected_row:
                    return
                remote_name = getattr(selected_row, 'remote_name', '')
                if not remote_name:
                    return
                if not getattr(selected_row, 'remote_selectable', True):
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

                # Same VTE transfer path as upload: scp runs in a terminal so
                # multi-step auth (password + OTP) stays visible. Browse dialog
                # only picks the remote path; listing uses native askpass auth.
                if session_password:
                    try:
                        connection.password = session_password
                    except Exception:
                        pass
                dialog.close()
                self._start_scp_transfer(
                    connection,
                    [remote_path],
                    str(destination_dir),
                    direction='download',
                )

            def _populate_list(entries: List[Tuple[str, bool]], directory: str, error_message: Optional[str]):
                _clear_list()
                current_dir = _normalize_remote_path(directory)

                if error_message:
                    status_label.set_text(error_message)
                    download_button.set_sensitive(False)
                    return

                parent_dir = _remote_parent(current_dir)
                if parent_dir is not None:
                    parent_row = Gtk.ListBoxRow()
                    parent_label = Gtk.Label(label='..')
                    parent_label.set_halign(Gtk.Align.START)
                    parent_label.set_hexpand(True)
                    try:
                        parent_label.add_css_class('monospace')
                    except Exception:
                        pass
                    try:
                        parent_label.add_css_class('dim-label')
                    except Exception:
                        pass
                    parent_row.set_child(parent_label)
                    try:
                        parent_row.set_selectable(False)
                        parent_row.set_activatable(True)
                    except Exception:
                        pass
                    setattr(parent_row, 'remote_name', '..')
                    setattr(parent_row, 'remote_is_dir', True)
                    setattr(parent_row, 'remote_selectable', False)
                    list_box.append(parent_row)

                if not entries:
                    status_label.set_text(
                        _('No entries found for {path}.').format(path=current_dir)
                    )
                    download_button.set_sensitive(False)
                    return

                for entry_name, is_dir in entries:
                    row = Gtk.ListBoxRow()
                    display_name = f"{entry_name}/" if is_dir else entry_name
                    label = Gtk.Label(label=display_name)
                    label.set_halign(Gtk.Align.START)
                    label.set_hexpand(True)
                    try:
                        label.add_css_class('monospace')
                    except Exception:
                        pass
                    row.set_child(label)
                    setattr(row, 'remote_name', entry_name)
                    setattr(row, 'remote_is_dir', is_dir)
                    setattr(row, 'remote_selectable', True)
                    list_box.append(row)

                status_label.set_text(_('Select an item to download.'))
                try:
                    candidate = list_box.get_first_child()
                    while candidate is not None:
                        if getattr(candidate, 'remote_selectable', True):
                            list_box.select_row(candidate)
                            break
                        candidate = candidate.get_next_sibling()
                except Exception:
                    pass

            auth_prompt_attempted = {'done': False}

            def _load_remote():
                directory = remote_row.get_text().strip() or '.'
                status_label.set_text(_('Loading…'))
                refresh_button.set_sensitive(False)
                list_box.set_sensitive(False)
                download_button.set_sensitive(False)

                def _worker():
                    nonlocal session_password
                    # Native auth (askpass) via list_remote_files / build_ssh_connection.
                    if session_password:
                        try:
                            connection.password = session_password
                        except Exception:
                            pass
                    files, error_message = list_remote_files(
                        connection,
                        directory,
                        connection_manager=getattr(
                            self.window, 'connection_manager', None
                        ),
                    )

                    from .ssh_utils import is_ssh_auth_failure_text
                    if (
                        error_message
                        and not files
                        and not auth_prompt_attempted['done']
                        and is_ssh_auth_failure_text(error_message)
                        and (profile.prefer_password or profile.saved_password
                             or session_password)
                    ):
                        auth_prompt_attempted['done'] = True
                        # The staged in-memory password was rejected — drop it
                        # so it can't shadow the keyring on later auths.
                        session_password = None
                        try:
                            connection.password = None
                        except Exception:
                            pass

                        def _prompt_and_retry():
                            nonlocal session_password
                            password = _show_password_passphrase_dialog(
                                dialog,
                                prompt_type='password',
                                display_name=display_name,
                                host=host_value,
                                username=username,
                                connection_manager=getattr(
                                    self.window, 'connection_manager', None
                                ),
                                heading=_('Password Required'),
                                body=_(
                                    'Authentication failed for {name}.\n\n'
                                    'Enter the correct password to continue:'
                                ).format(name=display_name),
                            )
                            if not password:
                                status_label.set_text(
                                    error_message or _('Authentication cancelled')
                                )
                                refresh_button.set_sensitive(True)
                                list_box.set_sensitive(True)
                                return False
                            session_password = password
                            try:
                                connection.password = password
                            except Exception:
                                pass
                            _load_remote()
                            return False

                        GLib.idle_add(_prompt_and_retry)
                        return

                    def _update():
                        _populate_list(files, directory, error_message)
                        refresh_button.set_sensitive(True)
                        list_box.set_sensitive(True)
                        selected_row = list_box.get_selected_row()
                        if selected_row is not None:
                            download_button.set_sensitive(getattr(selected_row, 'remote_selectable', True))
                        return False

                    GLib.idle_add(_update, priority=GLib.PRIORITY_DEFAULT)

                threading.Thread(target=_worker, daemon=True).start()

            def _refresh():
                _load_remote()

            refresh_button.connect('clicked', lambda *_: _refresh())
            remote_row.connect('activate', lambda *_: _refresh())
            download_button.connect('clicked', lambda *_: _start_download())

            def _on_row_activated(_box, row):
                if not row:
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
                else:
                    list_box.select_row(row)

            list_box.connect('row-activated', _on_row_activated)
            cancel_button.connect('clicked', lambda *_: dialog.close())

            dialog.present()
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
                self._start_scp_transfer(
                    connection,
                    [f.get_path() for f in files],
                    remote_dir,
                    direction='upload',
                )

            prompt.connect('response', _go)
            prompt.present()
        except Exception as e:
            logger.error(f'File selection failed: {e}')

    def _start_scp_transfer(self, connection, sources, destination, *, direction: str):
        """Run scp using the same terminal window layout as ssh-copy-id."""
        try:
            self._show_scp_terminal_window(connection, sources, destination, direction)
        except Exception as e:
            logger.error(f'scp {direction} failed to start: {e}')


    def _show_scp_terminal_window(self, connection, sources, destination, direction):
        try:
            alias_value = _get_connection_alias(connection)
            hostname_value = _get_connection_host(connection)
            host_value = alias_value or hostname_value
            target = (
                f"{connection.username}@{host_value}"
                if getattr(connection, 'username', '')
                else host_value
            )

            if direction == 'upload':
                title_text = _('Upload files (scp)')
                running_text = _('Uploading to {target}:{path}').format(
                    target=target, path=destination,
                )
                success_text = _('Uploaded to {target}:{path}').format(
                    target=target, path=destination,
                )
                failure_text = _('Failed to upload to {target}:{path}').format(
                    target=target, path=destination,
                )
                start_message = _('Starting upload…')
                success_message = _('Upload finished successfully.')
                failure_message = _('Upload failed. See output above.')
                result_heading_fail = _('Upload failed')
            elif direction == 'download':
                title_text = _('Download files (scp)')
                running_text = _('Downloading from {target}').format(target=target)
                success_text = _('Downloaded to {dest}').format(dest=destination)
                failure_text = _('Failed to download from {target}').format(
                    target=target,
                )
                start_message = _('Starting download…')
                success_message = _('Download finished successfully.')
                failure_message = _('Download failed. See output above.')
                result_heading_fail = _('Download failed')
            else:
                raise ValueError(f'Unsupported scp direction: {direction}')

            dlg = Adw.Dialog.new()
            dlg.set_title(title_text)
            dlg.set_follows_content_size(True)

            toolbar = Adw.ToolbarView()
            dlg.set_child(toolbar)

            header = Adw.HeaderBar()
            header.set_show_end_title_buttons(False)
            header.set_title_widget(Gtk.Label(label=title_text))

            scp_exit_state = {
                'finished': False,
                'handler_id': None,
                'prompt_poll_id': None,
            }

            def _cleanup_askpass_helpers() -> None:
                try:
                    if hasattr(self, '_scp_askpass_helpers'):
                        for helper_path in getattr(self, '_scp_askpass_helpers', []):
                            try:
                                os.unlink(helper_path)
                            except Exception:
                                pass
                        self._scp_askpass_helpers.clear()
                except Exception:
                    pass

            def _stop_prompt_poller() -> None:
                poll_id = scp_exit_state.get('prompt_poll_id')
                if poll_id is None:
                    return
                scp_exit_state['prompt_poll_id'] = None
                try:
                    GLib.source_remove(poll_id)
                except Exception:
                    pass

            def _on_dialog_closed(*_args):
                # Closing (Cancel/Close/Esc) kills the child below, which still
                # fires child-exited; mark finished first so cancel isn't a failure.
                scp_exit_state['finished'] = True
                _stop_prompt_poller()
                stop_progress_spinner()
                _cleanup_askpass_helpers()
                try:
                    if hasattr(term_widget, 'disconnect'):
                        term_widget.disconnect()
                except Exception:
                    pass

            dlg.connect('closed', _on_dialog_closed)

            def _close_dialog(*_args):
                dlg.close()

            cancel_btn = Gtk.Button(label=_('Cancel'))
            cancel_btn.connect('clicked', _close_dialog)
            header.pack_start(cancel_btn)

            close_btn = Gtk.Button(label=_('Close'))
            close_btn.add_css_class('suggested-action')
            close_btn.connect('clicked', _close_dialog)
            header.pack_end(close_btn)

            toolbar.add_top_bar(header)

            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            content_box.set_size_request(560, -1)
            content_box.set_margin_top(12)
            content_box.set_margin_bottom(12)
            content_box.set_margin_start(12)
            content_box.set_margin_end(12)

            (
                progress_row,
                start_progress_spinner,
                stop_progress_spinner,
                mark_progress_success,
                mark_progress_failure,
            ) = build_progress_status_row(running_text, success_text, failure_text)
            content_box.append(progress_row)

            term_widget = TerminalWidget(
                connection, self.window.config, self.window.connection_manager,
            )
            try:
                term_widget._set_connecting_overlay_visible(False)
                setattr(term_widget, '_suppress_disconnect_banner', True)
                setattr(term_widget, '_suppress_connection_exit_handling', True)
                term_widget._set_disconnected_banner_visible(False)
            except Exception:
                pass
            terminal_card = wrap_dialog_terminal(term_widget)
            terminal_card.set_size_request(-1, 260)

            def _focus_terminal_input() -> bool:
                try:
                    if hasattr(term_widget, 'vte') and term_widget.vte:
                        term_widget.vte.grab_focus()
                    else:
                        term_widget.grab_focus()
                except Exception:
                    pass
                return False

            def _on_terminal_expanded_changed(expanded: bool) -> None:
                if not expanded:
                    return
                _stop_prompt_poller()
                if not scp_exit_state['finished']:
                    GLib.idle_add(_focus_terminal_input)

            (
                terminal_disclosure,
                set_terminal_expanded,
                terminal_is_expanded,
            ) = build_terminal_disclosure(terminal_card, _on_terminal_expanded_changed)
            content_box.append(terminal_disclosure)

            toolbar.set_content(content_box)

            argv = self._build_scp_argv(
                connection,
                sources,
                destination,
                direction=direction,
                known_hosts_path=self.window.connection_manager.known_hosts_path,
            )

            env = os.environ.copy()
            # Apply the auth env from resolve_native_auth (askpass for passphrases
            # and stored login passwords; MFA stays on this VTE via prefer).
            from .scp_utils import _apply_native_auth_env
            _scp_auth = getattr(self, '_scp_auth', None)
            if _scp_auth is not None:
                _apply_native_auth_env(env, _scp_auth)
                self._scp_auth = None
                logger.debug(
                    "SCP: applied resolved auth env (askpass=%s)",
                    _scp_auth.use_askpass,
                )

            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"

            logger.debug(
                "SCP: Final environment variables: SSH_ASKPASS=%s, "
                "SSH_ASKPASS_REQUIRE=%s",
                env.get('SSH_ASKPASS', 'NOT_SET'),
                env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET'),
            )
            env_dict = dict(env)

            def _feed_colored_line(text: str, color: str):
                colors = {
                    'red': '\x1b[31m',
                    'green': '\x1b[32m',
                    'yellow': '\x1b[33m',
                    'blue': '\x1b[34m',
                }
                prefix = colors.get(color, '')
                try:
                    if hasattr(term_widget, 'backend') and term_widget.backend:
                        term_widget.backend.feed(
                            ("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8')
                        )
                    elif hasattr(term_widget, 'vte') and term_widget.vte:
                        term_widget.vte.feed(
                            ("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8')
                        )
                except Exception:
                    pass

            def _spawn_scp(spawn_argv):
                cmdline = ' '.join([GLib.shell_quote(a) for a in spawn_argv])
                logger.debug(f"SCP: Command line: {cmdline}")
                envv = [f"{k}={v}" for k, v in env_dict.items()]
                if hasattr(term_widget, 'backend') and term_widget.backend:
                    term_widget.backend.spawn_async(
                        argv=['bash', '-lc', cmdline],
                        env=env_dict if env_dict else None,
                        cwd=os.path.expanduser('~') or '/',
                        flags=0,
                        child_setup=None,
                        callback=None,
                        user_data=None,
                    )
                elif hasattr(term_widget, 'vte') and term_widget.vte:
                    term_widget.vte.spawn_async(
                        Vte.PtyFlags.DEFAULT,
                        os.path.expanduser('~') or '/',
                        ['bash', '-lc', cmdline],
                        envv,
                        GLib.SpawnFlags.DEFAULT,
                        None,
                        None,
                        -1,
                        None,
                        None,
                    )
                try:
                    term_widget._install_pty_autofill()
                except Exception:
                    logger.debug("SCP: could not arm PTY auto-fill", exc_info=True)

            # Tracks whether we have already retried using the legacy SCP
            # protocol (-O), so the fallback happens at most once.
            scp_legacy_attempted = {'done': False}
            # One password retype after a stale saved-password askpass autofill.
            scp_password_retry = {'done': False}

            def _present_failure_dialog(failure_body: str):
                _cleanup_askpass_helpers()
                mark_progress_failure()
                set_terminal_expanded(True)
                if hasattr(Adw, 'AlertDialog'):
                    msg = Adw.AlertDialog(
                        heading=result_heading_fail,
                        body=failure_body,
                    )
                    msg.add_response('ok', _('OK'))
                    msg.set_default_response('ok')
                    msg.set_close_response('ok')
                    msg.present(dlg)
                else:
                    msg = Adw.MessageDialog(
                        transient_for=self.window,
                        modal=True,
                        heading=result_heading_fail,
                        body=failure_body,
                    )
                    msg.add_response('ok', _('OK'))
                    msg.set_default_response('ok')
                    msg.set_close_response('ok')
                    msg.present()
                return False

            def _disconnect_scp_exit_handler() -> None:
                handler_id = scp_exit_state.get('handler_id')
                if handler_id is None:
                    return
                try:
                    if hasattr(term_widget, 'backend') and term_widget.backend:
                        term_widget.backend.disconnect(handler_id)
                    elif hasattr(term_widget, 'vte') and term_widget.vte:
                        term_widget.vte.disconnect(handler_id)
                except Exception:
                    pass
                scp_exit_state['handler_id'] = None

            def _finish_scp(status) -> bool:
                if scp_exit_state['finished']:
                    return False

                exit_code = normalize_child_exit_status(status)
                ok = exit_code == 0
                if ok:
                    scp_exit_state['finished'] = True
                    _stop_prompt_poller()
                    _disconnect_scp_exit_handler()
                    _cleanup_askpass_helpers()
                    mark_progress_success()
                    _feed_colored_line(success_message, 'green')
                    return False

                scraped = read_terminal_text(term_widget)
                from .ssh_utils import is_ssh_auth_failure_text
                profile = self._build_scp_connection_profile(connection)
                if (
                    not scp_password_retry['done']
                    and is_ssh_auth_failure_text(scraped)
                    and (
                        profile.prefer_password
                        or profile.saved_password
                        or getattr(connection, 'password', None)
                    )
                ):
                    scp_password_retry['done'] = True
                    # Whatever in-memory password was staged got rejected —
                    # drop it so it can't shadow the keyring on later auths.
                    try:
                        connection.password = None
                    except Exception:
                        pass

                    def _prompt_password_and_respawn():
                        if scp_exit_state['finished']:
                            return False
                        from .window_dialogs import show_ssh_password_dialog
                        display = (
                            profile.alias
                            or f"{profile.username}@{profile.host}"
                        )
                        password = show_ssh_password_dialog(
                            from_widget=dlg,
                            connection=connection,
                            host=profile.host,
                            username=profile.username,
                            display_name=display,
                            connection_manager=getattr(
                                self.window, 'connection_manager', None
                            ),
                            heading=_('Password Required'),
                            body=_(
                                'Authentication failed for {name}.\n\n'
                                'Enter the correct password to continue:'
                            ).format(name=display),
                        )
                        if not password:
                            scp_exit_state['finished'] = True
                            _stop_prompt_poller()
                            _disconnect_scp_exit_handler()
                            _feed_colored_line(failure_message, 'red')
                            return _present_failure_dialog(
                                _('Authentication cancelled')
                            )
                        try:
                            connection.password = password
                        except Exception:
                            pass
                        try:
                            retry_argv = self._build_scp_argv(
                                connection,
                                sources,
                                destination,
                                direction=direction,
                                known_hosts_path=(
                                    self.window.connection_manager.known_hosts_path
                                ),
                            )
                            # Start from the original spawn env so the Flatpak
                            # /app/bin PATH fix carries over; fresh auth wins.
                            env_retry = dict(env_dict)
                            from .scp_utils import _apply_native_auth_env
                            auth_retry = getattr(self, '_scp_auth', None)
                            if auth_retry is not None:
                                _apply_native_auth_env(env_retry, auth_retry)
                                self._scp_auth = None
                            env_dict.clear()
                            env_dict.update(env_retry)
                            _feed_colored_line(
                                _('Retrying with updated password…'), 'yellow'
                            )
                            _spawn_scp(retry_argv)
                        except Exception as exc:
                            logger.error(
                                'SCP: password-retry respawn failed: %s', exc
                            )
                            scp_exit_state['finished'] = True
                            _stop_prompt_poller()
                            _disconnect_scp_exit_handler()
                            _feed_colored_line(failure_message, 'red')
                            return _present_failure_dialog(str(exc))
                        return False

                    GLib.idle_add(_prompt_password_and_respawn)
                    return False

                # Failure: detect a missing/unavailable remote SFTP server.
                # OpenSSH 9+ scp uses the SFTP protocol by default, so retry
                # once with the legacy protocol (-O), which does not need it.
                friendly = classify_sftp_error(scraped)
                if friendly and not scp_legacy_attempted['done']:
                    scp_legacy_attempted['done'] = True
                    _feed_colored_line(
                        _('Retrying with legacy SCP protocol (-O)…'), 'yellow',
                    )
                    try:
                        legacy_argv = self._build_scp_argv(
                            connection,
                            sources,
                            destination,
                            direction=direction,
                            known_hosts_path=(
                                self.window.connection_manager.known_hosts_path
                            ),
                            legacy=True,
                        )
                        # The first attempt consumed any one-shot session
                        # password file; apply the fresh auth env from the
                        # rebuild so the retry can authenticate again.
                        env_retry = dict(env_dict)
                        from .scp_utils import _apply_native_auth_env
                        auth_retry = getattr(self, '_scp_auth', None)
                        if auth_retry is not None:
                            _apply_native_auth_env(env_retry, auth_retry)
                            self._scp_auth = None
                        env_dict.clear()
                        env_dict.update(env_retry)
                        _spawn_scp(legacy_argv)
                        return False
                    except Exception as exc:
                        logger.error(
                            'SCP: Failed to retry with legacy protocol: %s', exc
                        )

                scp_exit_state['finished'] = True
                _stop_prompt_poller()
                _disconnect_scp_exit_handler()
                _feed_colored_line(failure_message, 'red')
                failure_body = friendly or _(
                    'scp exited with an error. Please review the log output.'
                )
                return _present_failure_dialog(failure_body)

            def _on_scp_exited(widget, status):
                GLib.idle_add(_finish_scp, status)

            _feed_colored_line(start_message, 'yellow')

            try:
                if hasattr(term_widget, 'backend') and term_widget.backend:
                    scp_exit_state['handler_id'] = (
                        term_widget.backend.connect_child_exited(_on_scp_exited)
                    )
                elif hasattr(term_widget, 'vte') and term_widget.vte:
                    scp_exit_state['handler_id'] = term_widget.vte.connect(
                        'child-exited', _on_scp_exited,
                    )
            except Exception:
                pass

            def _poll_for_prompt() -> bool:
                if scp_exit_state['finished'] or terminal_is_expanded():
                    scp_exit_state['prompt_poll_id'] = None
                    return GLib.SOURCE_REMOVE
                content = read_terminal_text(term_widget)
                if terminal_awaiting_input(content):
                    scp_exit_state['prompt_poll_id'] = None
                    set_terminal_expanded(True)
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE

            try:
                _spawn_scp(argv)
            except Exception as e:
                logger.error(f'Failed to spawn scp in TerminalWidget: {e}')
                dlg.close()
                return

            scp_exit_state['prompt_poll_id'] = GLib.timeout_add(
                400, _poll_for_prompt,
            )
            dlg.present(self.window)
            GLib.idle_add(start_progress_spinner)
        except Exception as e:
            logger.error(f'Failed to open scp terminal window: {e}')

    def _build_scp_argv(
        self,
        connection,
        sources,
        destination,
        *,
        direction: str,
        known_hosts_path: Optional[str] = None,
        legacy: bool = False,
    ):
        profile = self._build_scp_connection_profile(connection)

        host_value = profile.host
        scp_host = host_value
        if scp_host and ':' in scp_host and not (scp_host.startswith('[') and scp_host.endswith(']')):
            scp_host = f"[{scp_host}]"
        username = profile.username
        target = f"{username}@{scp_host}" if username else scp_host
        transfer_sources, transfer_destination = assemble_scp_transfer_args(
            target,
            sources,
            destination,
            direction,
        )
        if hasattr(self.window, 'connection_manager') and self.window.connection_manager:
            try:
                if (
                    profile.key_mode in (1, 2)
                    and profile.keyfile_ok
                    and profile.keyfile_expanded
                ):
                    if profile.identity_agent_disabled:
                        logger.debug(
                            "SCP: IdentityAgent disabled; skipping key preload"
                        )
                    else:
                        self.window.connection_manager.prepare_key_for_connection(
                            profile.keyfile_expanded
                        )
            except Exception:
                pass
        # Resolve auth via the single shared resolver (same as terminal + ssh-copy-id):
        # askpass for passphrases and stored login passwords, or bare TTY when
        # nothing is saved. Stash it for _show_scp_terminal_window to apply.
        from .ssh_connection_builder import resolve_native_auth
        auth = resolve_native_auth(
            connection,
            getattr(self.window, 'connection_manager', None),
            getattr(self.window, 'config', None),
        )
        self._scp_auth = auth
        logger.debug("SCP: auth resolved (askpass=%s)", auth.use_askpass)

        try:
            # Downloads always recurse (`scp -r` is harmless on a regular file)
            # so directory transfers don't fail with "not a regular file"
            # (issue #1002). Uploads recurse when a local source is a directory
            # (os.path.isdir is reliable for local paths, symlinks included).
            recursive = direction == 'download' or any(
                os.path.isdir(path) for path in transfer_sources
            )
        except Exception:
            # If any path check fails (e.g. non-string items), default by
            # direction: recurse for downloads, plain for uploads.
            logger.debug('SCP: Failed to inspect sources for recursion; defaulting by direction')
            recursive = direction == 'download'

        # Shared scp prefix (same builder as the programmatic download/upload
        # path): app-level overrides, strict-host policy, port, explicit
        # keyfile, ClearAllForwardings and auth options, plus the
        # window-specific options carried by the profile.
        argv = _build_scp_argv_prefix(
            connection,
            getattr(self.window, 'config', None),
            recursive,
            known_hosts_path,
            list(profile.ssh_options),
            auth,
        )
        argv.insert(1, '-v')
        # Legacy SCP/rcp protocol (-O) does not require a remote sftp-server.
        if legacy:
            argv = insert_legacy_scp_flag(argv)

        for path in transfer_sources:
            argv.append(path)
        argv.append(transfer_destination)
        return argv
