import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, GLib, Gio, GObject
import os
import stat
import threading
from pathlib import Path
import subprocess
import json
import tempfile
from datetime import datetime
import shutil

# Import your existing SSH utilities
try:
    from .ssh_password_exec import run_ssh_with_password, run_scp_with_password
    from .askpass_utils import get_ssh_env_with_askpass, ensure_key_in_agent, connect_ssh_with_key
    from .ssh_utils import build_connection_ssh_options
except ImportError:
    # Fallback imports for standalone testing
    try:
        from ssh_password_exec import run_ssh_with_password, run_scp_with_password
        from askpass_utils import get_ssh_env_with_askpass, ensure_key_in_agent, connect_ssh_with_key
        from ssh_utils import build_connection_ssh_options
    except ImportError:
        # Define minimal fallbacks if modules not available
        def run_ssh_with_password(host, user, password, **kwargs):
            raise NotImplementedError("ssh_password_exec module not available")
        def run_scp_with_password(host, user, password, **kwargs):
            raise NotImplementedError("ssh_password_exec module not available")
        def get_ssh_env_with_askpass(**kwargs):
            return os.environ.copy()
        def ensure_key_in_agent(key_path):
            return True
        def connect_ssh_with_key(host, username, key_path, command=None):
            raise NotImplementedError("askpass_utils module not available")
        def build_connection_ssh_options(connection, config=None, for_ssh_copy_id=False):
            return []

