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
        self._scp_askpass_env = {}
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
        from .window import list_remote_files, _show_password_passphrase_dialog
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
            port = profile.port

            known_hosts_path = None
            saved_password = profile.saved_password
            # Session-level password that can be updated via prompts
            session_password = saved_password
            # Passphrase will be handled by SSH_ASKPASS (either from storage or GUI prompt)
            
            if hasattr(self.window, 'connection_manager') and self.window.connection_manager:
                known_hosts_path = getattr(self.window.connection_manager, 'known_hosts_path', None)
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

            ssh_extra_opts = list(profile.ssh_options)
            use_publickey_with_password = profile.use_publickey_with_password
            if profile.prefer_password:
                use_publickey_with_password = False

            # Set up askpass environment for passphrase-protected keys
            # SSH_ASKPASS will handle passphrase retrieval from storage or show GUI dialog if needed
            logger.debug(f"SCP Download: Checking identity_agent_disabled={profile.identity_agent_disabled}")
            logger.debug(f"SCP Download: Initial ssh_extra_opts={ssh_extra_opts}")
            base_env = os.environ.copy()
            
            # Set up askpass if we have a keyfile and not using password authentication
            if profile.keyfile_ok and not profile.prefer_password:
                from .askpass_utils import get_ssh_env_with_askpass, get_ssh_env_with_forced_askpass, get_scp_ssh_options
                
                # Use forced askpass if identity agent is disabled, otherwise use regular askpass
                if profile.identity_agent_disabled:
                    base_env = get_ssh_env_with_forced_askpass()
                    logger.debug("SCP: Using forced askpass environment (identity agent disabled)")
                else:
                    base_env = get_ssh_env_with_askpass()
                    logger.debug("SCP: Using askpass environment (identity agent enabled)")
                
                # Add SSH options to force publickey authentication only (when identity agent disabled)
                if profile.identity_agent_disabled:
                    scp_ssh_opts = get_scp_ssh_options()
                    logger.debug(f"SCP: Current ssh_extra_opts before adding: {ssh_extra_opts}")
                    
                    # Add options in pairs, checking for duplicates properly
                    for i in range(0, len(scp_ssh_opts), 2):
                        if i + 1 < len(scp_ssh_opts):
                            flag = scp_ssh_opts[i]
                            value = scp_ssh_opts[i + 1]
                            # Check if this exact option pair is already present
                            already_present = False
                            for j in range(0, len(ssh_extra_opts) - 1, 2):
                                if ssh_extra_opts[j] == flag and ssh_extra_opts[j + 1] == value:
                                    already_present = True
                                    break
                            if not already_present:
                                ssh_extra_opts.extend([flag, value])
                                logger.debug(f"SCP: Added option pair: {flag} {value}")
                    
                    logger.debug(f"SCP: Final ssh_extra_opts: {ssh_extra_opts}")
            elif profile.prefer_password:
                # If using password authentication, ensure askpass vars are not set
                base_env.pop('SSH_ASKPASS', None)
                base_env.pop('SSH_ASKPASS_REQUIRE', None)
                logger.debug("SCP Download: Using password auth - removed askpass environment")

            dialog = Adw.Window()
            dialog.set_transient_for(self.window)
            dialog.set_modal(True)
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
                # only picks the remote path; listing still uses list_remote_files.
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

            def _load_remote():
                directory = remote_row.get_text().strip() or '.'
                status_label.set_text(_('Loading…'))
                refresh_button.set_sensitive(False)
                list_box.set_sensitive(False)
                download_button.set_sensitive(False)

                def _worker():
                    # If using password authentication, strip askpass environment
                    # (askpass is only for passphrases, not passwords)
                    env_for_list = base_env.copy()
                    if session_password:
                        env_for_list.pop('SSH_ASKPASS', None)
                        env_for_list.pop('SSH_ASKPASS_REQUIRE', None)
                        logger.debug("SCP Download: Using password - removed askpass from environment")
                    
                    # SSH_ASKPASS will handle passphrase retrieval from storage or GUI dialog if needed
                    files, error_message = list_remote_files(
                        host_value,
                        username,
                        directory,
                        port=port,
                        password=session_password,
                        known_hosts_path=known_hosts_path,
                        extra_ssh_opts=ssh_extra_opts,
                        use_publickey=use_publickey_with_password,
                        inherit_env=env_for_list,
                        saved_passphrase=None,  # Let askpass handle retrieval/prompting
                        keyfile=profile.keyfile_expanded if profile.keyfile_ok else None,
                        key_mode=profile.key_mode,
                    )

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
            target = f"{connection.username}@{host_value}" if getattr(connection, 'username', '') else host_value

            if direction == 'upload':
                title_text = _('Upload files (scp)')
                subtitle_text = _('Uploading to {target}:{path}').format(target=target, path=destination)
                info_text = _('We will use scp to upload file(s) to the selected server.')
                start_message = _('Starting upload…')
                success_message = _('Upload finished successfully.')
                failure_message = _('Upload failed. See output above.')
                result_heading_ok = _('Upload complete')
                result_heading_fail = _('Upload failed')
                result_body_ok = _('Files uploaded to {target}:{path}').format(target=target, path=destination)
            elif direction == 'download':
                title_text = _('Download files (scp)')
                subtitle_text = _('Downloading from {target}').format(target=target)
                info_text = _('We will use scp to download file(s) from the selected server into {dest}.').format(dest=destination)
                start_message = _('Starting download…')
                success_message = _('Download finished successfully.')
                failure_message = _('Download failed. See output above.')
                result_heading_ok = _('Download complete')
                result_heading_fail = _('Download failed')
                result_body_ok = _('Files downloaded to {dest}').format(dest=destination)
            else:
                raise ValueError(f'Unsupported scp direction: {direction}')

            dlg = Adw.Window()
            dlg.set_transient_for(self.window)
            dlg.set_modal(True)
            try:
                dlg.set_title(title_text)
            except Exception:
                pass
            try:
                dlg.set_default_size(920, 520)
            except Exception:
                pass

            header = Adw.HeaderBar()
            title_widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(label=title_text)
            title_label.set_halign(Gtk.Align.START)
            subtitle_label = Gtk.Label(label=subtitle_text)
            subtitle_label.set_halign(Gtk.Align.START)
            try:
                title_label.add_css_class('title-2')
                subtitle_label.add_css_class('dim-label')
            except Exception:
                pass
            title_widget.append(title_label)
            title_widget.append(subtitle_label)
            header.set_title_widget(title_widget)

            cancel_btn = Gtk.Button(label=_('Cancel'))
            try:
                cancel_btn.add_css_class('flat')
            except Exception:
                pass
            header.pack_start(cancel_btn)

            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            try:
                content_box.set_margin_top(12)
                content_box.set_margin_bottom(12)
                content_box.set_margin_start(6)
                content_box.set_margin_end(6)
            except Exception:
                pass

            info_lbl = Gtk.Label(label=info_text)
            info_lbl.set_halign(Gtk.Align.START)
            try:
                info_lbl.add_css_class('dim-label')
                info_lbl.set_wrap(True)
            except Exception:
                pass
            content_box.append(info_lbl)

            term_widget = TerminalWidget(connection, self.window.config, self.window.connection_manager)
            try:
                term_widget._set_connecting_overlay_visible(False)
                setattr(term_widget, '_suppress_disconnect_banner', True)
                term_widget._set_disconnected_banner_visible(False)
            except Exception:
                pass
            term_widget.set_hexpand(True)
            term_widget.set_vexpand(True)
            content_box.append(term_widget)

            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root_box.append(header)
            root_box.append(content_box)
            dlg.set_content(root_box)

            def _on_cancel(btn):
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

                try:
                    if hasattr(term_widget, 'disconnect'):
                        term_widget.disconnect()
                except Exception:
                    pass
                dlg.close()

            cancel_btn.connect('clicked', _on_cancel)

            argv = self._build_scp_argv(
                connection,
                sources,
                destination,
                direction=direction,
                known_hosts_path=self.window.connection_manager.known_hosts_path,
            )

            env = os.environ.copy()
            # Apply the auth env resolved by _build_scp_argv (askpass for a saved
            # passphrase, or stripped for the sshpass / interactive cases). Key
            # preload is handled inside _build_scp_argv.
            from .scp_utils import _apply_native_auth_env
            _scp_auth = getattr(self, '_scp_auth', None)
            if _scp_auth is not None:
                _apply_native_auth_env(env, _scp_auth)
                self._scp_auth = None
                logger.debug(
                    "SCP: applied resolved auth env (askpass=%s, sshpass=%s)",
                    _scp_auth.use_askpass, _scp_auth.use_sshpass,
                )
                # Key-based stored password: type once on this VTE (no sshpass).
                if (_scp_auth.password and not _scp_auth.use_sshpass
                        and hasattr(term_widget, 'arm_password_pty_autofill')):
                    term_widget.arm_password_pty_autofill(_scp_auth.password)

            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"

            envv = [f"{k}={v}" for k, v in env.items()]
            logger.debug(f"SCP: Final environment variables: SSH_ASKPASS={env.get('SSH_ASKPASS', 'NOT_SET')}, SSH_ASKPASS_REQUIRE={env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET')}")
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
                        term_widget.backend.feed(("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8'))
                    elif hasattr(term_widget, 'vte') and term_widget.vte:
                        term_widget.vte.feed(("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8'))
                except Exception:
                    pass

            def _spawn_scp(spawn_argv):
                cmdline = ' '.join([GLib.shell_quote(a) for a in spawn_argv])
                logger.debug(f"SCP: Command line: {cmdline}")
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
                        None
                    )
                try:
                    term_widget._install_pty_autofill()
                except Exception:
                    logger.debug("SCP: could not arm PTY auto-fill", exc_info=True)

            def _scrape_terminal_text():
                try:
                    backend = getattr(term_widget, 'backend', None)
                    if backend and hasattr(backend, 'get_content'):
                        content = backend.get_content()
                        if content:
                            return content
                    if hasattr(term_widget, 'vte') and term_widget.vte:
                        content_result = term_widget.vte.get_text_range(
                            0, 0, -1, -1, lambda *args: True
                        )
                        return content_result[0] if content_result else None
                except Exception as exc:
                    logger.debug(f"SCP: Failed to scrape terminal output: {exc}")
                return None

            # Tracks whether we have already retried using the legacy SCP
            # protocol (-O), so the fallback happens at most once.
            scp_legacy_attempted = {'done': False}

            def _present_result_dialog(failure_body=None):
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

                msg = Adw.MessageDialog(
                    transient_for=dlg,
                    modal=True,
                    heading=result_heading_ok if failure_body is None else result_heading_fail,
                    body=(result_body_ok if failure_body is None else failure_body),
                )
                msg.add_response('ok', _('OK'))
                msg.set_default_response('ok')
                msg.set_close_response('ok')
                msg.present()
                return False

            def _on_scp_exited(widget, status):
                exit_code = None
                try:
                    if os.WIFEXITED(status):
                        exit_code = os.WEXITSTATUS(status)
                    else:
                        exit_code = status if 0 <= int(status) < 256 else ((int(status) >> 8) & 0xFF)
                except Exception:
                    try:
                        exit_code = int(status)
                    except Exception:
                        exit_code = status
                ok = (exit_code == 0)
                if ok:
                    _feed_colored_line(success_message, 'green')
                    GLib.idle_add(_present_result_dialog)
                    return

                # Failure: detect a missing/unavailable remote SFTP server.
                # OpenSSH 9+ scp uses the SFTP protocol by default, so retry
                # once with the legacy protocol (-O), which does not need it.
                friendly = classify_sftp_error(_scrape_terminal_text())
                if friendly and not scp_legacy_attempted['done']:
                    scp_legacy_attempted['done'] = True
                    _feed_colored_line(_('Retrying with legacy SCP protocol (-O)…'), 'yellow')
                    try:
                        legacy_argv = self._build_scp_argv(
                            connection,
                            sources,
                            destination,
                            direction=direction,
                            known_hosts_path=self.window.connection_manager.known_hosts_path,
                            legacy=True,
                        )
                        # Discard askpass env repopulated by the rebuild; the
                        # original env (env_dict) is reused for the retry.
                        self._scp_askpass_env = {}
                        _spawn_scp(legacy_argv)
                        return
                    except Exception as exc:
                        logger.error(f'SCP: Failed to retry with legacy protocol: {exc}')

                _feed_colored_line(failure_message, 'red')
                failure_body = friendly or _('scp exited with an error. Please review the log output.')
                GLib.idle_add(lambda: _present_result_dialog(failure_body))

            _feed_colored_line(start_message, 'yellow')

            try:
                if hasattr(term_widget, 'backend') and term_widget.backend:
                    term_widget.backend.connect_child_exited(_on_scp_exited)
                elif hasattr(term_widget, 'vte') and term_widget.vte:
                    term_widget.vte.connect('child-exited', _on_scp_exited)
            except Exception:
                pass

            try:
                _spawn_scp(argv)
            except Exception as e:
                logger.error(f'Failed to spawn scp in TerminalWidget: {e}')
                dlg.close()
                return

            dlg.present()
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
        # askpass for a saved passphrase, sshpass for a saved password, or bare TTY
        # prompts when nothing is saved. Stash it for _show_scp_terminal_window to
        # apply to the spawn environment.
        from .ssh_connection_builder import resolve_native_auth
        from .ssh_password_exec import wrap_argv_with_sshpass
        auth = resolve_native_auth(
            connection,
            getattr(self.window, 'connection_manager', None),
            getattr(self.window, 'config', None),
        )
        self._scp_auth = auth
        logger.debug(
            "SCP: auth resolved (askpass=%s, sshpass=%s)",
            auth.use_askpass, auth.use_sshpass,
        )

        try:
            # Downloads always recurse (`scp -r` is harmless on a regular file),
            # mirroring download_file() so this terminal path can't reproduce
            # issue #1002. Uploads recurse when a local source is a directory
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

        if auth.use_sshpass and auth.password:
            argv, _sshpass_cleanup = wrap_argv_with_sshpass(argv, auth.password)
            import atexit
            atexit.register(_sshpass_cleanup)

        for path in transfer_sources:
            argv.append(path)
        argv.append(transfer_destination)
        return argv
