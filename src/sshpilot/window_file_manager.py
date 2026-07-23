"""Manage-files (file manager) flow for MainWindow.

Extracted verbatim from window.py as a mixin (matching WindowActions and the
other Window*Mixin modules) to shrink the window.py god-object. MainWindow
inherits this; methods keep their signatures and `self.` state access, so this
is a pure code move with no behavior change.

Covers the manage-files entry action, the first-run / operation-mode / backup
prompts, the placeholder tab while the backend spawns, and file-manager tab
registration. The synchronous teardown of file-manager embeds stays in
window.py for now (it is shared with tab close/shutdown).
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from gi.repository import Adw, GLib, Gtk, Pango
from gettext import gettext as _

from .platform_utils import get_config_dir, get_ssh_dir
from .plugins.api import Capability
from .plugins.registry import capabilities_for
from .sftp_utils import should_use_in_app_file_manager
from .connection_display import get_connection_alias, get_connection_host
from .file_manager_integration import (
    launch_remote_file_manager,
    create_internal_file_manager_tab,
    has_internal_file_manager,
    has_native_gvfs_support,
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

        try:
            open_externally = bool(self.config.get_setting('file_manager.open_externally', False))
            force_internal = bool(self.config.get_setting('file_manager.force_internal', False))
        except Exception:
            open_externally = force_internal = False
        use_internal = (not open_externally
                        and (force_internal or should_use_in_app_file_manager())
                        and has_internal_file_manager())
        if use_internal:
            self._open_file_manager_with_picker()
        else:
            from .host_picker import show_host_picker
            show_host_picker(self, self.menu_button,
                             self._open_manage_files_for_connection)

    def _open_file_manager_with_picker(self):
        """Open the embedded file manager with no server; the remote pane
        shows the shared host picker until one is chosen."""
        ssh_config = None
        try:
            ssh_config = self.config.get_ssh_config()
        except Exception:
            ssh_config = None
        try:
            widget, controller = create_internal_file_manager_tab(
                user='',
                host='',
                port=None,
                nickname='',
                parent_window=self,
                connection=None,
                connection_manager=self.connection_manager,
                ssh_config=ssh_config,
            )
        except Exception as exc:
            logger.error("File manager with host picker failed: %s", exc, exc_info=True)
            self._show_manage_files_error(_('Remote Host'), str(exc))
            return
        page = self._register_file_manager_tab(widget, controller, None, None)
        if page is not None:
            page.set_title(_('Files'))

            def _on_host_picked(connection):
                name = (getattr(connection, 'nickname', None)
                        or _get_connection_host(connection)
                        or _('Remote Host'))
                page.set_title(_('{name} Files').format(name=name))

            controller._host_picked_callback = _on_host_picked

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
        """Open files for the supplied connection.

        On the very first invocation (per user profile, per machine) where a
        choice between built-in and system file managers actually exists,
        prompt the user once and remember their pick. Subsequent calls go
        straight to the open flow.
        """
        if Capability.FILE_TRANSFER not in capabilities_for(connection):
            logger.debug("Manage Files unavailable: protocol %r has no file transfer",
                         getattr(connection, 'protocol', 'ssh'))
            return
        if self._should_prompt_file_manager_choice():
            self._show_file_manager_first_run_dialog(
                lambda choice: self._continue_open_manage_files_after_choice(connection, choice)
            )
            return
        self._open_manage_files_now_for_connection(connection)

    def _continue_open_manage_files_after_choice(
        self, connection, choice: Optional[str]
    ) -> None:
        """Persist the first-run choice and then open the file manager.

        *choice* is ``None`` when the user cancelled / dismissed the dialog;
        in that case we do nothing — no preference saved, no file manager
        opened, and the next Manage Files click re-prompts.
        """
        if choice is None:
            logger.debug("File manager first-run dialog cancelled; deferring choice")
            return
        self._apply_file_manager_first_run_choice(choice)
        self._open_manage_files_now_for_connection(connection)

    def _should_prompt_file_manager_choice(self) -> bool:
        """Return True if we should ask the user which file manager to use."""
        try:
            already_shown = bool(
                self.config.get_setting('file_manager.first_run_prompt_shown', False)
            )
        except Exception:
            already_shown = False
        if already_shown:
            return False
        # If only the built-in is available (Flatpak, macOS, no GVFS) there's
        # no real choice to make. Silently mark the prompt as shown so we
        # never reach this branch again.
        if not has_internal_file_manager() or not has_native_gvfs_support():
            try:
                self.config.set_setting('file_manager.first_run_prompt_shown', True)
            except Exception as exc:
                logger.debug("Could not mark first-run file manager prompt: %s", exc)
            return False
        return True

    def _apply_file_manager_first_run_choice(self, choice: str) -> None:
        """Persist the user's pick from the first-run dialog."""
        try:
            if choice == 'builtin':
                self.config.set_setting('file_manager.force_internal', True)
            elif choice == 'system':
                self.config.set_setting('file_manager.force_internal', False)
            # Any unknown response (e.g. future close-response) is treated
            # the same as no preference change — but we still mark the
            # prompt as shown so we never ask again.
            self.config.set_setting('file_manager.first_run_prompt_shown', True)
        except Exception as exc:
            logger.error("Failed to persist file manager first-run choice: %s", exc)

    # --- Operation mode first-run dialog ---
    # Fresh installs start in default mode with no chooser and no automatic
    # SSH config backup. Keep the dialog helpers below so the flow can be
    # re-enabled by flipping this flag.
    _OPERATION_MODE_FIRST_RUN_PROMPT_ENABLED = False

    def _should_prompt_operation_mode(self) -> bool:
        """Return True if the operation mode dialog should be shown."""
        if not self._OPERATION_MODE_FIRST_RUN_PROMPT_ENABLED:
            # Mark as shown so a later re-enable does not surprise installs
            # that never saw the dialog (they already run in default mode).
            try:
                if not bool(
                    self.config.get_setting('ssh.operation_mode_prompt_shown', False)
                ):
                    self.config.set_setting('ssh.operation_mode_prompt_shown', True)
            except Exception:
                pass
            return False

        try:
            already_shown = bool(
                self.config.get_setting('ssh.operation_mode_prompt_shown', False)
            )
        except Exception:
            already_shown = False

        if not already_shown:
            # Migrate the old key name used before the rename.
            try:
                old_val = self.config.get_setting('ssh.config_mode_prompt_shown', None)
                if old_val is not None:
                    already_shown = bool(old_val)
                    # Write under new key and purge the old one.
                    self.config.set_setting('ssh.operation_mode_prompt_shown', already_shown)
                    try:
                        ssh_section = self.config.config_data.get('ssh', {})
                        ssh_section.pop('config_mode_prompt_shown', None)
                        self.config.save_json_config()
                    except Exception:
                        pass
            except Exception:
                pass

        if already_shown:
            return False
        if getattr(self, 'isolated_mode', False):
            try:
                self.config.set_setting('ssh.operation_mode_prompt_shown', True)
            except Exception:
                pass
            return False
        return True

    def _prompt_backup_ssh_config(self) -> None:
        """Let the user back up ~/.ssh/config to a location they choose.

        Uses Gtk.FileDialog (portal-backed, so it works inside the Flatpak
        sandbox) to pick the destination, copies the config there, then shows
        an alert confirming the saved path.  Does nothing — and never raises —
        if there is no config to back up.
        """
        src = Path(get_ssh_dir()) / 'config'
        if not src.exists() or src.stat().st_size == 0:
            logger.debug("No SSH config to back up; skipping backup prompt")
            return

        dialog = Gtk.FileDialog.new()
        dialog.set_title(_("Back Up SSH Config"))
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        dialog.set_initial_name(f"ssh_config_backup_{timestamp}.bak")

        def _on_done(dlg, result):
            try:
                gfile = dlg.save_finish(result)
            except GLib.Error:
                return  # user cancelled or portal denied
            if gfile is None:
                return
            dst = gfile.get_path()
            if not dst:
                logger.warning("Backup destination has no local path")
                return
            try:
                shutil.copy2(str(src), dst)
                logger.info("SSH config backed up to %s", dst)
            except Exception as exc:
                logger.error("Could not back up SSH config: %s", exc)
                self._show_backup_result_alert(False, dst)
                return
            self._show_backup_result_alert(True, dst)

        try:
            dialog.save(self, None, _on_done)
        except Exception as exc:
            logger.error("Could not open backup save dialog: %s", exc, exc_info=True)

    def _show_backup_result_alert(self, success: bool, path: str) -> None:
        """Tell the user where the SSH config backup was (or wasn't) saved."""
        if success:
            heading = _("Backup Saved")
            body = _("Your SSH config was backed up to:\n{path}").format(path=path)
        else:
            heading = _("Backup Failed")
            body = _(
                "SSH Pilot could not save the backup to:\n{path}"
            ).format(path=path)

        if hasattr(Adw, 'AlertDialog'):
            alert = Adw.AlertDialog(heading=heading, body=body)
            alert.add_response('ok', _("OK"))
            alert.set_default_response('ok')
            alert.set_close_response('ok')
            alert.present(self)
        else:
            alert = Adw.MessageDialog(
                transient_for=self, modal=True, heading=heading, body=body
            )
            alert.add_response('ok', _("OK"))
            alert.set_default_response('ok')
            alert.set_close_response('ok')
            alert.present()

    def _apply_operation_mode_choice(
        self, choice: Optional[str], copy: bool = False
    ) -> None:
        """Persist the operation mode choice and optionally seed the isolated config.

        When the user picks Isolated the app must restart for the new config
        path to take full effect (ConnectionManager and KeyManager are already
        initialised with the default path).  restart_app() is called after all
        settings are saved so the fresh process picks up the new mode.
        """
        try:
            if choice == 'isolated':
                self.config.set_setting('ssh.use_isolated_config', True)
                self.config.set_setting('ssh.operation_mode_prompt_shown', True)
                if copy:
                    src = Path(get_ssh_dir()) / 'config'
                    dst = Path(get_config_dir()) / 'ssh_config'
                    if src.exists() and not dst.exists():
                        try:
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(str(src), str(dst))
                            logger.info("Seeded isolated SSH config from %s", src)
                        except Exception as exc:
                            logger.warning(
                                "Could not copy SSH config to isolated path: %s", exc
                            )
                from .platform_utils import restart_app
                restart_app()
            elif choice == 'default':
                self.config.set_setting('ssh.use_isolated_config', False)
                self.config.set_setting('ssh.operation_mode_prompt_shown', True)
            else:
                # Skip — mark shown so the dialog never reappears.
                self.config.set_setting('ssh.operation_mode_prompt_shown', True)
        except Exception as exc:
            logger.error("Failed to persist operation mode choice: %s", exc)

    def _show_operation_mode_dialog(self) -> None:
        """One-time dialog asking the user which operation mode to use.

        Follows the GNOME HIG choice-dialog pattern used by
        _show_file_manager_first_run_dialog.  When the user picks Isolated,
        _apply_operation_mode_choice calls restart_app() so the new config
        path is honoured from the very next launch.
        """
        heading = _("Choose Operation Mode")
        body = _(
            "SSH Pilot can use your .ssh/config "
            "or use its own configuration file. "
            
        )

        use_alert = hasattr(Adw, 'AlertDialog')
        if use_alert:
            dialog = Adw.AlertDialog(heading=heading, body=body)
        else:
            dialog = Adw.MessageDialog(
                transient_for=self, modal=True, heading=heading, body=body
            )

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_top(12)
        content_box.set_size_request(460, -1)

        icon = Gtk.Image.new_from_icon_name('shield-full-symbolic')
        icon.set_pixel_size(64)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_margin_bottom(4)
        content_box.append(icon)

        mode_group = Adw.PreferencesGroup()

        default_row = Adw.ActionRow()
        default_row.set_title(_("Default — use ~/.ssh/config"))
        default_row.set_subtitle(_("SSH Pilot shares config with the system SSH client"))
        default_radio = Gtk.CheckButton()
        default_radio.set_valign(Gtk.Align.CENTER)
        default_radio.set_active(True)
        default_row.add_prefix(default_radio)
        default_row.set_activatable_widget(default_radio)
        mode_group.add(default_row)

        isolated_row = Adw.ActionRow()
        isolated_row.set_title(_("Isolated — use a private SSH config"))
        isolated_row.set_subtitle(
            _("SSH Pilot stores its own ssh config file in its directory")
        )
        isolated_radio = Gtk.CheckButton()
        isolated_radio.set_valign(Gtk.Align.CENTER)
        isolated_radio.set_group(default_radio)
        isolated_row.add_prefix(isolated_radio)
        isolated_row.set_activatable_widget(isolated_radio)
        mode_group.add(isolated_row)

        content_box.append(mode_group)

        src_exists = (Path(get_ssh_dir()) / 'config').exists()
        copy_group = Adw.PreferencesGroup()
        copy_row = Adw.ActionRow()
        copy_row.set_title(_("Copy existing ssh config into the isolated profile"))
        copy_row.set_subtitle(_("Your hosts and keys will be available immediately"))
        copy_check = Gtk.CheckButton()
        copy_check.set_valign(Gtk.Align.CENTER)
        copy_check.set_active(True)
        copy_row.add_prefix(copy_check)
        copy_row.set_activatable_widget(copy_check)
        copy_group.add(copy_row)
        copy_group.set_visible(False)
        content_box.append(copy_group)

        # Default mode: offer an explicit, opt-in backup of the existing config.
        # Shown only when there is a config to back up; the actual destination
        # is chosen later via a portal-aware save dialog.
        backup_group = Adw.PreferencesGroup()
        backup_row = Adw.ActionRow()
        backup_row.set_title(_("Back up my existing SSH config first"))
        backup_row.set_subtitle(
            _("You'll choose where to save a copy of ~/.ssh/config")
        )
        backup_check = Gtk.CheckButton()
        backup_check.set_valign(Gtk.Align.CENTER)
        backup_check.set_active(True)
        backup_row.add_prefix(backup_check)
        backup_row.set_activatable_widget(backup_check)
        backup_group.add(backup_row)
        backup_group.set_visible(src_exists)
        content_box.append(backup_group)

        def _sync_option_visibility(*_args):
            isolated = isolated_radio.get_active()
            copy_group.set_visible(isolated and src_exists)
            backup_group.set_visible(not isolated and src_exists)

        default_radio.connect('toggled', _sync_option_visibility)
        isolated_radio.connect('toggled', _sync_option_visibility)
        _sync_option_visibility()

        footer = Gtk.Label(
            label=_("You can change this anytime in Preferences › SSH Settings.")
        )
        footer.set_wrap(True)
        footer.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        footer.set_xalign(0)
        footer.set_halign(Gtk.Align.START)
        footer.add_css_class('caption')
        footer.add_css_class('dim-label')
        content_box.append(footer)

        dialog.set_extra_child(content_box)

        dialog.add_response('skip', _("Skip"))
        dialog.add_response('confirm', _("Confirm"))
        dialog.set_default_response('confirm')
        dialog.set_close_response('skip')
        try:
            dialog.set_response_appearance(
                'confirm', Adw.ResponseAppearance.SUGGESTED
            )
        except Exception:
            pass

        def _on_response(_d, response: str) -> None:
            if response == 'confirm':
                if isolated_radio.get_active():
                    self._apply_operation_mode_choice(
                        'isolated', copy=copy_check.get_active() and src_exists
                    )
                else:
                    self._apply_operation_mode_choice('default')
                    if backup_check.get_active() and src_exists:
                        self._prompt_backup_ssh_config()
            else:
                self._apply_operation_mode_choice(None)

        dialog.connect('response', _on_response)

        if use_alert:
            dialog.present(self)
        else:
            dialog.present()

    def _show_file_manager_first_run_dialog(self, on_choice) -> None:
        """Present the one-time built-in vs system chooser.

        *on_choice* is called with ``'builtin'`` or ``'system'`` after the
        user picks and confirms, or ``None`` if they cancel / dismiss the
        dialog (no preference is saved in that case, so the next Manage
        Files click re-prompts).

        Layout follows the GNOME HIG choice-dialog pattern: heading, a brief
        body, then an Adw.PreferencesGroup of Adw.ActionRow rows each with
        a prefix radio button. The whole row is the radio's activatable
        widget, so clicking anywhere on a row toggles the selection.
        """
        heading = _("Choose your file manager")
        body = _("How should SSH Pilot manage files on remote hs?")

        use_alert = hasattr(Adw, 'AlertDialog')
        if use_alert:
            dialog = Adw.AlertDialog(heading=heading, body=body)
        else:
            dialog = Adw.MessageDialog(
                transient_for=self, modal=True, heading=heading, body=body
            )

        # --- Choice list ---
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content_box.set_margin_top(12)
        # Give the dialog room to breathe; AlertDialog will respect this
        # as a minimum content width.
        content_box.set_size_request(440, -1)

        try:
            # Gtk.Image is for app-chosen icon sizes (pixel-size); Gtk.Picture
            # shows paintables at natural size — see GTK 4 docs for each widget.
            illustration = Gtk.Image.new_from_resource(
                '/io/github/mfat/sshpilot/file-manager-choice.png'
            )
            illustration.set_pixel_size(180)
            illustration.set_halign(Gtk.Align.CENTER)
            illustration.set_vexpand(False)
            content_box.append(illustration)
        except GLib.Error as exc:
            logger.debug(
                "File manager choice dialog illustration unavailable: %s", exc
            )

        group = Adw.PreferencesGroup()

        # Built-in option (recommended → pre-selected).
        builtin_row = Adw.ActionRow()
        builtin_row.set_title(_("Use the built-in, dual-pane File Manager"))
        builtin_row.set_subtitle(
            _("Recommended — opens in a tab inside sshPilot")
        )
        builtin_radio = Gtk.CheckButton()
        builtin_radio.set_valign(Gtk.Align.CENTER)
        builtin_radio.set_active(True)
        builtin_row.add_prefix(builtin_radio)
        builtin_row.set_activatable_widget(builtin_radio)
        group.add(builtin_row)

        # System option.
        system_row = Adw.ActionRow()
        system_row.set_title(_("Use the system file manager"))
        system_row.set_subtitle(
            _("Opens your desktop file manager (e.g. GNOME Files)")
        )
        system_radio = Gtk.CheckButton()
        system_radio.set_valign(Gtk.Align.CENTER)
        # set_group makes this radio share state with the first one — picking
        # one deselects the other.
        system_radio.set_group(builtin_radio)
        system_row.add_prefix(system_radio)
        system_row.set_activatable_widget(system_radio)
        group.add(system_row)

        content_box.append(group)

        # --- Footer hint ---
        footer = Gtk.Label(
            label=_("You can change this anytime in Preferences → File Management.")
        )
        footer.set_wrap(True)
        footer.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        footer.set_xalign(0)
        footer.set_halign(Gtk.Align.START)
        footer.add_css_class("caption")
        footer.add_css_class("dim-label")
        content_box.append(footer)

        dialog.set_extra_child(content_box)

        # --- Response buttons (Cancel / Confirm) ---
        dialog.add_response('cancel', _("Cancel"))
        dialog.add_response('confirm', _("Confirm"))
        dialog.set_default_response('confirm')
        dialog.set_close_response('cancel')
        try:
            dialog.set_response_appearance(
                'confirm', Adw.ResponseAppearance.SUGGESTED
            )
        except Exception:
            pass

        def _on_response(_d, response: str) -> None:
            if response == 'confirm':
                choice = 'builtin' if builtin_radio.get_active() else 'system'
                on_choice(choice)
            else:
                on_choice(None)

        dialog.connect('response', _on_response)

        if use_alert:
            dialog.present(self)
        else:
            dialog.present()

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

        ssh_config = None
        if hasattr(self, 'config') and self.config is not None:
            try:
                ssh_config = self.config.get_ssh_config()
            except Exception as exc:
                logger.debug("Failed to read SSH configuration for file manager: %s", exc)
                ssh_config = None

        open_externally = False
        force_internal = False
        try:
            open_externally = bool(self.config.get_setting('file_manager.open_externally', False))
            force_internal = bool(self.config.get_setting('file_manager.force_internal', False))
        except Exception:
            open_externally = False
            force_internal = False

        use_internal = False
        if force_builtin:
            use_internal = True
        elif not open_externally:
            use_internal = force_internal or should_use_in_app_file_manager()

        placeholder_info = None
        if use_internal and has_internal_file_manager():
            placeholder_info = self._create_file_manager_placeholder_tab(nickname, host_value)

            def _create_embedded_file_manager():
                try:
                    widget, controller = create_internal_file_manager_tab(
                        user=str(username or ''),
                        host=str(host_value or ''),
                        port=effective_port,
                        nickname=str(nickname),
                        parent_window=self,
                        connection=connection,
                        connection_manager=self.connection_manager,
                        ssh_config=ssh_config,
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

            if GLib.idle_add(_create_embedded_file_manager, priority=GLib.PRIORITY_DEFAULT_IDLE):
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
                    ssh_config=ssh_config,
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
            logger.info(f"Started file manager for {nickname}")
            if window is not None:
                self._track_internal_file_manager_window(window)
        else:
            message = error_msg or "Failed to start file manager process"
            logger.error(f"Failed to start file manager process for {nickname}: {message}")
            self._show_manage_files_error(str(nickname), message)

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

        container.append(inner_box)

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

        try:
            widget.set_hexpand(True)
            widget.set_vexpand(True)
        except Exception:  # pragma: no cover - optional sizing
            pass

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
