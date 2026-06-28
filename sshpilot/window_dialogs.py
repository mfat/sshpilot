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

    def show_export_dialog(self):
        """Show export configuration dialog"""
        logger.info("Show export configuration dialog")
        try:
            from .backup_manager import BackupManager
            
            # Create file chooser dialog for saving
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title(_("Export Configuration"))
            file_dialog.set_initial_name(f"sshpilot_config_{datetime.now().strftime('%Y%m%d')}.json")
            
            # Set default folder to user's documents or home
            try:
                docs_path = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
                if docs_path:
                    file_dialog.set_initial_folder(Gio.File.new_for_path(docs_path))
            except Exception:
                pass
            
            def on_save_response(dialog, result):
                try:
                    file = dialog.save_finish(result)
                    if file:
                        export_path = file.get_path()
                        
                        # Perform export
                        backup_mgr = BackupManager(self.config, self.connection_manager)
                        success, error = backup_mgr.export_configuration(export_path)
                        
                        if success:
                            # Show success dialog
                            success_dialog = Adw.MessageDialog(
                                transient_for=self,
                                modal=True,
                                heading=_("Export Successful"),
                                body=_("Configuration exported successfully to:\n{}").format(export_path)
                            )
                            success_dialog.add_response('ok', _('OK'))
                            success_dialog.present()
                        else:
                            # Show error dialog
                            error_dialog = Adw.MessageDialog(
                                transient_for=self,
                                modal=True,
                                heading=_("Export Failed"),
                                body=_("Failed to export configuration:\n{}").format(error or "Unknown error")
                            )
                            error_dialog.add_response('ok', _('OK'))
                            error_dialog.present()
                            
                except GLib.Error as e:
                    # Check if user cancelled the dialog (error code 2 = GTK_DIALOG_ERROR_DISMISSED)
                    if e.code == 2:
                        logger.info("Export cancelled by user")
                    else:
                        logger.error(f"Export failed: {e}")
                        error_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Export Failed"),
                            body=_("An error occurred during export:\n{}").format(str(e))
                        )
                        error_dialog.add_response('ok', _('OK'))
                        error_dialog.present()
                except Exception as e:
                    logger.error(f"Export failed: {e}")
                    error_dialog = Adw.MessageDialog(
                        transient_for=self,
                        modal=True,
                        heading=_("Export Failed"),
                        body=_("An error occurred during export:\n{}").format(str(e))
                    )
                    error_dialog.add_response('ok', _('OK'))
                    error_dialog.present()
            
            file_dialog.save(self, None, on_save_response)
            
        except Exception as e:
            logger.error(f"Failed to show export dialog: {e}")

    def show_import_dialog(self):
        """Show import configuration dialog"""
        logger.info("Show import configuration dialog")
        try:
            # Create file chooser dialog for opening
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title(_("Import Configuration"))
            
            # Set file filter for JSON files
            filter_json = Gtk.FileFilter()
            filter_json.set_name(_("JSON files"))
            filter_json.add_mime_type("application/json")
            filter_json.add_pattern("*.json")
            
            filter_all = Gtk.FileFilter()
            filter_all.set_name(_("All files"))
            filter_all.add_pattern("*")
            
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(filter_json)
            filters.append(filter_all)
            file_dialog.set_filters(filters)
            file_dialog.set_default_filter(filter_json)
            
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
                        # Show import mode selection dialog
                        self._show_import_mode_dialog(import_path)
                        
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

    def _show_import_mode_dialog(self, import_path: str):
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
                    self._perform_import(import_path, mode)
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
        except Exception as e:
            logger.error(f"Failed to show import mode dialog: {e}")

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