class SftpFileManager(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        
        self.connection_info = None  # Will store connection details
        self.connected = False
        self.current_local_path = Path.home()
        self.current_remote_path = "/"
        
        self.set_title("SFTP File Manager")
        self.set_default_size(1200, 800)
        
        self.setup_ui()
        self.refresh_local_view()
        
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
        
        # Main paned view
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
        
    def create_file_panel(self, title, is_local):
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
            download_btn = Gtk.Button(label="← Download")
            download_btn.add_css_class("suggested-action")
            download_btn.connect("clicked", self.download_file)
            download_btn.set_sensitive(False)
            action_box.append(download_btn)
            self.download_btn = download_btn
        
        return panel_box
    
    def show_connection_dialog(self, button):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Connect to SFTP Server"
        )
        
        # Create form with authentication method selection
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
        host_row.set_text("localhost")
        host_group.add(host_row)
        form_box.append(host_group)
        
        # Port
        port_group = Adw.PreferencesGroup()
        port_row = Adw.EntryRow()
        port_row.set_title("Port")
        port_row.set_text("22")
        port_group.add(port_row)
        form_box.append(port_group)
        
        # Username
        user_group = Adw.PreferencesGroup()
        user_row = Adw.EntryRow()
        user_row.set_title("Username")
        user_group.add(user_row)
        form_box.append(user_group)
        
        # Authentication method
        auth_group = Adw.PreferencesGroup()
        auth_group.set_title("Authentication")
        
        # Auth method selection
        auth_row = Adw.ComboRow()
        auth_row.set_title("Method")
        auth_model = Gtk.StringList()
        auth_model.append("SSH Key")
        auth_model.append("Password")
        auth_row.set_model(auth_model)
        auth_row.set_selected(0)
        auth_group.add(auth_row)
        
        # Key file selection (initially visible)
        key_row = Adw.ActionRow()
        key_row.set_title("SSH Key File")
        key_entry = Gtk.Entry()
        key_entry.set_text(str(Path.home() / ".ssh" / "id_rsa"))
        key_entry.set_hexpand(True)
        key_button = Gtk.Button(label="Browse...")
        key_button.connect("clicked", lambda b: self.browse_key_file(key_entry))
        key_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        key_box.append(key_entry)
        key_box.append(key_button)
        key_row.add_suffix(key_box)
        auth_group.add(key_row)
        
        # Password entry (initially hidden)
        pass_row = Adw.PasswordEntryRow()
        pass_row.set_title("Password")
        pass_row.set_visible(False)
        auth_group.add(pass_row)
        
        # Toggle visibility based on auth method
        def on_auth_changed(combo_row, *args):
            is_password = combo_row.get_selected() == 1
            key_row.set_visible(not is_password)
            pass_row.set_visible(is_password)
        
        auth_row.connect("notify::selected", on_auth_changed)
        
        form_box.append(auth_group)
        
        dialog.set_extra_child(form_box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("connect", "Connect")
        dialog.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        
        dialog.connect("response", lambda d, r: self.on_connection_response(
            d, r, host_row.get_text(), int(port_row.get_text() or 22), 
            user_row.get_text(), auth_row.get_selected(), key_entry.get_text(), pass_row.get_text()
        ))
        
        dialog.present()
    
    def browse_key_file(self, entry):
        """Open file chooser for SSH key selection"""
        dialog = Gtk.FileChooserDialog(
            title="Select SSH Key File",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN
        )
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL,
            "Open", Gtk.ResponseType.ACCEPT
        )
        
        # Set initial directory to ~/.ssh
        ssh_dir = Path.home() / ".ssh"
        if ssh_dir.exists():
            dialog.set_current_folder(Gio.File.new_for_path(str(ssh_dir)))
        
        dialog.connect("response", lambda d, r: self.on_key_file_selected(d, r, entry))
        dialog.present()
    
    def on_key_file_selected(self, dialog, response, entry):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file:
                entry.set_text(file.get_path())
        dialog.destroy()
    
    def on_connection_response(self, dialog, response, host, port, username, auth_method, key_file, password):
        dialog.close()
        if response == "connect":
            if auth_method == 0:  # SSH Key
                self.connect_with_key(host, port, username, key_file)
            else:  # Password
                self.connect_with_password(host, port, username, password)
    
    def connect_with_key(self, host, port, username, key_file):
        """Connect using SSH key authentication with your askpass system"""
        def do_connect():
            try:
                # Ensure key is loaded in ssh-agent using your askpass system
                if not ensure_key_in_agent(key_file):
                    GLib.idle_add(self.on_connection_error, f"Failed to load SSH key: {key_file}")
                    return
                
                # Test connection using your SSH utilities
                result = connect_ssh_with_key(host, username, key_file, "echo 'connection_test'")
                
                if result.returncode == 0 and "connection_test" in result.stdout:
                    self.connection_info = {
                        'host': host,
                        'port': port,
                        'username': username,
                        'auth_method': 'key',
                        'key_file': key_file
                    }
                    self.connected = True
                    GLib.idle_add(self.on_connected, host)
                else:
                    error_msg = result.stderr or "Key authentication failed"
                    GLib.idle_add(self.on_connection_error, error_msg)
                    
            except Exception as e:
                GLib.idle_add(self.on_connection_error, str(e))
        
        threading.Thread(target=do_connect, daemon=True).start()
        self.status_label.set_text("Connecting with SSH key...")

    def connect_with_password(self, host, port, username, password):
        """Connect using password authentication with your sshpass system"""
        def do_connect():
            try:
                # Test connection using your password SSH utilities
                result = run_ssh_with_password(
                    host, username, password,
                    port=port,
                    argv_tail=["echo", "connection_test"]
                )
                
                if result.returncode == 0 and "connection_test" in result.stdout:
                    self.connection_info = {
                        'host': host,
                        'port': port,
                        'username': username,
                        'auth_method': 'password',
                        'password': password
                    }
                    self.connected = True
                    GLib.idle_add(self.on_connected, host)
                else:
                    error_msg = result.stderr or "Password authentication failed"
                    GLib.idle_add(self.on_connection_error, error_msg)
                    
            except Exception as e:
                GLib.idle_add(self.on_connection_error, str(e))
        
        threading.Thread(target=do_connect, daemon=True).start()
        self.status_label.set_text("Connecting with password...")
    
    def on_connected(self, host):
        self.status_label.set_text(f"Connected to {host}")
        self.connect_btn.set_label("Disconnect")
        self.connect_btn.remove_css_class("suggested-action")
        self.connect_btn.add_css_class("destructive-action")
        self.connect_btn.disconnect_by_func(self.show_connection_dialog)
        self.connect_btn.connect("clicked", self.disconnect_sftp)
        
        self.remote_path_entry.set_sensitive(True)
        self.upload_btn.set_sensitive(True)
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
        self.connection_info = None
        self.connected = False
        
        self.status_label.set_text("Not connected")
        self.connect_btn.set_label("Connect to Server")
        self.connect_btn.remove_css_class("destructive-action")
        self.connect_btn.add_css_class("suggested-action")
        self.connect_btn.disconnect_by_func(self.disconnect_sftp)
        self.connect_btn.connect("clicked", self.show_connection_dialog)
        
        self.remote_path_entry.set_sensitive(False)
        self.upload_btn.set_sensitive(False)
        self.download_btn.set_sensitive(False)
        
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
        
        self.local_path_entry.set_text(str(self.current_local_path))
    
    def refresh_remote_view(self):
        if not self.connected:
            return
            
        def do_refresh():
            try:
                # Use your SSH utilities to get directory listing
                if self.connection_info['auth_method'] == 'key':
                    result = connect_ssh_with_key(
                        self.connection_info['host'],
                        self.connection_info['username'],
                        self.connection_info['key_file'],
                        f'ls -la --time-style="+%Y-%m-%d %H:%M" "{self.current_remote_path}"'
                    )
                else:  # password
                    result = run_ssh_with_password(
                        self.connection_info['host'],
                        self.connection_info['username'],
                        self.connection_info['password'],
                        port=self.connection_info['port'],
                        argv_tail=[f'ls -la --time-style="+%Y-%m-%d %H:%M" "{self.current_remote_path}"']
                    )
                
                if result.returncode == 0:
                    entries = self.parse_ls_output(result.stdout)
                    GLib.idle_add(self.update_remote_store, entries)
                else:
                    GLib.idle_add(self.show_error, f"Cannot read remote directory: {result.stderr}")
                    
            except Exception as e:
                GLib.idle_add(self.show_error, f"Cannot read remote directory: {e}")
        
        threading.Thread(target=do_refresh, daemon=True).start()
    
    def parse_ls_output(self, ls_output):
        """Parse ls -la output into file entries"""
        entries = []
        lines = ls_output.strip().split('\n')[1:]  # Skip total line
        
        for line in lines:
            if not line.strip():
                continue
                
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
                
            permissions = parts[0]
            size_str = parts[4]
            date_str = f"{parts[5]} {parts[6]}"
            filename = parts[8]
            
            # Skip . and .. entries
            if filename in ['.', '..']:
                continue
            
            # Determine if it's a directory
            is_dir = permissions.startswith('d')
            
            # Parse size
            try:
                size = int(size_str) if not is_dir else 0
            except ValueError:
                size = 0
            
            entries.append({
                'filename': filename,
                'size': size,
                'modified': date_str,
                'is_dir': is_dir
            })
        
        # Sort: directories first, then files
        entries.sort(key=lambda x: (not x['is_dir'], x['filename'].lower()))
        return entries
    
    def update_remote_store(self, entries):
        self.remote_store.clear()
        for entry in entries:
            size = self.format_size(entry['size']) if not entry['is_dir'] else ""
            file_type = "Directory" if entry['is_dir'] else "File"
            
            self.remote_store.append([
                entry['filename'], 
                size, 
                entry['modified'], 
                file_type, 
                entry['is_dir']
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
        if not self.connected:
            return
            
        if path == "..":
            new_path = str(Path(self.current_remote_path).parent)
        else:
            new_path = str(Path(self.current_remote_path) / path)
        
        def test_path():
            try:
                # Test if path exists using your SSH utilities
                if self.connection_info['auth_method'] == 'key':
                    result = connect_ssh_with_key(
                        self.connection_info['host'],
                        self.connection_info['username'],
                        self.connection_info['key_file'],
                        f'test -d "{new_path}" && echo "OK"'
                    )
                else:  # password
                    result = run_ssh_with_password(
                        self.connection_info['host'],
                        self.connection_info['username'],
                        self.connection_info['password'],
                        port=self.connection_info['port'],
                        argv_tail=[f'test -d "{new_path}" && echo "OK"']
                    )
                
                if result.returncode == 0 and "OK" in result.stdout:
                    self.current_remote_path = new_path
                    GLib.idle_add(self.refresh_remote_view)
                else:
                    GLib.idle_add(self.show_error, f"Cannot navigate to {new_path}")
                    
            except Exception as e:
                GLib.idle_add(self.show_error, f"Cannot navigate to {new_path}: {e}")
        
        threading.Thread(target=test_path, daemon=True).start()
    
    def on_local_path_changed(self, entry):
        path = Path(entry.get_text())
        if path.exists() and path.is_dir():
            self.current_local_path = path
            self.refresh_local_view()
    
    def on_remote_path_changed(self, entry):
        if not self.connected:
            return
        
        path = entry.get_text()
        
        def test_path():
            try:
                # Test if path exists using your SSH utilities
                if self.connection_info['auth_method'] == 'key':
                    result = connect_ssh_with_key(
                        self.connection_info['host'],
                        self.connection_info['username'],
                        self.connection_info['key_file'],
                        f'test -d "{path}" && echo "OK"'
                    )
                else:  # password
                    result = run_ssh_with_password(
                        self.connection_info['host'],
                        self.connection_info['username'],
                        self.connection_info['password'],
                        port=self.connection_info['port'],
                        argv_tail=[f'test -d "{path}" && echo "OK"']
                    )
                
                if result.returncode == 0 and "OK" in result.stdout:
                    self.current_remote_path = path
                    GLib.idle_add(self.refresh_remote_view)
                else:
                    GLib.idle_add(self.show_error, f"Cannot navigate to {path}")
                    GLib.idle_add(entry.set_text, self.current_remote_path)
                    
            except Exception as e:
                GLib.idle_add(self.show_error, f"Cannot navigate to {path}: {e}")
                GLib.idle_add(entry.set_text, self.current_remote_path)
        
        threading.Thread(target=test_path, daemon=True).start()
    
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
                if self.connection_info['auth_method'] == 'key':
                    # For key-based auth, use scp with proper SSH options
                    env = get_ssh_env_with_askpass("force")

                    cmd = [
                        'scp',
                        '-o', 'IdentitiesOnly=yes',
                        '-i', self.connection_info['key_file'],
                        '-P', str(self.connection_info['port']),
                        '-o', 'StrictHostKeyChecking=accept-new',
                        str(local_path),
                        f"{self.connection_info['username']}@{self.connection_info['host']}:{remote_path}"
                    ]

                    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
                else:
                    # For password auth, use your scp utility
                    result = run_scp_with_password(
                        self.connection_info['host'],
                        self.connection_info['username'],
                        self.connection_info['password'],
                        [str(local_path)],
                        str(Path(self.current_remote_path)),
                        port=self.connection_info['port']
                    )

                if result.returncode == 0:
                    GLib.idle_add(self.refresh_remote_view)
                    GLib.idle_add(self.show_info, f"Uploaded {filename}")
                else:
                    GLib.idle_add(self.show_error, f"Upload failed: {result.stderr}")

            except subprocess.TimeoutExpired:
                GLib.idle_add(self.show_error, f"Upload timeout for {filename}")
            except Exception as e:
                GLib.idle_add(self.show_error, f"Upload failed: {e}")

        upload_thread = threading.Thread(target=do_upload, daemon=True)
        upload_thread.start()
        return
    
    def download_file(self, button):
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
                cmd = [
                    'scp',
                    '-o', 'BatchMode=yes',
                    '-o', 'ConnectTimeout=10', 
                    '-o', 'StrictHostKeyChecking=no',
                    '-P', str(self.ssh_connection['port']),
                    f"{self.ssh_connection['username']}@{self.ssh_connection['host']}:{remote_path}",
                    str(local_path)
                ]
                
                if self.ssh_connection.get('password'):
                    cmd = ['sshpass', '-p', self.ssh_connection['password']] + cmd
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                
                if result.returncode == 0:
                    GLib.idle_add(self.refresh_local_view)
                    GLib.idle_add(self.show_info, f"Downloaded {filename}")
                else:
                    GLib.idle_add(self.show_error, f"Download failed: {result.stderr}")
                    
            except subprocess.TimeoutExpired:
                GLib.idle_add(self.show_error, f"Download timeout for {filename}")
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