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
import threading
from datetime import datetime

from gi.repository import Adw, Gio, GLib, Gtk
from gettext import gettext as _

from .platform_utils import get_config_dir
from .preferences import PreferencesWindow

logger = logging.getLogger(__name__)

# Minimum content width for export/import backup dialogs (Adw.MessageDialog sizes to
# its extra_child; without this they stay uncomfortably narrow).
BACKUP_DIALOG_MIN_WIDTH = 520


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
            body=_("Select items to include in this backup."))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_size_request(BACKUP_DIALOG_MIN_WIDTH, -1)
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

        def switch_row(label, active=False, caption=None, destructive=False):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            text_label = Gtk.Label(label=label, xalign=0, hexpand=True)
            if destructive:
                try:
                    text_label.add_css_class('error')
                except Exception:
                    pass
            if caption:
                text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                text_col.append(text_label)
                cap = Gtk.Label(label=caption, xalign=0, wrap=True, hexpand=True)
                try:
                    cap.add_css_class('dim-label')
                except Exception:
                    pass
                text_col.append(cap)
                top.append(text_col)
            else:
                top.append(text_label)
            switch = Gtk.Switch(active=bool(active))
            switch.set_valign(Gtk.Align.CENTER)
            top.append(switch)
            row.append(top)
            return row, switch

        app_row, app_settings_check = switch_row(
            _("App settings and groups"), option_defaults.get('app_settings', False))
        ssh_row, ssh_config_check = switch_row(
            _("Connection profiles (SSH config)"), option_defaults.get('ssh_config', False))
        known_row, known_hosts_check = switch_row(
            _("Known hosts"), option_defaults.get('known_hosts', False))
        keys_row, private_keys_check = switch_row(
            _("Private key files"), option_defaults.get('private_keys', False),
            destructive=True)
        secrets_row, secrets_check = switch_row(
            _("Saved secrets (passwords and passphrases)"),
            option_defaults.get('secrets', False),
            caption=_("Choose which connections to include saved passwords, passphrases, "
                      "and private key files for."))
        option_checks = {
            'app_settings': app_settings_check,
            'ssh_config': ssh_config_check,
            'known_hosts': known_hosts_check,
            'secrets': secrets_check,
            'private_keys': private_keys_check,
        }
        for row in (app_row, ssh_row, known_row, keys_row, secrets_row):
            category_box.append(row)
        box.append(category_box)

        connection_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        select_all = Gtk.CheckButton(label=_("Select all"))
        select_all.set_active(False)
        select_all.add_css_class('selection-mode')
        connection_section.append(select_all)

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
            cb.add_css_class('selection-mode')
            for edge in ('start', 'end'):
                getattr(cb, f'set_margin_{edge}')(6)
            for edge in ('top', 'bottom'):
                getattr(cb, f'set_margin_{edge}')(4)
            row = Gtk.ListBoxRow(); row.set_child(cb); listbox.append(row)
            checks.append((cb, conn, key))
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(180)
        scrolled.set_child(listbox)
        connection_section.append(scrolled)
        box.append(connection_section)

        def on_select_all(switch, *_a):
            for cb, _c, _k in checks:
                cb.set_active(switch.get_active())
        select_all.connect('notify::active', on_select_all)

        def sync_connection_controls(*_a):
            needs_connections = secrets_check.get_active() or private_keys_check.get_active()
            connection_section.set_visible(needs_connections)
        def on_private_keys_toggled(switch, _pspec):
            if not private_keys_alert_guard[0] and switch.get_active():
                def decline():
                    private_keys_alert_guard[0] = True
                    try:
                        switch.set_active(False)
                    finally:
                        private_keys_alert_guard[0] = False

                self._alert_private_keys_export_risk(dialog, on_decline=decline)
            sync_connection_controls()

        private_keys_alert_guard = [False]
        secrets_check.connect('notify::active', sync_connection_controls)
        private_keys_check.connect('notify::active', on_private_keys_toggled)
        sync_connection_controls()

        dest_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        dest_heading = Gtk.Label(label=_("Destination"), xalign=0)
        dest_heading.add_css_class('heading'); dest_heading.set_margin_top(6)
        dest_box.append(dest_heading)
        dest_file = Gtk.CheckButton(label=_("Save to file (.spbk)")); dest_file.set_active(True)
        dest_bw = Gtk.CheckButton(label=_("Save to Bitwarden")); dest_bw.set_group(dest_file)
        dest_box.append(dest_file); dest_box.append(dest_bw)
        mirror_logins_check = Gtk.CheckButton(
            label=_("Also copy saved secrets as Bitwarden login items"))
        mirror_logins_check.set_margin_start(24)
        dest_box.append(mirror_logins_check)
        box.append(dest_box)

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
            # Bitwarden uses the vault's own encryption — the passphrase UI only applies to files.
            to_bw = dest_bw.get_active()
            on = enc_switch.get_active() and not to_bw
            enc_row.set_visible(not to_bw)
            pw.set_visible(on)
            caption.set_visible(not on and not to_bw)
            # "mirror as login items" only makes sense for Bitwarden with secrets included.
            mirror_logins_check.set_visible(to_bw and secrets_check.get_active())
        enc_switch.connect('notify::active', sync_pw)
        dest_file.connect('notify::active', sync_pw)
        dest_bw.connect('notify::active', sync_pw)
        secrets_check.connect('notify::active', sync_pw)
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
            # Secrets/keys are gathered per selected connection — requiring them with nothing
            # selected would silently produce an empty result.
            if (options.get('secrets') or options.get('private_keys')) and not selected:
                GLib.idle_add(lambda: (self._show_export_options_dialog(
                    sel_ids, enc_switch.get_active(), options,
                    error=_("Select at least one connection to include its saved passwords "
                            "or private keys.")), False)[1])
                return
            if dest_bw.get_active():
                # Bitwarden note destination — no file/passphrase; vault encryption only.
                mirror = (mirror_logins_check.get_active()
                          and bool(options.get('secrets')))
                self._export_to_bitwarden(selected, options, mirror_logins=mirror)
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

    def _alert_private_keys_export_risk(self, parent, *, on_decline=None):
        """Warn before including private key files in a backup."""
        heading = _("Private Key Files")
        body = _("It is not recommended to move your private keys to another machine. "
                 "If you still want to do this, make sure to protect the backup "
                 "with a passphrase.")
        if hasattr(Adw, 'AlertDialog'):
            alert = Adw.AlertDialog(heading=heading, body=body)
        else:
            alert = Adw.MessageDialog(
                transient_for=parent, modal=True, heading=heading, body=body,
            )
        alert.add_response('cancel', _('Cancel'))
        alert.add_response('ok', _('OK'))
        alert.set_default_response('ok')
        alert.set_close_response('cancel')

        def on_response(_dlg, resp):
            if resp != 'ok' and on_decline:
                on_decline()

        alert.connect('response', on_response)
        if hasattr(Adw, 'AlertDialog'):
            alert.present(parent)
        else:
            alert.present()

    def _export_to_bitwarden(self, connections, options, mirror_logins=False):
        """Get Bitwarden ready, then store the backup manifest in a secure note (optionally also
        mirroring saved secrets as Bitwarden login items)."""
        from .bitwarden_backup_setup import ensure_bitwarden_ready

        def after_ready(ready):
            if not ready:
                return

            def do_export(*_a):
                from .backup_manager import BackupManager
                from .backup_backends import BitwardenBackupBackend, BackupTooLargeForNote
                from .secret_storage import get_secret_manager
                from .bitwarden_backup_setup import progress_dialog
                name = _("sshPilot Backup {}").format(datetime.now().strftime('%Y-%m-%d %H:%M'))
                bw = get_secret_manager().get_backend("bitwarden")
                mgr = BackupManager(self.config, self.connection_manager)
                backend = BitwardenBackupBackend(bw, item_name=name)

                cancelled = {'v': False}
                _set_status, close_spinner = progress_dialog(
                    self, _("Export to Bitwarden"),
                    _("Exporting to Bitwarden — this may take a while…"),
                    on_cancel=lambda: cancelled.__setitem__('v', True))

                # Building the manifest reads secrets (may hit bw) and the note write spawns bw —
                # all off the main thread so the UI doesn't freeze ("not responding").
                def worker():
                    try:
                        entry = mgr.export_to_backend(
                            backend, connections=connections, options=options,
                            mirror_to=(bw if mirror_logins else None))
                        counts = getattr(mgr, 'last_export_counts', {})
                        mirror = getattr(mgr, 'last_mirror_counts', None)
                        payload = ('ok', entry.name, counts.get('credentials', 0),
                                   counts.get('private_keys', 0), mirror)
                    except BackupTooLargeForNote as e:
                        payload = ('toobig', str(e))
                    except Exception as e:
                        logger.error("Bitwarden export failed: %s", e)
                        payload = ('error', str(e))
                    GLib.idle_add(lambda: (_report(payload), False)[1])

                def _report(p):
                    if cancelled['v']:
                        return   # user cancelled the wait
                    close_spinner()
                    if p[0] == 'ok':
                        msg = _("Backup saved to Bitwarden as “{}”.\n\n{} credential(s) and {} "
                                "private key(s) included.").format(p[1], p[2], p[3])
                        mirror = p[4]
                        if mirror:
                            msg += "\n\n" + _("{} secret(s) also copied as Bitwarden login "
                                              "items.").format(mirror.get('mirrored', 0))
                        self._simple_dialog(_("Export Successful"), msg)
                    elif p[0] == 'toobig':
                        self._simple_dialog(_("Backup too large for Bitwarden"), p[1])
                    else:
                        self._simple_dialog(_("Export Failed"), p[1])

                threading.Thread(target=worker, daemon=True).start()

            # Reading saved secrets for the manifest may need the CURRENT secrets backend unlocked.
            self._run_after_vault_unlock_for_secrets(
                do_export, needed=bool(options.get('secrets')),
                cancelled_heading=_("Export Cancelled"))

        ensure_bitwarden_ready(self, after_ready)

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

            def do_export(*_args):
                backup_mgr = BackupManager(self.config, self.connection_manager)
                success, error = backup_mgr.export_backup(
                    export_path, connections=connections, passphrase=passphrase,
                    options=options)
                if success:
                    counts = getattr(backup_mgr, 'last_export_counts', {})
                    msg = _("Backup saved to:\n{}\n\n{} credential(s) and {} private key(s) "
                            "included; encryption: {}.").format(
                        export_path, counts.get('credentials', 0),
                        counts.get('private_keys', 0),
                        _("on") if passphrase else _("off"))
                    skipped = getattr(backup_mgr, 'last_export_skipped_config_files', []) or []
                    if skipped:
                        msg += "\n\n" + _("{} SSH config file(s) outside your ~/.ssh were not "
                                          "included (system or shared files):\n{}").format(
                            len(skipped), "\n".join(skipped))
                    missing_keys = getattr(backup_mgr, 'last_export_missing_key_files', []) or []
                    if missing_keys:
                        msg += "\n\n" + _("{} referenced key file(s) were missing and not "
                                          "included:\n{}").format(
                            len(missing_keys), "\n".join(missing_keys))
                    self._simple_dialog(_("Export Successful"), msg)
                else:
                    self._simple_dialog(_("Export Failed"), error or _("Unknown error"))

            self._run_after_vault_unlock_for_secrets(
                do_export,
                needed=bool(options.get('secrets')),
                cancelled_heading=_("Export Cancelled"),
            )

        file_dialog.save(self, None, on_save_response)

    def show_import_dialog(self):
        """Ask where to import from (file or Bitwarden), then run that flow."""
        logger.info("Show import source dialog")
        try:
            dialog = Adw.MessageDialog(
                transient_for=self, modal=True, heading=_("Import Configuration"),
                body=_("Import a backup from a file or from your Bitwarden vault."))
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('bitwarden', _('From Bitwarden'))
            dialog.add_response('file', _('From File'))
            dialog.set_default_response('file')
            dialog.set_close_response('cancel')

            def on_source(_d, resp):
                if resp == 'file':
                    self._import_from_file()
                elif resp == 'bitwarden':
                    self._import_from_bitwarden()
            dialog.connect('response', on_source)
            dialog.present()
        except Exception as e:
            logger.error(f"Failed to show import source dialog: {e}")

    def _import_from_file(self):
        """Show the file chooser for a .spbk / .json import."""
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

    def _import_from_bitwarden(self):
        """Ensure Bitwarden is ready, list sshPilot backups, then restore the chosen one."""
        from .bitwarden_backup_setup import ensure_bitwarden_ready

        def proceed(ready):
            if not ready:
                return
            from .backup_backends import BitwardenBackupBackend
            from .secret_storage import get_secret_manager
            from .bitwarden_backup_setup import progress_dialog
            backend = BitwardenBackupBackend(get_secret_manager().get_backend("bitwarden"))
            cancelled = {'v': False}
            _set_status, close_spinner = progress_dialog(
                self, _("Import from Bitwarden"), _("Loading backups from Bitwarden…"),
                on_cancel=lambda: cancelled.__setitem__('v', True))

            def worker():   # bw list items is slow — keep it off the main thread
                try:
                    payload = ('ok', backend.list_exports())
                except Exception as e:
                    logger.error("Listing Bitwarden backups failed: %s", e)
                    payload = ('error', str(e))
                GLib.idle_add(lambda: (_after_list(payload), False)[1])

            def _after_list(p):
                if cancelled['v']:
                    return
                close_spinner()
                if p[0] != 'ok':
                    self._simple_dialog(_("Import Failed"), p[1])
                    return
                entries = p[1]
                if not entries:
                    self._simple_dialog(
                        _("No backups found"),
                        _("No sshPilot backups were found in your Bitwarden vault."))
                    return
                self._show_bitwarden_entry_chooser(backend, entries)

            threading.Thread(target=worker, daemon=True).start()

        ensure_bitwarden_ready(self, proceed)

    def _show_bitwarden_entry_chooser(self, backend, entries):
        dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Import from Bitwarden"),
            body=_("Choose a backup to restore:"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(12); box.set_margin_bottom(12)
        radios = []
        group = None
        for entry in entries:
            label = entry.name + (f"  ({entry.date[:10]})" if entry.date else "")
            rb = Gtk.CheckButton(label=label)
            if group is None:
                group = rb
                rb.set_active(True)
            else:
                rb.set_group(group)
            radios.append((rb, entry))
            box.append(rb)
        dialog.set_extra_child(box)
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('open', _('Restore'))
        dialog.set_response_appearance('open', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('open')
        dialog.set_close_response('cancel')

        def on_resp(_d, resp):
            if resp != 'open':
                return
            entry = next((e for rb, e in radios if rb.get_active()), None)
            if entry is None:
                return
            from .bitwarden_backup_setup import progress_dialog
            cancelled = {'v': False}
            _set_status, close_spinner = progress_dialog(
                self, _("Import from Bitwarden"), _("Reading backup from Bitwarden…"),
                on_cancel=lambda: cancelled.__setitem__('v', True))

            def worker():   # bw get item is slow — keep it off the main thread
                try:
                    payload = ('ok', backend.read(entry))
                except Exception as e:
                    logger.error("Reading Bitwarden backup failed: %s", e)
                    payload = ('error', str(e))
                GLib.idle_add(lambda: (_after_read(payload), False)[1])

            def _after_read(p):
                if cancelled['v']:
                    return
                close_spinner()
                if p[0] != 'ok':
                    self._simple_dialog(_("Import Failed"), p[1])
                    return
                # Reuse the .spbk apply path — a Bitwarden-note manifest has the same shape.
                self._show_import_mode_dialog("", manifest=p[1])

            threading.Thread(target=worker, daemon=True).start()
        dialog.connect('response', on_resp)
        dialog.present()

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
        entry.set_size_request(BACKUP_DIALOG_MIN_WIDTH, -1)
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
            content_box.set_size_request(BACKUP_DIALOG_MIN_WIDTH, -1)
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
                    'ssh_config': _("Connection profiles (SSH config)"),
                    'known_hosts': _("Known hosts"),
                    'secrets': _("Saved secrets (passwords and passphrases)"),
                    'private_keys': _("Private key files"),
                }
                for key in BACKUP_OPTION_KEYS:
                    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                    label = Gtk.Label(label=labels[key], xalign=0, hexpand=True)
                    if key == 'private_keys':
                        try:
                            label.add_css_class('error')
                        except Exception:
                            pass
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
                        will_replace_ssh = restore_options.get('ssh_config', False)
                        self._guard_default_mode_replace(
                            mode, will_replace_ssh,
                            lambda: self._perform_spbk_import(manifest, mode, restore_options))
                    else:
                        # Legacy JSON: we can't see categories up front, so assume ssh_config.
                        self._guard_default_mode_replace(
                            mode, True,
                            lambda: self._perform_import(import_path, mode))
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
        except Exception as e:
            logger.error(f"Failed to show import mode dialog: {e}")

    def _guard_default_mode_replace(self, mode: str, will_replace_ssh: bool, proceed):
        """Before a Replace that overwrites the GLOBAL ``~/.ssh/config`` (default mode), make the
        blast radius explicit — that file is shared with ssh/scp/git/rsync and everything else.
        In isolated mode (sshPilot's own config file) or for Merge, proceed without the prompt."""
        isolated = bool(getattr(self.connection_manager, 'isolated_mode', False))
        if mode != 'replace' or not will_replace_ssh or isolated:
            proceed()
            return
        ssh_path = getattr(self.connection_manager, 'ssh_config_path', '') \
            or os.path.expanduser('~/.ssh/config')
        warn = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Replace your global SSH config?"),
            body=_("Replace mode will overwrite {} entirely. That file is shared with ssh, "
                   "scp, git and every other SSH tool on this machine — any Host blocks, "
                   "Include and Match rules not managed by sshPilot will be lost.\n\n"
                   "Choose Merge instead to add hosts without overwriting, or continue to "
                   "replace. A backup is saved first either way.").format(ssh_path))
        warn.add_response('cancel', _('Cancel'))
        warn.add_response('replace', _('Replace Anyway'))
        warn.set_response_appearance('replace', Adw.ResponseAppearance.DESTRUCTIVE)
        warn.set_close_response('cancel')

        def on_warn(dlg, resp):
            if resp == 'replace':
                proceed()
        warn.connect('response', on_warn)
        warn.present()

    def _run_after_vault_unlock_for_secrets(self, proceed, *, needed: bool,
                                            cancelled_heading: str):
        """Run ``proceed()`` once the session vault is unlocked when secrets are involved.

        If the vault is locked, prompt to unlock first. When unlock fails or the user cancels,
        show ``cancelled_heading`` and do **not** call ``proceed()`` — export/import must not
        silently continue with zero credentials restored or included."""
        if not needed:
            proceed()
            return
        try:
            from .secret_storage import get_secret_manager
            if not get_secret_manager().selected_needs_unlock():
                proceed()
                return
            from .secret_unlock_dialog import prompt_unlock

            def _on_done(unlocked):
                if unlocked:
                    proceed()
                    return
                self._simple_dialog(
                    cancelled_heading,
                    _("Your secret vault must be unlocked to include or restore saved "
                      "credentials. Unlock it and try again."))

            prompt_unlock(self, on_done=_on_done)
        except Exception:
            # If we cannot even verify the vault, abort rather than silently proceed with a
            # backup/restore that would be missing credentials.
            logger.warning("Pre-operation vault unlock check failed; aborting", exc_info=True)
            self._simple_dialog(
                cancelled_heading,
                _("Could not verify the secret vault, so the operation was cancelled to avoid "
                  "leaving saved credentials out. Try again."))

    def _perform_spbk_import(self, manifest, mode: str, restore_options=None):
        """Apply a decrypted .spbk manifest: config (replace/merge) + restore credentials.

        If the manifest carries credentials and the selected secret backend is a **locked
        session vault** (e.g. Bitwarden), unlock it first — import is aborted if unlock fails
        or the user cancels, so credentials are never silently skipped.  Applying a manifest
        may invoke the selected secret backend once per credential, so keep that work off the
        GTK main thread and show progress until the result is ready."""
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
            from .bitwarden_backup_setup import progress_dialog

            _set_status, close_spinner = progress_dialog(
                self, _("Import Configuration"),
                _("Applying backup — this may take a while…"))

            def worker():
                try:
                    from .backup_manager import BackupManager
                    backup_mgr = BackupManager(self.config, self.connection_manager)
                    success, error, restored, restored_keys = (
                        backup_mgr.apply_imported_manifest(
                            manifest, mode=mode, create_backup=True,
                            restore_options=restore_options)
                    )
                    payload = (
                        'ok', success, error, restored, restored_keys,
                        getattr(backup_mgr, 'last_import_skipped_keys', 0),
                        getattr(backup_mgr, 'last_import_secrets_persisted', True),
                        getattr(backup_mgr, 'last_import_skipped_credentials', 0),
                        getattr(backup_mgr, 'last_merge_collisions', []) or [],
                        getattr(backup_mgr, 'last_merge_dropped_globals', 0),
                    )
                except Exception as e:
                    logger.error("Backup import failed: %s", e)
                    payload = ('error', str(e))
                GLib.idle_add(lambda: (_report(payload), False)[1])

            def _report(payload):
                close_spinner()
                if payload[0] == 'error':
                    self._simple_dialog(_("Import Failed"), payload[1])
                    return
                (_, success, error, restored, restored_keys, skipped_keys,
                 secrets_persisted, skipped_creds, merge_collisions,
                 dropped_globals) = payload
                if not success:
                    self._simple_dialog(
                        _("Import Failed"), error or _("Unknown error"))
                    return
                self._show_import_success(
                    restored, total, restored_keys, total_keys,
                    skipped_keys, secrets_persisted, skipped_creds,
                    merge_collisions=merge_collisions,
                    dropped_globals=dropped_globals)

            threading.Thread(target=worker, daemon=True).start()

        self._run_after_vault_unlock_for_secrets(
            do_apply,
            needed=total > 0,
            cancelled_heading=_("Import Cancelled"),
        )

    def _show_import_success(self, restored: int, total: int,
                             restored_keys: int = 0, total_keys: int = 0,
                             skipped_keys: int = 0, secrets_persisted: bool = True,
                             skipped_credentials: int = 0, merge_collisions=None,
                             dropped_globals: int = 0):
        # Keys that already existed were left untouched by design (never overwritten), so they
        # are NOT counted as failures. Genuine key failures are the remainder.
        failed_keys = max(0, total_keys - restored_keys - skipped_keys)
        # Secrets that already existed were likewise left untouched — not a failure either.
        failed_creds = max(0, total - restored - skipped_credentials)
        lines = []
        if total == 0 and total_keys == 0:
            lines.append(_("Backup imported successfully."))
        else:
            all_ok = (failed_creds == 0) and (failed_keys == 0)
            lines.append(_("Backup imported successfully.") if all_ok
                         else _("Backup imported, with some items skipped."))
            done = []
            if restored:
                done.append(_("{} credential(s)").format(restored))
            if restored_keys:
                done.append(_("{} private key(s)").format(restored_keys))
            if done:
                lines.append(_("Restored {}.").format(_(", ").join(done)))
            if skipped_credentials:
                lines.append(_("{} credential(s) already existed and were left untouched — "
                               "sshPilot never overwrites a saved secret.").format(
                                   skipped_credentials))
            if skipped_keys:
                lines.append(_("{} private key(s) already existed and were left untouched — "
                               "sshPilot never overwrites a private key.").format(skipped_keys))
            if failed_creds:
                if not secrets_persisted:
                    lines.append(_("{} credential(s) were not stored because the selected secret "
                                   "backend does not save secrets (“agent”). Choose a "
                                   "storage backend in Preferences ▸ Security & Credentials, "
                                   "then import again.").format(failed_creds))
                else:
                    lines.append(_("{} of {} credential(s) could not be restored — the selected "
                                   "secret backend may be locked or unavailable.").format(
                                       failed_creds, total))
            if failed_keys:
                lines.append(_("{} private key(s) could not be written (the target path may "
                               "not be writable).").format(failed_keys))
        if merge_collisions:
            names = ", ".join(" ".join(p) for p in merge_collisions)
            lines.append(_("{} imported host(s) shared a name with an existing host; the "
                           "conflicting names were left as-is: {}.").format(
                               len(merge_collisions), names))
        if dropped_globals:
            lines.append(_("{} global rule(s) from the backup (Host * / Match blocks) were not "
                           "imported — they affect every connection, so they are not merged "
                           "automatically.").format(dropped_globals))
        lines.append(_("Reload now to apply the imported configuration. Some settings may still "
                       "need a full restart of sshPilot to take effect."))
        body = "\n\n".join(lines)

        success_dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Import Successful"), body=body)
        success_dialog.add_response('ok', _('OK'))
        success_dialog.add_response('restart', _('Reload Now'))
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
                body = _("Configuration imported successfully.")
                ignored = getattr(backup_mgr, 'last_import_ignored_secrets', 0)
                if ignored:
                    body += "\n\n" + _("{} saved password(s)/key(s) in this .json file were not "
                                       "imported — legacy JSON backups can't restore secrets. "
                                       "Use an encrypted .spbk backup to include them.").format(
                                           ignored)
                body += "\n\n" + _("Reload now to apply it. Some settings may still need a full "
                                   "restart of sshPilot to take effect.")
                success_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Import Successful"),
                    body=body,
                )
                success_dialog.add_response('ok', _('OK'))
                success_dialog.add_response('restart', _('Reload Now'))
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

