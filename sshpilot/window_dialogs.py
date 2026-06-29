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

    def _show_export_options_dialog(self, prefill_ids=None, encrypt_default=True):
        """Select which connections' credentials to include + whether to encrypt."""
        try:
            connections = list(self.connection_manager.get_connections()) \
                if self.connection_manager else []
        except Exception:
            connections = []

        dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Export Backup"),
            body=_("Your full configuration is always backed up. Choose which connections' "
                   "saved passwords and key passphrases to include."))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(8); box.set_margin_end(8)
        box.set_margin_top(8); box.set_margin_bottom(8)

        select_all = Gtk.CheckButton(label=_("Select all connections"))
        box.append(select_all)

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
            cb = Gtk.CheckButton(label=label)
            cb.set_active(key in prefill)
            row = Gtk.ListBoxRow(); row.set_child(cb); listbox.append(row)
            checks.append((cb, conn, key))
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(180)
        scrolled.set_child(listbox)
        box.append(scrolled)

        def on_select_all(btn):
            for cb, _c, _k in checks:
                cb.set_active(btn.get_active())
        select_all.connect('toggled', on_select_all)

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
            if enc_switch.get_active():
                passphrase = pw.get_text() or ''
                if not passphrase:
                    # Re-open with the same selection so they don't lose it.
                    self._simple_dialog(_("Passphrase required"),
                                        _("Enter a passphrase, or turn off encryption."))
                    GLib.idle_add(lambda: (self._show_export_options_dialog(sel_ids, True), False)[1])
                    return
                self._choose_export_path(selected, passphrase)
            else:
                self._confirm_plaintext_then_export(selected, sel_ids)

        dialog.connect('response', on_response)
        dialog.present()

    def _confirm_plaintext_then_export(self, connections, sel_ids):
        warn = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Export without encryption?"),
            body=_("Saved passwords and key passphrases will be written in PLAIN TEXT and "
                   "readable by anyone with the file. Continue?"))
        warn.add_response('back', _('Go Back'))
        warn.add_response('plain', _('Export Unencrypted'))
        warn.set_response_appearance('plain', Adw.ResponseAppearance.DESTRUCTIVE)
        warn.set_close_response('back')

        def on_warn(dlg, resp):
            if resp == 'plain':
                self._choose_export_path(connections, None)
            else:
                GLib.idle_add(lambda: (self._show_export_options_dialog(sel_ids, False), False)[1])
        warn.connect('response', on_warn)
        warn.present()

    def _choose_export_path(self, connections, passphrase):
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
                export_path, connections=connections, passphrase=passphrase)
            if success:
                self._simple_dialog(
                    _("Export Successful"),
                    _("Backup saved to:\n{}\n\n{} credential(s) included; encryption: {}.").format(
                        export_path, len(connections),
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
                        self._perform_spbk_import(manifest, mode)
                    else:
                        self._perform_import(import_path, mode)
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
        except Exception as e:
            logger.error(f"Failed to show import mode dialog: {e}")

    def _perform_spbk_import(self, manifest, mode: str):
        """Apply a decrypted .spbk manifest: config (replace/merge) + restore credentials."""
        try:
            from .backup_manager import BackupManager
            backup_mgr = BackupManager(self.config, self.connection_manager)
            success, error, restored = backup_mgr.apply_imported_manifest(
                manifest, mode=mode, create_backup=True)
            if not success:
                self._simple_dialog(_("Import Failed"), error or _("Unknown error"))
                return
            cred_line = (_("\n\n{} credential(s) were restored.").format(restored)
                         if restored else "")
            success_dialog = Adw.MessageDialog(
                transient_for=self, modal=True, heading=_("Import Successful"),
                body=_("Backup imported successfully.{}\n\nIt is recommended to restart "
                       "SSH Pilot for all changes to take effect.").format(cred_line))
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
        except Exception as e:
            logger.error(f"Backup import failed: {e}")
            self._simple_dialog(_("Import Failed"), str(e))

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

