"""Config-related dialogs/windows for MainWindow.

Extracted verbatim from window.py as a mixin (matching WindowActions and the
other Window*Mixin modules) to shrink the window.py god-object. MainWindow
inherits this; methods keep their signatures and `self.` state access, so this
is a pure code move with no behavior change.

Covers the known-hosts editor launcher, the preferences window launcher, and
the config export / import flow (including the import-mode prompt and the
import itself). The generic `_error_dialog` / `_info_dialog` helpers these call
stay in window.py and resolve via `self`.
"""

import logging
import os
from datetime import datetime

from gi.repository import Adw, Gio, GLib, Gtk
from gettext import gettext as _

from .platform_utils import get_config_dir
from .preferences import PreferencesWindow

logger = logging.getLogger(__name__)


class WindowConfigDialogsMixin:
    """Known-hosts editor, preferences, and config export/import dialogs."""

    def show_known_hosts_editor(self):
        """Show known hosts editor window"""
        logger.info("Show known hosts editor window")
        try:
            from .known_hosts_editor import KnownHostsEditorWindow
            editor = KnownHostsEditorWindow(self, self.connection_manager)
            editor.present()
        except Exception as e:
            logger.error(f"Failed to open known hosts editor: {e}")

    def show_preferences(self):
        """Show preferences dialog"""
        logger.info("Show preferences dialog")
        existing = getattr(self, '_preferences_window', None)
        if existing is not None:
            try:
                existing.present()
                return
            except Exception:
                self._preferences_window = None
        try:
            preferences_window = PreferencesWindow(self, self.config)
            self._preferences_window = preferences_window
            preferences_window.connect(
                'close-request',
                lambda _w: (setattr(self, '_preferences_window', None), False)[1],
            )
            preferences_window.present()
        except Exception as e:
            logger.error(f"Failed to show preferences dialog: {e}")

    def _simple_dialog(self, heading, body):
        d = Adw.MessageDialog(transient_for=self, modal=True, heading=heading, body=body)
        d.add_response('ok', _('OK'))
        d.present()

    def show_export_dialog(self):
        """Show the backup export flow: pick connections + encryption, then save a .spbk."""
        logger.info("Show export backup dialog")
        try:
            self._show_export_options_dialog()
        except Exception as e:
            logger.error(f"Failed to show export dialog: {e}")

    def _show_export_options_dialog(self, prefill_ids=None, encrypt_default=True,
                                    option_defaults=None, error=None):
        """Select backup categories, scoped connections, and encryption."""
        try:
            connections = list(self.connection_manager.get_connections()) \
                if self.connection_manager else []
        except Exception:
            connections = []
        from .backup_manager import BackupManager
        option_defaults = BackupManager.normalize_backup_options(option_defaults)

        dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Export Backup"),
            body=_("Choose what to include in this backup. Saved secrets and private keys "
                   "are included only for the selected connections."))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(8); box.set_margin_end(8)
        box.set_margin_top(8); box.set_margin_bottom(8)

        # A validation message is shown INSIDE this dialog (not a separate alert, which would
        # get hidden behind this window when we re-open it).
        if error:
            err_label = Gtk.Label(label=error, xalign=0, wrap=True)
            try:
                err_label.add_css_class('error')
            except Exception:
                pass
            box.append(err_label)

        category_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        category_label = Gtk.Label(label=_("Include"), xalign=0)
        category_label.add_css_class('heading')
        category_box.append(category_label)

        def switch_row(label, active=False):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            text = Gtk.Label(label=label, xalign=0, hexpand=True)
            switch = Gtk.Switch(active=bool(active))
            switch.set_valign(Gtk.Align.CENTER)
            row.append(text); row.append(switch)
            return row, switch

        app_row, app_settings_check = switch_row(
            _("App settings and groups"), option_defaults.get('app_settings', False))
        ssh_row, ssh_config_check = switch_row(
            _("SSH config entries"), option_defaults.get('ssh_config', False))
        known_row, known_hosts_check = switch_row(
            _("Known hosts"), option_defaults.get('known_hosts', False))
        secrets_row, secrets_check = switch_row(
            _("Saved passwords, sudo passwords, and key passphrases"),
            option_defaults.get('secrets', False))
        keys_row, private_keys_check = switch_row(
            _("Private key files"), option_defaults.get('private_keys', False))
        option_checks = {
            'app_settings': app_settings_check,
            'ssh_config': ssh_config_check,
            'known_hosts': known_hosts_check,
            'secrets': secrets_check,
            'private_keys': private_keys_check,
        }
        for row in (app_row, ssh_row, known_row, secrets_row, keys_row):
            category_box.append(row)
        box.append(category_box)

        connection_label = Gtk.Label(
            label=_("Connections for saved secrets and private keys"),
            xalign=0)
        connection_label.add_css_class('heading')
        box.append(connection_label)

        select_all_row, select_all = switch_row(_("Select all connections"), False)
        box.append(select_all_row)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class('boxed-list')
        checks = []
        prefill = set(prefill_ids or [])
        for conn in connections:
            try:
                label = getattr(conn, 'nickname', '') or conn.get_effective_host() or '?'
                key = getattr(conn, 'nickname', '') or label
            except Exception:
                label, key = '?', '?'
            switch_box, cb = switch_row(label, key in prefill)
            row = Gtk.ListBoxRow(); row.set_child(switch_box); listbox.append(row)
            checks.append((cb, conn, key))
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(180)
        scrolled.set_child(listbox)
        box.append(scrolled)

        def on_select_all(btn):
            for cb, _c, _k in checks:
                cb.set_active(btn.get_active())
        select_all.connect('notify::active', on_select_all)

        def sync_connection_controls(*_a):
            needs_connections = secrets_check.get_active() or private_keys_check.get_active()
            select_all.set_sensitive(needs_connections)
            listbox.set_sensitive(needs_connections)
            connection_label.set_sensitive(needs_connections)
        secrets_check.connect('notify::active', sync_connection_controls)
        private_keys_check.connect('notify::active', sync_connection_controls)
        sync_connection_controls()

        enc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        enc_label = Gtk.Label(label=_("Encrypt with a passphrase"), xalign=0, hexpand=True)
        enc_switch = Gtk.Switch(active=bool(encrypt_default))
        enc_switch.set_valign(Gtk.Align.CENTER)
        enc_row.append(enc_label); enc_row.append(enc_switch)
        enc_row.set_margin_top(6)
        box.append(enc_row)

        pw = Gtk.PasswordEntry(show_peek_icon=True)
        pw.set_property('placeholder-text', _("Passphrase"))
        box.append(pw)
        caption = Gtk.Label(label=_("Without a passphrase, secrets are written in plain text."))
        caption.set_xalign(0); caption.set_wrap(True)
        for css in ('dim-label', 'caption'):
            try: caption.add_css_class(css)
            except Exception: pass
        box.append(caption)

        def sync_pw(*_a):
            on = enc_switch.get_active()
            pw.set_visible(on)
            caption.set_visible(not on)
        enc_switch.connect('notify::active', sync_pw)
        sync_pw()

        dialog.set_extra_child(box)
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('next', _('Continue'))
        dialog.set_response_appearance('next', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('next')
        dialog.set_close_response('cancel')

        def on_response(dlg, resp):
            if resp != 'next':
                return
            selected = [conn for cb, conn, _k in checks if cb.get_active()]
            sel_ids = [k for cb, _c, k in checks if cb.get_active()]
            options = {
                key: check.get_active()
                for key, check in option_checks.items()
            }
            if not any(options.values()):
                # Re-open with the selection preserved + the error shown in-dialog.
                GLib.idle_add(lambda: (self._show_export_options_dialog(
                    sel_ids, enc_switch.get_active(), options,
                    error=_("Choose at least one item to include in the backup.")), False)[1])
                return
            if enc_switch.get_active():
                passphrase = pw.get_text() or ''
                if not passphrase:
                    GLib.idle_add(lambda: (self._show_export_options_dialog(
                        sel_ids, True, options,
                        error=_("Enter a passphrase, or turn off encryption.")), False)[1])
                    return
                self._choose_export_path(selected, passphrase, options)
            else:
                if options.get('secrets') or options.get('private_keys'):
                    self._confirm_plaintext_then_export(selected, sel_ids, options)
                else:
                    self._choose_export_path(selected, None, options)

        dialog.connect('response', on_response)
        dialog.present()

    def _confirm_plaintext_then_export(self, connections, sel_ids, options):
        sensitive_items = []
        if options.get('secrets'):
            sensitive_items.append(_("saved passwords and passphrases"))
        if options.get('private_keys'):
            sensitive_items.append(_("private keys"))
        sensitive_text = _(" and ").join(sensitive_items) if sensitive_items else _("backup data")
        warn = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Export without encryption?"),
            body=_("{} will be written in PLAIN TEXT and readable by anyone with the "
                   "file. Continue?").format(sensitive_text))
        warn.add_response('back', _('Go Back'))
        warn.add_response('plain', _('Export Unencrypted'))
        warn.set_response_appearance('plain', Adw.ResponseAppearance.DESTRUCTIVE)
        warn.set_close_response('back')

        def on_warn(dlg, resp):
            if resp == 'plain':
                self._choose_export_path(connections, None, options)
            else:
                GLib.idle_add(lambda: (self._show_export_options_dialog(
                    sel_ids, False, options), False)[1])
        warn.connect('response', on_warn)
        warn.present()

    def _choose_export_path(self, connections, passphrase, options):
        from .backup_manager import BackupManager
        file_dialog = Gtk.FileDialog()
        file_dialog.set_title(_("Export Backup"))
        file_dialog.set_initial_name(
            f"sshpilot_backup_{datetime.now().strftime('%Y%m%d')}.spbk")
        spbk_filter = Gtk.FileFilter()
        spbk_filter.set_name(_("sshPilot backup (*.spbk)"))
        spbk_filter.add_pattern("*.spbk")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(spbk_filter)
        file_dialog.set_filters(filters)
        file_dialog.set_default_filter(spbk_filter)
        try:
            docs_path = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
            if docs_path:
                file_dialog.set_initial_folder(Gio.File.new_for_path(docs_path))
        except Exception:
            pass

        def on_save_response(dialog, result):
            try:
                file = dialog.save_finish(result)
            except GLib.Error as e:
                if getattr(e, 'code', None) == 2:
                    logger.info("Export cancelled by user")
                else:
                    logger.error(f"Export failed: {e}")
                    self._simple_dialog(_("Export Failed"), str(e))
                return
            if not file:
                return
            export_path = file.get_path()
            if not export_path.endswith('.spbk'):
                export_path += '.spbk'
            backup_mgr = BackupManager(self.config, self.connection_manager)
            success, error = backup_mgr.export_backup(
                export_path, connections=connections, passphrase=passphrase,
                options=options)
            if success:
                counts = getattr(backup_mgr, 'last_export_counts', {})
                self._simple_dialog(
                    _("Export Successful"),
                    _("Backup saved to:\n{}\n\n{} credential(s) and {} private key(s) "
                      "included; encryption: {}.").format(
                        export_path, counts.get('credentials', 0),
                        counts.get('private_keys', 0),
                        _("on") if passphrase else _("off")))
            else:
                self._simple_dialog(_("Export Failed"), error or _("Unknown error"))

        file_dialog.save(self, None, on_save_response)

    def show_import_dialog(self):
        """Show import configuration dialog"""
        logger.info("Show import configuration dialog")
        try:
            # Create file chooser dialog for opening
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title(_("Import Configuration"))
            
            # Backups (.spbk) and legacy JSON configs.
            filter_backup = Gtk.FileFilter()
            filter_backup.set_name(_("sshPilot backups & configs"))
            filter_backup.add_pattern("*.spbk")
            filter_backup.add_pattern("*.json")

            filter_all = Gtk.FileFilter()
            filter_all.set_name(_("All files"))
            filter_all.add_pattern("*")

            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(filter_backup)
            filters.append(filter_all)
            file_dialog.set_filters(filters)
            file_dialog.set_default_filter(filter_backup)
            
            # Set default folder to user's documents or home
            try:
                docs_path = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
                if docs_path:
                    file_dialog.set_initial_folder(Gio.File.new_for_path(docs_path))
            except Exception:
                pass
            
            def on_open_response(dialog, result):
                try:
                    file = dialog.open_finish(result)
                    if file:
                        import_path = file.get_path()
                        self._begin_import(import_path)

                except GLib.Error as e:
                    # Check if user cancelled the dialog (error code 2 = GTK_DIALOG_ERROR_DISMISSED)
                    if e.code == 2:
                        logger.info("Import cancelled by user")
                    else:
                        logger.error(f"Import file selection failed: {e}")
                except Exception as e:
                    logger.error(f"Import file selection failed: {e}")
            
            file_dialog.open(self, None, on_open_response)
            
        except Exception as e:
            logger.error(f"Failed to show import dialog: {e}")

    def _begin_import(self, import_path: str):
        """Route an import by format: .spbk (decrypt as needed) vs legacy JSON."""
        try:
            from .backup_archive import is_spbk
            if is_spbk(import_path):
                self._import_spbk(import_path)
            else:
                self._show_import_mode_dialog(import_path)
        except Exception as e:
            logger.error(f"Failed to start import: {e}")
            self._simple_dialog(_("Import Failed"), str(e))

    def _import_spbk(self, import_path: str):
        """Decrypt (if needed) a .spbk, then proceed to the import-mode dialog with its manifest."""
        from .backup_archive import read_spbk, spbk_is_encrypted, SpbkError
        try:
            if spbk_is_encrypted(import_path):
                self._prompt_spbk_passphrase(import_path)
                return
            manifest = read_spbk(import_path, None)
            self._show_import_mode_dialog(import_path, manifest=manifest)
        except SpbkError as e:
            self._simple_dialog(_("Import Failed"), str(e))
        except Exception as e:
            logger.error(f"Failed to read backup: {e}")
            self._simple_dialog(_("Import Failed"), str(e))

    def _prompt_spbk_passphrase(self, import_path: str, error: str = None):
        """Prompt for the backup passphrase; retry on a wrong passphrase."""
        from .backup_archive import read_spbk, SpbkPassphraseError, SpbkError
        dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Encrypted Backup"),
            body=error or _("Enter the passphrase used when this backup was created."))
        entry = Gtk.PasswordEntry(show_peek_icon=True)
        entry.set_property('activates-default', True)
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('ok', _('Unlock'))
        dialog.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('ok')
        dialog.set_close_response('cancel')

        def on_response(dlg, resp):
            if resp != 'ok':
                return
            passphrase = entry.get_text() or ''
            try:
                manifest = read_spbk(import_path, passphrase)
            except SpbkPassphraseError:
                GLib.idle_add(lambda: (self._prompt_spbk_passphrase(
                    import_path, _("Wrong passphrase — try again.")), False)[1])
                return
            except SpbkError as e:
                self._simple_dialog(_("Import Failed"), str(e))
                return
            self._show_import_mode_dialog(import_path, manifest=manifest)

        dialog.connect('response', on_response)
        dialog.present()
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])

    def _show_import_mode_dialog(self, import_path: str, manifest=None):
        """Show dialog to select import mode (replace or merge)"""
        try:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Import Configuration"),
                body=_("Choose how to import the configuration:")
            )
            
            # Create content box with radio buttons
            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content_box.set_margin_start(20)
            content_box.set_margin_end(20)
            content_box.set_margin_top(20)
            content_box.set_margin_bottom(20)
            
            # Replace mode radio
            replace_radio = Gtk.CheckButton()
            replace_radio.set_label(_("Replace current configuration"))
            replace_radio.set_active(True)
            content_box.append(replace_radio)
            
            replace_desc = Gtk.Label()
            replace_desc.set_markup(_("<small>All current settings will be replaced with imported configuration</small>"))
            replace_desc.set_xalign(0)
            replace_desc.set_margin_start(24)
            replace_desc.add_css_class('dim-label')
            content_box.append(replace_desc)
            
            # Merge mode radio
            merge_radio = Gtk.CheckButton()
            merge_radio.set_label(_("Merge with current configuration"))
            merge_radio.set_group(replace_radio)
            content_box.append(merge_radio)
            
            merge_desc = Gtk.Label()
            merge_desc.set_markup(_("<small>Add new connections and groups, preserve existing ones</small>"))
            merge_desc.set_xalign(0)
            merge_desc.set_margin_start(24)
            merge_desc.add_css_class('dim-label')
            content_box.append(merge_desc)

            restore_checks = {}
            if manifest is not None:
                from .backup_manager import BackupManager, BACKUP_OPTION_KEYS
                backup_mgr = BackupManager(self.config, self.connection_manager)
                all_requested = {key: True for key in BACKUP_OPTION_KEYS}
                included = backup_mgr._restore_options_for_manifest(
                    manifest, restore_options=all_requested)

                restore_label = Gtk.Label(label=_("Restore"), xalign=0)
                restore_label.add_css_class('heading')
                restore_label.set_margin_top(12)
                content_box.append(restore_label)

                labels = {
                    'app_settings': _("App settings and groups"),
                    'ssh_config': _("SSH config entries"),
                    'known_hosts': _("Known hosts"),
                    'secrets': _("Saved passwords, sudo passwords, and key passphrases"),
                    'private_keys': _("Private key files"),
                }
                for key in BACKUP_OPTION_KEYS:
                    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                    label = Gtk.Label(label=labels[key], xalign=0, hexpand=True)
                    check = Gtk.Switch(active=included.get(key, False))
                    check.set_valign(Gtk.Align.CENTER)
                    row.append(label); row.append(check)
                    row.set_sensitive(included.get(key, False))
                    if not included.get(key, False):
                        row.set_tooltip_text(_("This backup does not include this item."))
                    restore_checks[key] = check
                    content_box.append(row)
            
            # Warning label
            from sshpilot import icon_utils
            warning_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            warning_box.set_margin_top(12)
            warning_icon = icon_utils.new_image_from_icon_name('dialog-warning-symbolic')
            warning_box.append(warning_icon)
            warning_label = Gtk.Label()
            backup_dir = os.path.join(get_config_dir(), 'backups')
            warning_label.set_markup(_("<small>A backup will be created automatically before importing.\nBackup location: {}</small>").format(backup_dir))
            warning_label.set_wrap(True)
            warning_label.set_xalign(0)
            warning_label.add_css_class('dim-label')
            warning_box.append(warning_label)
            content_box.append(warning_box)
            
            dialog.set_extra_child(content_box)
            
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('import', _('Import'))
            dialog.set_response_appearance('import', Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response('import')
            dialog.set_close_response('cancel')
            
            def on_response(dialog, response):
                if response == 'import':
                    mode = 'replace' if replace_radio.get_active() else 'merge'
                    if manifest is not None:
                        restore_options = {
                            key: check.get_active()
                            for key, check in restore_checks.items()
                        }
                        if not any(restore_options.values()):
                            self._simple_dialog(
                                _("Nothing selected"),
                                _("Choose at least one item to restore from this backup."))
                            dialog.destroy()
                            GLib.idle_add(lambda: (self._show_import_mode_dialog(
                                import_path, manifest=manifest), False)[1])
                            return
                        self._perform_spbk_import(manifest, mode, restore_options)
                    else:
                        self._perform_import(import_path, mode)
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
        except Exception as e:
            logger.error(f"Failed to show import mode dialog: {e}")

    def _perform_spbk_import(self, manifest, mode: str, restore_options=None):
        """Apply a decrypted .spbk manifest: config (replace/merge) + restore credentials.

        If the manifest carries credentials and the selected secret backend is a **locked
        session vault** (e.g. Bitwarden), unlock it first so the restores don't silently fail.
        Proceed-regardless: if the user cancels the unlock we still import and report how many
        credentials could be restored."""
        restore_options = restore_options or {}
        total = (
            len([c for c in (manifest.get('credentials') or [])
                 if c.get('secret') is not None])
            if restore_options.get('secrets', True) else 0
        )
        total_keys = (
            len(manifest.get('private_keys') or [])
            if restore_options.get('private_keys', False) else 0
        )

        def do_apply(*_args):
            try:
                from .backup_manager import BackupManager
                backup_mgr = BackupManager(self.config, self.connection_manager)
                success, error, restored, restored_keys = backup_mgr.apply_imported_manifest(
                    manifest, mode=mode, create_backup=True, restore_options=restore_options)
            except Exception as e:
                logger.error(f"Backup import failed: {e}")
                self._simple_dialog(_("Import Failed"), str(e))
                return
            if not success:
                self._simple_dialog(_("Import Failed"), error or _("Unknown error"))
                return
            skipped_keys = getattr(backup_mgr, 'last_import_skipped_keys', 0)
            self._show_import_success(restored, total, restored_keys, total_keys, skipped_keys)

        if total:
            try:
                from .secret_storage import get_secret_manager
                if get_secret_manager().selected_needs_unlock():
                    from .secret_unlock_dialog import prompt_unlock
                    prompt_unlock(self, on_done=do_apply)   # do_apply runs once unlock resolves
                    return
            except Exception:
                logger.debug("Pre-restore unlock check failed", exc_info=True)
        do_apply()

    def _show_import_success(self, restored: int, total: int,
                             restored_keys: int = 0, total_keys: int = 0,
                             skipped_keys: int = 0):
        # Keys that already existed were left untouched by design (never overwritten), so they
        # are NOT counted as failures. Genuine key failures are the remainder.
        failed_keys = max(0, total_keys - restored_keys - skipped_keys)
        lines = []
        if total == 0 and total_keys == 0:
            lines.append(_("Backup imported successfully."))
        else:
            all_ok = (restored >= total) and (failed_keys == 0)
            lines.append(_("Backup imported successfully.") if all_ok
                         else _("Backup imported, with some items skipped."))
            done = []
            if restored:
                done.append(_("{} credential(s)").format(restored))
            if restored_keys:
                done.append(_("{} private key(s)").format(restored_keys))
            if done:
                lines.append(_("Restored {}.").format(_(", ").join(done)))
            if skipped_keys:
                lines.append(_("{} private key(s) already existed and were left untouched — "
                               "sshPilot never overwrites a private key.").format(skipped_keys))
            if restored < total:
                lines.append(_("{} of {} credential(s) could not be restored — the selected "
                               "secret backend may be locked or unavailable.").format(
                                   restored, total))
            if failed_keys:
                lines.append(_("{} private key(s) could not be written (the target path may "
                               "not be writable).").format(failed_keys))
        lines.append(_("It is recommended to restart SSH Pilot for all changes to take effect."))
        body = "\n\n".join(lines)

        success_dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Import Successful"), body=body)
        success_dialog.add_response('ok', _('OK'))
        success_dialog.add_response('restart', _('Restart Now'))
        success_dialog.set_response_appearance('restart', Adw.ResponseAppearance.SUGGESTED)

        def on_success_response(dialog, response):
            if response == 'restart':
                try:
                    self.config.config_data = self.config.load_json_config()
                    if self.connection_manager:
                        self.connection_manager.load_ssh_config()
                    if self.group_manager:
                        self.group_manager._load_groups()
                    self.rebuild_connection_list()
                    self.toast_overlay.add_toast(Adw.Toast.new(_("Configuration reloaded")))
                except Exception as e:
                    logger.error(f"Failed to reload configuration: {e}")
            dialog.destroy()

        success_dialog.connect('response', on_success_response)
        success_dialog.present()

    def _perform_import(self, import_path: str, mode: str):
        """Perform the actual import operation"""
        try:
            from .backup_manager import BackupManager
            
            backup_mgr = BackupManager(self.config, self.connection_manager)
            success, error = backup_mgr.import_configuration(import_path, mode=mode, create_backup=True)
            
            if success:
                # Show success dialog with restart suggestion
                success_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Import Successful"),
                    body=_("Configuration imported successfully.\n\nIt is recommended to restart SSH Pilot for all changes to take effect.")
                )
                success_dialog.add_response('ok', _('OK'))
                success_dialog.add_response('restart', _('Restart Now'))
                success_dialog.set_response_appearance('restart', Adw.ResponseAppearance.SUGGESTED)
                
                def on_success_response(dialog, response):
                    if response == 'restart':
                        # Reload the connection list and config
                        try:
                            self.config.config_data = self.config.load_json_config()
                            if self.connection_manager:
                                self.connection_manager.load_ssh_config()
                            # Reload group manager to pick up imported groups and colors
                            if self.group_manager:
                                self.group_manager._load_groups()
                            self.rebuild_connection_list()
                            
                            # Show confirmation
                            self.toast_overlay.add_toast(Adw.Toast.new(_("Configuration reloaded")))
                        except Exception as e:
                            logger.error(f"Failed to reload configuration: {e}")
                    dialog.destroy()
                
                success_dialog.connect('response', on_success_response)
                success_dialog.present()
            else:
                # Show error dialog
                error_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Import Failed"),
                    body=_("Failed to import configuration:\n{}").format(error or "Unknown error")
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()
                
        except Exception as e:
            logger.error(f"Import failed: {e}")
            error_dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Import Failed"),
                body=_("An error occurred during import:\n{}").format(str(e))
            )
            error_dialog.add_response('ok', _('OK'))
            error_dialog.present()

