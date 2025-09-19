from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, GLib, Gio, GObject
import os
import stat
import threading
from pathlib import Path
from typing import Optional, TYPE_CHECKING
import paramiko
from datetime import datetime

if TYPE_CHECKING:
    from .connection_manager import Connection

class SftpFileManager(Adw.ApplicationWindow):
    def __init__(
        self,
        app,
        *,
        connection: Optional["Connection"] = None,
        restrict_to_file_manager: bool = False,
        auto_connect: bool = False,
    ):
        super().__init__(application=app)

        self.sftp_client = None
        self.ssh_client = None
        self.current_local_path = Path.home()
        self.current_remote_path = "/"

        self.local_store = None
        self.local_tree_view = None
        self.remote_store = None
        self.remote_tree_view = None
        self.upload_btn = None
        self.download_btn = None

        self.prefill_host: Optional[str] = None
        self.prefill_port: int = 22
        self.prefill_username: Optional[str] = None
        self.prefill_password: Optional[str] = None
        self.auto_connect = auto_connect
        self.restrict_to_file_manager = restrict_to_file_manager

        if connection is not None:
            self._apply_connection_defaults(connection)

        self.set_title("SFTP File Manager")
        self.set_default_size(1200, 800)

        self.setup_ui()
        self.refresh_local_view()

        if (
            self.auto_connect
            and self.prefill_host
            and self.prefill_username
            and self.prefill_password
        ):
            try:
                self.connect_sftp(
                    self.prefill_host,
                    self.prefill_port,
                    self.prefill_username,
                    self.prefill_password,
                )
            except Exception:
                pass
        elif self.auto_connect and (self.prefill_host or self.prefill_username):
            GLib.idle_add(lambda: (self.show_connection_dialog(self.connect_btn), False)[1])

    def _apply_connection_defaults(self, connection: "Connection"):
        host = getattr(connection, "hostname", None) or getattr(connection, "host", None)
        if isinstance(host, str) and host:
            self.prefill_host = host
        nickname = getattr(connection, "nickname", None)
        if not self.prefill_host and isinstance(nickname, str) and nickname:
            self.prefill_host = nickname
        try:
            port = int(getattr(connection, "port", 22) or 22)
        except Exception:
            port = 22
        self.prefill_port = port
        username = getattr(connection, "username", None)
        if isinstance(username, str) and username:
            self.prefill_username = username
        password = getattr(connection, "password", None)
        if isinstance(password, str) and password:
            self.prefill_password = password
        
    def setup_ui(self):
        # Main content
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)

        # Header bar
        header_bar = Adw.HeaderBar()
        main_box.append(header_bar)
        
        # Connection button
        self.connect_btn = Gtk.Button(label="Connect to Server")
        self.connect_btn.add_css_class("suggested-action")
        self.connect_btn.connect("clicked", self.show_connection_dialog)
        header_bar.pack_start(self.connect_btn)
        
        # Status label
        self.status_label = Gtk.Label(label="Not connected")
        self.status_label.add_css_class("dim-label")
        header_bar.pack_end(self.status_label)
        
        # Main content area
        if self.restrict_to_file_manager:
            remote_panel = self.create_file_panel("Remote", False, enable_transfers=False)
            main_box.append(remote_panel)
            self.paned = None
        else:
            self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            self.paned.set_shrink_start_child(False)
            self.paned.set_shrink_end_child(False)
            self.paned.set_position(600)
            main_box.append(self.paned)

            # Local panel
            local_panel = self.create_file_panel("Local", True)
            self.paned.set_start_child(local_panel)

            # Remote panel
            remote_panel = self.create_file_panel("Remote", False)
            self.paned.set_end_child(remote_panel)
        
    def create_file_panel(self, title, is_local, enable_transfers=True):
        # Main container
        panel_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Panel header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header_box.set_margin_start(12)
        header_box.set_margin_end(12)
        header_box.set_margin_top(6)
        header_box.set_margin_bottom(6)
        panel_box.append(header_box)
        
        # Title
        title_label = Gtk.Label(label=title)
        title_label.add_css_class("heading")
        header_box.append(title_label)
        
        # Path entry
        path_entry = Gtk.Entry()
        path_entry.set_hexpand(True)
        path_entry.set_margin_start(12)
        if is_local:
            path_entry.set_text(str(self.current_local_path))
            path_entry.connect("activate", self.on_local_path_changed)
            self.local_path_entry = path_entry
        else:
            path_entry.set_text(self.current_remote_path)
            path_entry.connect("activate", self.on_remote_path_changed)
            path_entry.set_sensitive(False)
            self.remote_path_entry = path_entry
        header_box.append(path_entry)
        
        # Navigation buttons
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        nav_box.add_css_class("linked")
        header_box.append(nav_box)
        
        # Up button
        up_btn = Gtk.Button(icon_name="go-up-symbolic")
        up_btn.set_tooltip_text("Go up")
        if is_local:
            up_btn.connect("clicked", lambda x: self.navigate_local(".."))
        else:
            up_btn.connect("clicked", lambda x: self.navigate_remote(".."))
        nav_box.append(up_btn)
        
        # Refresh button
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh")
        if is_local:
            refresh_btn.connect("clicked", lambda x: self.refresh_local_view())
        else:
            refresh_btn.connect("clicked", lambda x: self.refresh_remote_view())
        nav_box.append(refresh_btn)
        
        # File list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        panel_box.append(scrolled)
        
        # Create list store and tree view
        store = Gtk.ListStore(str, str, str, str, bool)  # name, size, modified, type, is_dir
        tree_view = Gtk.TreeView(model=store)
        tree_view.set_headers_visible(True)
        
        # Name column
        name_renderer = Gtk.CellRendererText()
        name_column = Gtk.TreeViewColumn("Name", name_renderer, text=0)
        name_column.set_expand(True)
        tree_view.append_column(name_column)
        
        # Size column
        size_renderer = Gtk.CellRendererText()
        size_column = Gtk.TreeViewColumn("Size", size_renderer, text=1)
        size_column.set_min_width(80)
        tree_view.append_column(size_column)
        
        # Modified column
        modified_renderer = Gtk.CellRendererText()
        modified_column = Gtk.TreeViewColumn("Modified", modified_renderer, text=2)
        modified_column.set_min_width(150)
        tree_view.append_column(modified_column)
        
        # Type column
        type_renderer = Gtk.CellRendererText()
        type_column = Gtk.TreeViewColumn("Type", type_renderer, text=3)
        type_column.set_min_width(80)
        tree_view.append_column(type_column)
        
        # Connect double-click
        if is_local:
            tree_view.connect("row-activated", self.on_local_row_activated)
            self.local_store = store
            self.local_tree_view = tree_view
        else:
            tree_view.connect("row-activated", self.on_remote_row_activated)
            self.remote_store = store
            self.remote_tree_view = tree_view
        
        scrolled.set_child(tree_view)
        
        # Action buttons
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        action_box.set_spacing(6)
        action_box.set_margin_start(12)
        action_box.set_margin_end(12)
        action_box.set_margin_bottom(12)
        panel_box.append(action_box)

        if is_local:
            upload_btn = Gtk.Button(label="Upload →")
            upload_btn.add_css_class("suggested-action")
            upload_btn.connect("clicked", self.upload_file)
            upload_btn.set_sensitive(False)
            action_box.append(upload_btn)
            self.upload_btn = upload_btn
        else:
            if enable_transfers:
                download_btn = Gtk.Button(label="← Download")
                download_btn.add_css_class("suggested-action")
                download_btn.connect("clicked", self.download_file)
                download_btn.set_sensitive(False)
                action_box.append(download_btn)
                self.download_btn = download_btn
            else:
                self.download_btn = None

        return panel_box
    
    def show_connection_dialog(self, button):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Connect to SFTP Server"
        )
        
        # Create form
        form_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        form_box.set_spacing(12)
        form_box.set_margin_start(12)
        form_box.set_margin_end(12)
        form_box.set_margin_top(12)
        form_box.set_margin_bottom(12)
        
        # Host
        host_group = Adw.PreferencesGroup()
        host_row = Adw.EntryRow()
        host_row.set_title("Host")
        if self.prefill_host:
            host_row.set_text(self.prefill_host)
        else:
            host_row.set_text("localhost")
        host_group.add(host_row)
        form_box.append(host_group)

        # Port
        port_group = Adw.PreferencesGroup()
        port_row = Adw.EntryRow()
        port_row.set_title("Port")
        port_row.set_text(str(self.prefill_port))
        port_group.add(port_row)
        form_box.append(port_group)

        # Username
        user_group = Adw.PreferencesGroup()
        user_row = Adw.EntryRow()
        user_row.set_title("Username")
        if self.prefill_username:
            user_row.set_text(self.prefill_username)
        user_group.add(user_row)
        form_box.append(user_group)

        # Password
        pass_group = Adw.PreferencesGroup()
        pass_row = Adw.PasswordEntryRow()
        pass_row.set_title("Password")
        if self.prefill_password:
            pass_row.set_text(self.prefill_password)
        pass_group.add(pass_row)
        form_box.append(pass_group)
        
        dialog.set_extra_child(form_box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("connect", "Connect")
        dialog.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        
        dialog.connect("response", lambda d, r: self.on_connection_response(
            d, r, host_row.get_text(), int(port_row.get_text() or 22), 
            user_row.get_text(), pass_row.get_text()
        ))
        
        dialog.present()
    
    def on_connection_response(self, dialog, response, host, port, username, password):
        dialog.close()
        if response == "connect":
            self.connect_sftp(host, port, username, password)
    
    def connect_sftp(self, host, port, username, password):
        def do_connect():
            try:
                self.ssh_client = paramiko.SSHClient()
                self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.ssh_client.connect(host, port, username, password)
                self.sftp_client = self.ssh_client.open_sftp()
                
                GLib.idle_add(self.on_connected, host)
            except Exception as e:
                GLib.idle_add(self.on_connection_error, str(e))
        
        threading.Thread(target=do_connect, daemon=True).start()
        self.status_label.set_text("Connecting...")
    
    def on_connected(self, host):
        self.status_label.set_text(f"Connected to {host}")
        self.connect_btn.set_label("Disconnect")
        self.connect_btn.remove_css_class("suggested-action")
        self.connect_btn.add_css_class("destructive-action")
        self.connect_btn.disconnect_by_func(self.show_connection_dialog)
        self.connect_btn.connect("clicked", self.disconnect_sftp)
        
        self.remote_path_entry.set_sensitive(True)
        if self.upload_btn:
            self.upload_btn.set_sensitive(True)
        if self.download_btn:
            self.download_btn.set_sensitive(True)
        
        self.refresh_remote_view()
    
    def on_connection_error(self, error):
        self.status_label.set_text("Connection failed")
        
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Connection Error",
            body=f"Failed to connect: {error}"
        )
        dialog.add_response("ok", "OK")
        dialog.present()
    
    def disconnect_sftp(self, button):
        if self.sftp_client:
            self.sftp_client.close()
        if self.ssh_client:
            self.ssh_client.close()
        
        self.sftp_client = None
        self.ssh_client = None
        
        self.status_label.set_text("Not connected")
        self.connect_btn.set_label("Connect to Server")
        self.connect_btn.remove_css_class("destructive-action")
        self.connect_btn.add_css_class("suggested-action")
        self.connect_btn.disconnect_by_func(self.disconnect_sftp)
        self.connect_btn.connect("clicked", self.show_connection_dialog)
        
        self.remote_path_entry.set_sensitive(False)
        if self.upload_btn:
            self.upload_btn.set_sensitive(False)
        if self.download_btn:
            self.download_btn.set_sensitive(False)

        if self.remote_store:
            self.remote_store.clear()
    
    def refresh_local_view(self):
        self.local_store.clear()
        try:
            entries = list(self.current_local_path.iterdir())
            entries.sort(key=lambda x: (not x.is_dir(), x.name.lower()))

            for entry in entries:
                try:
                    stat_info = entry.stat()
                    size = self.format_size(stat_info.st_size) if entry.is_file() else ""
                    modified = datetime.fromtimestamp(stat_info.st_mtime).strftime("%Y-%m-%d %H:%M")
                    file_type = "Directory" if entry.is_dir() else "File"
                    
                    self.local_store.append([
                        entry.name, size, modified, file_type, entry.is_dir()
                    ])
                except (OSError, PermissionError):
                    continue
                    
        except (OSError, PermissionError) as e:
            self.show_error(f"Cannot read directory: {e}")
        
        if hasattr(self, "local_path_entry") and self.local_path_entry:
            self.local_path_entry.set_text(str(self.current_local_path))
    
    def refresh_remote_view(self):
        if not self.sftp_client:
            return

        def do_refresh():
            try:
                entries = self.sftp_client.listdir_attr(self.current_remote_path)
                entries.sort(key=lambda x: (not stat.S_ISDIR(x.st_mode), x.filename.lower()))
                
                GLib.idle_add(self.update_remote_store, entries)
            except Exception as e:
                GLib.idle_add(self.show_error, f"Cannot read remote directory: {e}")
        
        threading.Thread(target=do_refresh, daemon=True).start()
    
    def update_remote_store(self, entries):
        if not self.remote_store:
            return
        self.remote_store.clear()
        for entry in entries:
            size = self.format_size(entry.st_size) if not stat.S_ISDIR(entry.st_mode) else ""
            modified = datetime.fromtimestamp(entry.st_mtime).strftime("%Y-%m-%d %H:%M")
            file_type = "Directory" if stat.S_ISDIR(entry.st_mode) else "File"
            
            self.remote_store.append([
                entry.filename, size, modified, file_type, stat.S_ISDIR(entry.st_mode)
            ])
        
        self.remote_path_entry.set_text(self.current_remote_path)
    
    def format_size(self, size):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
    
    def navigate_local(self, path):
        if path == "..":
            new_path = self.current_local_path.parent
        else:
            new_path = self.current_local_path / path
        
        if new_path.exists() and new_path.is_dir():
            self.current_local_path = new_path
            self.refresh_local_view()
    
    def navigate_remote(self, path):
        if not self.sftp_client:
            return
            
        if path == "..":
            new_path = str(Path(self.current_remote_path).parent)
        else:
            new_path = str(Path(self.current_remote_path) / path)
        
        try:
            self.sftp_client.listdir(new_path)
            self.current_remote_path = new_path
            self.refresh_remote_view()
        except Exception as e:
            self.show_error(f"Cannot navigate to {new_path}: {e}")
    
    def on_local_path_changed(self, entry):
        path = Path(entry.get_text())
        if path.exists() and path.is_dir():
            self.current_local_path = path
            self.refresh_local_view()
    
    def on_remote_path_changed(self, entry):
        if not self.sftp_client:
            return
        
        path = entry.get_text()
        try:
            self.sftp_client.listdir(path)
            self.current_remote_path = path
            self.refresh_remote_view()
        except Exception as e:
            self.show_error(f"Cannot navigate to {path}: {e}")
            entry.set_text(self.current_remote_path)
    
    def on_local_row_activated(self, tree_view, path, column):
        model = tree_view.get_model()
        iterator = model.get_iter(path)
        name = model.get_value(iterator, 0)
        is_dir = model.get_value(iterator, 4)
        
        if is_dir:
            self.navigate_local(name)
    
    def on_remote_row_activated(self, tree_view, path, column):
        model = tree_view.get_model()
        iterator = model.get_iter(path)
        name = model.get_value(iterator, 0)
        is_dir = model.get_value(iterator, 4)
        
        if is_dir:
            self.navigate_remote(name)
    
    def upload_file(self, button):
        if not self.local_tree_view:
            self.show_error("Local browser is unavailable in this mode")
            return
        selection = self.local_tree_view.get_selection()
        model, iterator = selection.get_selected()
        
        if not iterator:
            self.show_error("Please select a file to upload")
            return
        
        filename = model.get_value(iterator, 0)
        is_dir = model.get_value(iterator, 4)
        
        if is_dir:
            self.show_error("Directory upload not supported yet")
            return
        
        local_path = self.current_local_path / filename
        remote_path = str(Path(self.current_remote_path) / filename)
        
        def do_upload():
            try:
                self.sftp_client.put(str(local_path), remote_path)
                GLib.idle_add(self.refresh_remote_view)
                GLib.idle_add(self.show_info, f"Uploaded {filename}")
            except Exception as e:
                GLib.idle_add(self.show_error, f"Upload failed: {e}")
        
        threading.Thread(target=do_upload, daemon=True).start()
    
    def download_file(self, button):
        if not self.remote_tree_view:
            self.show_error("Remote browser is unavailable")
            return
        selection = self.remote_tree_view.get_selection()
        model, iterator = selection.get_selected()
        
        if not iterator:
            self.show_error("Please select a file to download")
            return
        
        filename = model.get_value(iterator, 0)
        is_dir = model.get_value(iterator, 4)
        
        if is_dir:
            self.show_error("Directory download not supported yet")
            return
        
        remote_path = str(Path(self.current_remote_path) / filename)
        local_path = self.current_local_path / filename
        
        def do_download():
            try:
                self.sftp_client.get(remote_path, str(local_path))
                GLib.idle_add(self.refresh_local_view)
                GLib.idle_add(self.show_info, f"Downloaded {filename}")
            except Exception as e:
                GLib.idle_add(self.show_error, f"Download failed: {e}")
        
        threading.Thread(target=do_download, daemon=True).start()
    
    def show_error(self, message):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Error",
            body=message
        )
        dialog.add_response("ok", "OK")
        dialog.present()
    
    def show_info(self, message):
        toast = Adw.Toast(title=message)
        toast.set_timeout(3)
        # Note: You'd need to add a toast overlay to show toasts
        print(f"Info: {message}")  # Fallback for now

def launch_sftp_file_manager_for_connection(
    app: Adw.Application,
    connection: "Connection",
    *,
    restrict_to_file_manager: bool = False,
    auto_connect: bool = True,
) -> SftpFileManager:
    """Create and present a :class:`SftpFileManager` configured for ``connection``."""

    window = SftpFileManager(
        app,
        connection=connection,
        restrict_to_file_manager=restrict_to_file_manager,
        auto_connect=auto_connect,
    )
    window.present()
    return window

class SftpFileManagerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.example.sftpfilemanager")
        self.connect("activate", self.on_activate)
    
    def on_activate(self, app):
        self.window = SftpFileManager(self)
        self.window.present()

if __name__ == "__main__":
    app = SftpFileManagerApp()
    app.run()