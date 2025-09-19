"""In-app two-pane SFTP file manager window.

This module provides a libadwaita based window that mimics a traditional
file manager experience while running entirely inside sshPilot.  It exposes
two panes that can each browse an independent remote path.  All filesystem
operations are executed on background worker threads to keep the UI
responsive and results are marshalled back to the main GTK loop using
``GLib.idle_add``.  The implementation intentionally favours clarity over raw
performance – the goal is to provide a dependable fallback for situations
where a native GVFS/GIO based file manager is not available (e.g. Flatpak
deployments).

The window follows the GNOME HIG by composing libadwaita widgets such as
``Adw.ToolbarView`` and ``Adw.HeaderBar``.  Each pane exposes both list and
grid representations of directory contents, navigation controls, progress
indicators and toast based feedback.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import posixpath
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional, Tuple


import paramiko
from gi.repository import Adw, Gio, GLib, GObject, Gdk, Gtk, Pango


# ---------------------------------------------------------------------------
# Utility data structures


@dataclasses.dataclass
class FileEntry:
    """Light weight description of a directory entry."""

    name: str
    is_dir: bool
    size: int
    modified: float


class _MainThreadDispatcher:
    """Helper that marshals callbacks back to the GTK main loop."""

    @staticmethod
    def dispatch(func: Callable, *args, **kwargs) -> None:
        GLib.idle_add(lambda: func(*args, **kwargs))


# ---------------------------------------------------------------------------
# Asynchronous SFTP layer


class AsyncSFTPManager(GObject.GObject):
    """Small wrapper around :mod:`paramiko` that performs operations in
    worker threads.

    The class exposes a queue of operations and emits signals when important
    events happen.  Tests can monkeypatch :class:`paramiko.SSHClient` to avoid
    talking to a real server.
    """

    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, tuple()),
        "connection-error": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float, str)),
        "operation-error": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),
        "directory-loaded": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, object),
        ),
    }

    def __init__(
        self,
        host: str,
        username: str,
        port: int = 22,
        password: Optional[str] = None,
        *,
        dispatcher: Callable[[Callable, tuple, dict], None] | None = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._dispatcher = dispatcher or (
            lambda cb, args=(), kwargs=None: _MainThreadDispatcher.dispatch(
                cb, *args, **(kwargs or {})
            )
        )
        self._lock = threading.Lock()

    # -- connection -----------------------------------------------------

    def connect_to_server(self) -> None:
        self._submit(
            self._connect_impl,
            on_success=lambda *_: self.emit("connected"),
            on_error=lambda exc: self.emit("connection-error", str(exc)),
        )

    def close(self) -> None:
        with self._lock:
            if self._sftp is not None:
                self._sftp.close()
                self._sftp = None
            if self._client is not None:
                self._client.close()
                self._client = None
        self._executor.shutdown(wait=False)

    # -- helpers --------------------------------------------------------

    def _submit(
        self,
        func: Callable[[], object],
        *,
        on_success: Optional[Callable[[object], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> Future:
        future = self._executor.submit(func)

        def _done(fut: Future) -> None:
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - errors handled uniformly
                if on_error:
                    self._dispatcher(on_error, (exc,), {})
                else:
                    self._dispatcher(self.emit, ("operation-error", str(exc)), {})
            else:
                if on_success:
                    self._dispatcher(on_success, (result,), {})

        future.add_done_callback(_done)
        return future

    # -- actual work ----------------------------------------------------

    def _connect_impl(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self._host,
            username=self._username,
            password=self._password,
            port=self._port,
            allow_agent=True,
            look_for_keys=True,
            timeout=15,
        )
        sftp = client.open_sftp()
        with self._lock:
            self._client = client
            self._sftp = sftp

    # -- public operations ----------------------------------------------

    def listdir(self, path: str) -> None:
        def _impl() -> Tuple[str, List[FileEntry]]:
            entries: List[FileEntry] = []
            assert self._sftp is not None
            
            # Expand ~ to user's home directory
            expanded_path = path
            if path == "~" or path.startswith("~/"):
                # Use the most reliable method to get home directory
                # The SFTP normalize method with "." should give us the initial directory
                # which is typically the user's home directory
                try:
                    if path == "~":
                        # For just ~, resolve to the absolute home directory
                        expanded_path = self._sftp.normalize(".")
                    else:
                        # For ~/subpath, we need to resolve the home directory first
                        # Try to get the actual home directory path
                        home_path = self._sftp.normalize(".")
                        expanded_path = home_path + path[1:]  # Replace ~ with home_path
                except Exception:
                    # If normalize fails, try common patterns
                    try:
                        possible_homes = [
                            f"/home/{self._username}",
                            f"/Users/{self._username}",  # macOS
                            f"/export/home/{self._username}",  # Solaris
                        ]
                        for possible_home in possible_homes:
                            try:
                                # Test if this directory exists
                                self._sftp.listdir_attr(possible_home)
                                if path == "~":
                                    expanded_path = possible_home
                                else:
                                    expanded_path = possible_home + path[1:]
                                break
                            except Exception:
                                continue
                        else:
                            # Final fallback
                            expanded_path = f"/home/{self._username}" + (path[1:] if path.startswith("~/") else "")
                    except Exception:
                        # Ultimate fallback
                        expanded_path = f"/home/{self._username}" + (path[1:] if path.startswith("~/") else "")
            
            for attr in self._sftp.listdir_attr(expanded_path):
                entries.append(
                    FileEntry(
                        name=attr.filename,
                        is_dir=stat_isdir(attr),
                        size=attr.st_size,
                        modified=attr.st_mtime,
                    )
                )
            return expanded_path, entries

        self._submit(
            _impl,
            on_success=lambda result: self.emit("directory-loaded", *result),
            on_error=lambda exc: self.emit("operation-error", str(exc)),
        )

    def mkdir(self, path: str) -> Future:
        return self._submit(
            lambda: self._sftp.mkdir(path),
            on_success=lambda *_: self.listdir(os.path.dirname(path) or "/"),
        )

    def remove(self, path: str) -> Future:
        def _impl() -> None:
            assert self._sftp is not None
            try:
                self._sftp.remove(path)
            except IOError:
                # fallback to directory remove
                for entry in self._sftp.listdir(path):
                    self.remove(os.path.join(path, entry))
                self._sftp.rmdir(path)

        parent = os.path.dirname(path) or "/"
        return self._submit(_impl, on_success=lambda *_: self.listdir(parent))

    def rename(self, source: str, target: str) -> Future:
        return self._submit(
            lambda: self._sftp.rename(source, target),
            on_success=lambda *_: self.listdir(os.path.dirname(target) or "/"),
        )

    def download(self, source: str, destination: pathlib.Path) -> Future:
        destination.parent.mkdir(parents=True, exist_ok=True)

        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Starting download…")
            self._sftp.get(source, str(destination))
            self.emit("progress", 1.0, "Download complete")

        return self._submit(_impl)

    def upload(self, source: pathlib.Path, destination: str) -> Future:
        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Starting upload…")
            self._sftp.put(str(source), destination)
            self.emit("progress", 1.0, "Upload complete")

        return self._submit(_impl)

    # Helpers for directory recursion – these are intentionally simplistic
    # and rely on Paramiko's high level API.

    def download_directory(self, source: str, destination: pathlib.Path) -> Future:
        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Preparing download…")
            for root, dirs, files in walk_remote(self._sftp, source):
                rel_root = os.path.relpath(root, source)
                target_root = destination / rel_root
                target_root.mkdir(parents=True, exist_ok=True)
                for name in files:
                    self._sftp.get(os.path.join(root, name), str(target_root / name))
            self.emit("progress", 1.0, "Directory downloaded")

        return self._submit(_impl)

    def upload_directory(self, source: pathlib.Path, destination: str) -> Future:
        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Preparing upload…")
            for root, dirs, files in os.walk(source):
                rel_root = os.path.relpath(root, str(source))
                remote_root = (
                    destination if rel_root == "." else os.path.join(destination, rel_root)
                )
                try:
                    self._sftp.mkdir(remote_root)
                except IOError:
                    pass
                for name in files:
                    self._sftp.put(
                        os.path.join(root, name), os.path.join(remote_root, name)
                    )
            self.emit("progress", 1.0, "Directory uploaded")

        return self._submit(_impl)


def stat_isdir(attr: paramiko.SFTPAttributes) -> bool:
    """Return ``True`` when the attribute represents a directory."""

    return bool(attr.st_mode & 0o40000)


def walk_remote(sftp: paramiko.SFTPClient, root: str) -> Iterable[Tuple[str, List[str], List[str]]]:
    """Yield a remote directory tree similar to :func:`os.walk`."""

    dirs: List[str] = []
    files: List[str] = []
    for entry in sftp.listdir_attr(root):
        if stat_isdir(entry):
            dirs.append(entry.filename)
        else:
            files.append(entry.filename)
    yield root, dirs, files
    for directory in dirs:
        new_root = os.path.join(root, directory)
        yield from walk_remote(sftp, new_root)


# ---------------------------------------------------------------------------
# UI widgets


class PathEntry(Gtk.Entry):
    """Simple entry used for the editable pathbar."""

    def __init__(self) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_placeholder_text("/remote/path")


class ViewToggle(Gtk.ToggleButton):
    def __init__(self, icon_name: str, tooltip: str) -> None:
        super().__init__()
        self.set_icon_name(icon_name)
        self.set_valign(Gtk.Align.CENTER)
        self.set_tooltip_text(tooltip)


class PaneControls(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_valign(Gtk.Align.CENTER)
        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.up_button = Gtk.Button.new_from_icon_name("go-up-symbolic")
        self.new_folder_button = Gtk.Button.new_from_icon_name("folder-new-symbolic")
        self.upload_button = Gtk.Button(label="Upload")
        self.download_button = Gtk.Button(label="Download")
        for widget in (
            self.back_button,
            self.up_button,
            self.new_folder_button,
            self.upload_button,
            self.download_button,
        ):
            widget.set_valign(Gtk.Align.CENTER)
        for widget in (self.back_button, self.up_button, self.new_folder_button):
            widget.add_css_class("flat")
        self.append(self.back_button)
        self.append(self.up_button)
        self.append(self.new_folder_button)
        self.append(self.upload_button)
        self.append(self.download_button)


class PaneToolbar(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        
        # Create the actual header bar
        self._header_bar = Adw.HeaderBar()
        self._header_bar.set_title_widget(Gtk.Label(label=""))
        
        self.controls = PaneControls()
        self.path_entry = PathEntry()
        self.list_toggle = ViewToggle("view-list-symbolic", "List view")
        self.grid_toggle = ViewToggle("view-grid-symbolic", "Icon view")
        self.grid_toggle.set_group(self.list_toggle)
        self.list_toggle.set_active(True)
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        action_box.append(self.list_toggle)
        action_box.append(self.grid_toggle)
        self._header_bar.pack_start(self.controls)
        self._header_bar.pack_start(self.path_entry)
        self._header_bar.pack_end(action_box)
        
        self.append(self._header_bar)
    
    def get_header_bar(self):
        """Get the actual header bar for toolbar view."""
        return self._header_bar


class FilePane(Gtk.Box):
    """Represents a single pane in the manager."""

    __gsignals__ = {
        "path-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "request-operation": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, object),
        ),
    }

    def __init__(self, label: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.toolbar = PaneToolbar()
        self.toolbar.get_header_bar().get_title_widget().set_text(label)
        self.append(self.toolbar)

        self._is_remote = label.lower() == "remote"

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        self._list_store = Gio.ListStore(item_type=Gtk.StringObject)
        self._selection_model = Gtk.SingleSelection(model=self._list_store)

        list_factory = Gtk.SignalListItemFactory()
        list_factory.connect("setup", self._on_list_setup)
        list_factory.connect("bind", self._on_list_bind)
        list_view = Gtk.ListView(model=self._selection_model, factory=list_factory)
        list_view.add_css_class("rich-list")
        # Navigate on row activation (double click / Enter)
        self._list_view = list_view
        list_view.connect("activate", self._on_list_activate)
        
        # Wrap list view in a scrolled window for proper scrolling
        list_scrolled = Gtk.ScrolledWindow()
        list_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scrolled.set_child(list_view)

        grid_factory = Gtk.SignalListItemFactory()
        grid_factory.connect("setup", self._on_grid_setup)
        grid_factory.connect("bind", self._on_grid_bind)
        grid_view = Gtk.GridView(
            model=self._selection_model,
            factory=grid_factory,
            max_columns=6,
        )
        grid_view.add_css_class("iconview")
        self._grid_view = grid_view
        # Navigate on grid item activation (double click / Enter)
        grid_view.connect("activate", self._on_grid_activate)
        
        # Wrap grid view in a scrolled window for proper scrolling
        grid_scrolled = Gtk.ScrolledWindow()
        grid_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        grid_scrolled.set_child(grid_view)

        self._stack.add_named(list_scrolled, "list")
        self._stack.add_named(grid_scrolled, "grid")

        overlay = Adw.ToastOverlay()
        overlay.set_child(self._stack)
        self._overlay = overlay
        self.append(overlay)

        self.toolbar.list_toggle.connect("toggled", self._on_view_toggle, "list")
        self.toolbar.grid_toggle.connect("toggled", self._on_view_toggle, "grid")
        self.toolbar.path_entry.connect("activate", self._on_path_entry)
        # Wire navigation buttons
        self.toolbar.controls.up_button.connect("clicked", self._on_up_clicked)
        self.toolbar.controls.back_button.connect("clicked", self._on_back_clicked)
        self.toolbar.controls.new_folder_button.connect(
            "clicked", lambda *_: self.emit("request-operation", "mkdir", None)
        )
        if self._is_remote:
            self.toolbar.controls.upload_button.connect("clicked", self._on_upload_clicked)
            self.toolbar.controls.download_button.set_visible(False)
        else:
            self.toolbar.controls.upload_button.set_visible(False)
            self.toolbar.controls.download_button.connect("clicked", self._on_download_clicked)

        self._history: List[str] = []
        self._current_path = "/"
        self._entries: List[FileEntry] = []
        self._suppress_history_push: bool = False
        self._selection_model.connect("selection-changed", self._on_selection_changed)

        self._menu_actions: Dict[str, Gio.SimpleAction] = {}
        self._menu_action_group = Gio.SimpleActionGroup()
        self.insert_action_group("pane", self._menu_action_group)
        self._menu_popover: Gtk.PopoverMenu = self._create_menu_model()
        self._add_context_controller(list_view)
        self._add_context_controller(grid_view)

        # Drag and drop controllers – these provide the visual affordance and
        # forward requests to the window which understands the context.
        drop_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop_target.connect("accept", lambda *_: True)
        drop_target.connect("drop", self._on_drop)
        self.add_controller(drop_target)
        self._update_menu_state()

    # -- callbacks ------------------------------------------------------

    def _on_view_toggle(self, button: Gtk.ToggleButton, name: str) -> None:
        if button.get_active():
            self._stack.set_visible_child_name(name)

    def _on_path_entry(self, entry: Gtk.Entry) -> None:
        self.emit("path-changed", entry.get_text() or "/")

    def _on_list_setup(self, factory: Gtk.SignalListItemFactory, item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        icon.set_valign(Gtk.Align.CENTER)
        label = Gtk.Label(xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_max_width_chars(40)
        label.set_hexpand(True)
        box.append(icon)
        box.append(label)
        item.set_child(box)

    def _on_list_bind(self, factory, item):
        box = item.get_child()
        label = box.get_last_child()
        icon = box.get_first_child()
        value = item.get_item().get_string()
        label.set_text(value)
        label.set_tooltip_text(value)
        # Choose icon based on whether value ends with '/'
        if value.endswith('/'):
            icon.set_from_icon_name("folder-symbolic")
        else:
            icon.set_from_icon_name("text-x-generic-symbolic")

    def _on_grid_setup(self, factory, item):
        button = Gtk.Button()
        button.set_has_frame(False)
        image = Gtk.Image.new_from_icon_name("folder-symbolic")
        button.set_child(image)
        item.set_child(button)

    def _on_grid_bind(self, factory, item):
        # Grid view uses the same icon for now but honours the entry name as
        # tooltip so users can differentiate.
        button = item.get_child()
        value = item.get_item().get_string()
        button.set_tooltip_text(value)
        # Update the image icon based on type
        image = button.get_child()
        if value.endswith('/'):
            image.set_from_icon_name("folder-symbolic")
        else:
            image.set_from_icon_name("text-x-generic-symbolic")

    def _on_selection_changed(self, model, position, n_items):
        self._update_menu_state()

    def _create_menu_model(self) -> Gtk.PopoverMenu:
        if not self._menu_actions:
            def _add_action(name: str, callback: Callable[[], None]) -> None:
                action = Gio.SimpleAction.new(name, None)

                def _on_activate(_action: Gio.SimpleAction, _param: Optional[GLib.Variant]) -> None:
                    callback()

                action.connect("activate", _on_activate)
                self._menu_action_group.add_action(action)
                self._menu_actions[name] = action

            _add_action("download", self._on_menu_download)
            _add_action("upload", self._on_menu_upload)
            _add_action("rename", lambda: self._emit_entry_operation("rename"))
            _add_action("delete", lambda: self._emit_entry_operation("delete"))
            _add_action("new_folder", lambda: self.emit("request-operation", "mkdir", None))

        menu_model = Gio.Menu()
        menu_model.append("Download", "pane.download")
        menu_model.append("Upload…", "pane.upload")

        manage_section = Gio.Menu()
        manage_section.append("Rename…", "pane.rename")
        manage_section.append("Delete", "pane.delete")
        menu_model.append_section(None, manage_section)
        menu_model.append("New Folder", "pane.new_folder")

        popover = Gtk.PopoverMenu.new_from_model(menu_model)
        popover.set_has_arrow(True)
        return popover

    def _add_context_controller(self, widget: Gtk.Widget) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(Gdk.BUTTON_SECONDARY)

        def _on_pressed(_gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
            self._show_context_menu(widget, x, y)

        gesture.connect("pressed", _on_pressed)
        widget.add_controller(gesture)

        long_press = Gtk.GestureLongPress()

        def _on_long_press(_gesture: Gtk.GestureLongPress, x: float, y: float) -> None:
            self._show_context_menu(widget, x, y)

        long_press.connect("pressed", _on_long_press)
        widget.add_controller(long_press)

    def _show_context_menu(self, widget: Gtk.Widget, x: float, y: float) -> None:
        self._update_selection_for_menu(widget, x, y)
        self._update_menu_state()
        try:
            widget.grab_focus()
        except Exception:
            pass
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._menu_popover.set_parent(widget)
        self._menu_popover.set_pointing_to(rect)
        self._menu_popover.popup()

    def _update_selection_for_menu(self, widget: Gtk.Widget, x: float, y: float) -> None:
        position = Gtk.INVALID_LIST_POSITION
        int_x, int_y = int(x), int(y)

        if widget is self._list_view:
            item = self._list_view.get_item_at_pos(int_x, int_y)
            if item is not None:
                position = item.get_position()
        elif widget is self._grid_view:
            item = self._grid_view.get_item_at_pos(int_x, int_y)
            if item is not None:
                position = item.get_position()

        self._selection_model.set_selected(position)

    def _update_menu_state(self) -> None:
        entry = self.get_selected_entry()
        has_selection = entry is not None

        def _set_enabled(name: str, enabled: bool) -> None:
            action = self._menu_actions.get(name)
            if action is not None:
                action.set_enabled(enabled)

        window = self.get_root()
        local_has_selection = False
        if self._is_remote and isinstance(window, FileManagerWindow):
            local_pane = getattr(window, "_left_pane", None)
            if isinstance(local_pane, FilePane):
                local_has_selection = local_pane.get_selected_entry() is not None

        _set_enabled("download", self._is_remote and has_selection)
        _set_enabled("upload", self._is_remote and local_has_selection)
        _set_enabled("rename", has_selection)
        _set_enabled("delete", has_selection)
        _set_enabled("new_folder", True)

    def _emit_entry_operation(self, action: str) -> None:
        entry = self.get_selected_entry()
        if entry is None:
            if action != "new_folder":
                self.show_toast("Select an item first")
            return
        payload = {"entry": entry, "directory": self._current_path}
        self.emit("request-operation", action, payload)

    def _on_menu_download(self) -> None:
        if not self._is_remote:
            return
        self._on_download_clicked(None)

    def _on_menu_upload(self) -> None:
        self._on_upload_clicked(None)

    def _on_drop(self, target: Gtk.DropTarget, value: Gio.File, x: float, y: float):
        self.emit("request-operation", "upload", value)
        return True

    def get_selected_entry(self) -> Optional[FileEntry]:
        index = self._selection_model.get_selected()
        if index is None or index < 0 or index >= len(self._entries):
            return None
        return self._entries[index]

    def _on_upload_clicked(self, _button: Gtk.Button) -> None:
        window = self.get_root()
        if not isinstance(window, FileManagerWindow):
            return

        local_pane = getattr(window, "_left_pane", None)
        if not isinstance(local_pane, FilePane):
            self.show_toast("Local pane is unavailable")
            return

        destination_pane: Optional[FilePane]
        if self._is_remote:
            destination_pane = self
        else:
            destination_pane = getattr(window, "_right_pane", None)
            if not isinstance(destination_pane, FilePane) or not destination_pane._is_remote:
                destination_pane = None

        if destination_pane is None:
            self.show_toast("Remote pane is unavailable")
            return

        entry = local_pane.get_selected_entry()
        if entry is None:
            self.show_toast("Select an item in the local pane to upload")
            return

        base_dir = window._normalize_local_path(local_pane.toolbar.path_entry.get_text())
        source_path = pathlib.Path(os.path.join(base_dir, entry.name))
        if not source_path.exists():
            self.show_toast("Selected local item is not accessible")
            return

        destination = destination_pane.toolbar.path_entry.get_text() or "/"
        payload = {"paths": [source_path], "destination": destination}
        self.emit("request-operation", "upload", payload)


    def _on_download_clicked(self, _button: Gtk.Button) -> None:
        entry = self.get_selected_entry()
        if entry is None:
            self.show_toast("Select an item to download")
            return

        window = self.get_root()
        if not isinstance(window, FileManagerWindow):
            return

        local_pane = getattr(window, "_left_pane", None)
        if local_pane is None:
            self.show_toast("Local pane is unavailable")
            return

        destination_root = window._normalize_local_path(local_pane.toolbar.path_entry.get_text())
        if not os.path.isdir(destination_root):
            self.show_toast("Local destination is not accessible")
            return

        payload = {
            "source": posixpath.join(self._current_path or "/", entry.name),
            "is_dir": entry.is_dir,
            "destination": pathlib.Path(destination_root),
        }
        self.emit("request-operation", "download", payload)

    @staticmethod
    def _dialog_dismissed(error: GLib.Error) -> bool:
        dialog_error = getattr(Gtk, "DialogError", None)
        if dialog_error is not None and error.matches(dialog_error, dialog_error.DISMISSED):
            return True
        return error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED)

    # -- public API -----------------------------------------------------

    def show_entries(self, path: str, entries: Iterable[FileEntry]) -> None:
        self._list_store.remove_all()
        # Store entries so we can determine directories on activation
        self._entries = list(entries)
        for entry in self._entries:
            suffix = "/" if entry.is_dir else ""
            self._list_store.append(Gtk.StringObject.new(entry.name + suffix))
        self._current_path = path
        self.toolbar.path_entry.set_text(path)
        self._selection_model.unselect_all()
        self._update_menu_state()

    # -- navigation helpers --------------------------------------------

    def _on_list_activate(self, _list_view: Gtk.ListView, position: int) -> None:
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            if entry.is_dir:
                self.emit("path-changed", os.path.join(self._current_path, entry.name))

    def _on_grid_activate(self, _grid_view: Gtk.GridView, position: int) -> None:
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            if entry.is_dir:
                self.emit("path-changed", os.path.join(self._current_path, entry.name))

    def _on_up_clicked(self, _button) -> None:
        parent = os.path.dirname(self._current_path.rstrip('/')) or '/'
        # Avoid navigating past root repeatedly
        if parent != self._current_path:
            self.emit("path-changed", parent)

    def _on_back_clicked(self, _button) -> None:
        prev = self.pop_history()
        if prev:
            # Suppress history push for back navigation
            self._suppress_history_push = True
            self.emit("path-changed", prev)

    def push_history(self, path: str) -> None:
        if self._history and self._history[-1] == path:
            return
        self._history.append(path)

    def pop_history(self) -> Optional[str]:
        if len(self._history) > 1:
            self._history.pop()
            return self._history[-1]
        return None

    def show_toast(self, text: str) -> None:
        self._overlay.add_toast(Adw.Toast.new(text))


class FileManagerWindow(Adw.Window):
    """Top-level window hosting two :class:`FilePane` instances."""

    def __init__(
        self,
        application: Adw.Application,
        *,
        host: str,
        username: str,
        port: int = 22,
        initial_path: str = "~",
    ) -> None:
        super().__init__(title="Remote Files")
        # Set default and minimum sizes following GNOME HIG
        self.set_default_size(1000, 640)
        # Set minimum size to ensure usability (GNOME HIG recommends minimum 360px width)
        self.set_size_request(600, 400)
        # Ensure window is resizable (this is the default, but being explicit)
        self.set_resizable(True)
        # Ensure window decorations are shown (minimize, maximize, close buttons)
        self.set_decorated(True)

        # Use ToolbarView like other Adw.Window instances
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)
        
        # Create header bar with window controls
        header_bar = Adw.HeaderBar()
        header_bar.set_title_widget(Gtk.Label(label=f"{username}@{host}"))
        # Enable window controls (minimize, maximize, close) following GNOME HIG
        header_bar.set_show_start_title_buttons(True)
        header_bar.set_show_end_title_buttons(True)
        toolbar_view.add_top_bar(header_bar)

        self._toast_overlay = Adw.ToastOverlay()
        toolbar_view.set_content(self._toast_overlay)
        self._progress_toast: Optional[Adw.Toast] = None
        self._connection_error_reported = False

        panes = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        panes.set_wide_handle(True)
        # Set position to split evenly by default (50%)
        panes.set_position(500)  # This will be adjusted when window is resized
        # Enable resizing and shrinking for both panes following GNOME HIG
        panes.set_resize_start_child(True)
        panes.set_resize_end_child(True)
        panes.set_shrink_start_child(False)
        panes.set_shrink_end_child(False)
        self._toast_overlay.set_child(panes)

        self._left_pane = FilePane("Local")
        self._right_pane = FilePane("Remote")
        panes.set_start_child(self._left_pane)
        panes.set_end_child(self._right_pane)
        
        # Store reference to panes for resize handling
        self._panes = panes
        
        # Connect to size-allocate to maintain proportional split
        self.connect("notify::default-width", self._on_window_resize)

        self._manager = AsyncSFTPManager(host, username, port)
        self._manager.connect("connected", self._on_connected)
        self._manager.connect("connection-error", self._on_connection_error)
        self._manager.connect("progress", self._on_progress)
        self._manager.connect("operation-error", self._on_operation_error)
        self._manager.connect("directory-loaded", self._on_directory_loaded)
        self._manager.connect_to_server()

        for pane in (self._left_pane, self._right_pane):
            pane.connect("path-changed", self._on_path_changed, pane)
            pane.connect("request-operation", self._on_request_operation, pane)

        # Initialize panes: left is LOCAL home, right is REMOTE home (~)
        self._pending_paths: Dict[FilePane, Optional[str]] = {
            self._left_pane: None,
            self._right_pane: initial_path,
        }

        # Prime the left (local) pane immediately with local home directory
        try:
            local_home = os.path.expanduser("~")
            self._load_local(local_home)
            self._left_pane.push_history(local_home)
        except Exception as exc:
            self._left_pane.show_toast(f"Failed to load local home: {exc}")

        self._show_progress(0.1, "Connecting…")

    # -- signal handlers ------------------------------------------------

    def _clear_progress_toast(self) -> None:
        if self._progress_toast is not None:
            self._progress_toast.dismiss()
            self._progress_toast = None

    def _show_progress(self, fraction: float, message: str) -> None:
        del fraction  # Fraction reserved for future progress UI enhancements
        self._clear_progress_toast()
        toast = Adw.Toast.new(message)
        self._toast_overlay.add_toast(toast)
        self._progress_toast = toast

    def _on_connected(self, *_args) -> None:
        self._show_progress(0.4, "Connected")
        # Trigger directory loads for all panes that have a pending initial path
        for pane, pending in self._pending_paths.items():
            if pending:
                self._manager.listdir(pending)


    def _on_progress(self, _manager, fraction: float, message: str) -> None:
        self._show_progress(fraction, message)

    def _on_operation_error(self, _manager, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_priority(Adw.ToastPriority.HIGH)
        self._toast_overlay.add_toast(toast)

    def _on_connection_error(self, _manager, message: str) -> None:
        self._clear_progress_toast()
        if self._connection_error_reported:
            return
        toast = Adw.Toast.new(message or "Connection failed")
        toast.set_priority(Adw.ToastPriority.HIGH)
        self._toast_overlay.add_toast(toast)
        self._connection_error_reported = True

    def _on_directory_loaded(
        self, _manager, path: str, entries: Iterable[FileEntry]
    ) -> None:
        # Prefer the pane explicitly waiting for this exact path; otherwise
        # assign to the next pane that still has a pending request. This makes
        # initial dual loads robust even if the backend normalizes paths.
        target = next((pane for pane, pending in self._pending_paths.items() if pending == path), None)
        if target is None:
            target = next((pane for pane, pending in self._pending_paths.items() if pending is not None), self._left_pane)
        # Clear the pending flag for the resolved pane
        self._pending_paths[target] = None

        target.show_entries(path, entries)
        target.push_history(path)
        target.show_toast(f"Loaded {path}")

    # -- local filesystem helpers ---------------------------------------

    def _load_local(self, path: str) -> None:
        """Load local directory contents into the left pane.

        This is a synchronous operation using the local filesystem.
        """
        try:
            path = os.path.expanduser(path or "~")
            if not os.path.isabs(path):
                path = os.path.abspath(path)
            if not os.path.isdir(path):
                raise NotADirectoryError(f"Not a directory: {path}")

            entries: List[FileEntry] = []
            with os.scandir(path) as it:
                for dirent in it:
                    try:
                        stat = dirent.stat(follow_symlinks=False)
                        entries.append(
                            FileEntry(
                                name=dirent.name,
                                is_dir=dirent.is_dir(follow_symlinks=False),
                                size=getattr(stat, "st_size", 0) or 0,
                                modified=getattr(stat, "st_mtime", 0.0) or 0.0,
                            )
                        )
                    except Exception:
                        # Skip entries we cannot stat
                        continue

            # Show results in the left pane
            self._left_pane.show_entries(path, entries)
        except Exception as exc:
            self._left_pane.show_toast(str(exc))

    def _on_path_changed(self, pane: FilePane, path: str, user_data=None) -> None:
        # Route local vs remote browsing
        if pane is self._left_pane:
            # Local pane: expand ~ and navigate local filesystem
            local_path = os.path.expanduser(path) if path.startswith("~") else path
            if not local_path:
                local_path = os.path.expanduser("~")
            try:
                self._load_local(local_path)
                # Only push history if not triggered by Back
                if getattr(pane, "_suppress_history_push", False):
                    pane._suppress_history_push = False
                else:
                    pane.push_history(local_path)
            except Exception as exc:
                pane.show_toast(str(exc))
        else:
            # Remote pane: use SFTP manager
            self._pending_paths[pane] = path
            # Only push history if not triggered by Back
            if getattr(pane, "_suppress_history_push", False):
                pane._suppress_history_push = False
            else:
                pane.push_history(path)
            self._manager.listdir(path)

    def _on_request_operation(self, pane: FilePane, action: str, payload, user_data=None) -> None:
        if action == "mkdir":
            dialog = Adw.MessageDialog.new(
                self,
                title="New Folder",
                body="Enter a name for the new folder",
            )
            entry = Gtk.Entry()
            dialog.set_extra_child(entry)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Create")
            dialog.set_default_response("ok")
            dialog.set_close_response("cancel")

            def _on_response(_dialog, response: str) -> None:
                if response == "ok":
                    name = entry.get_text().strip()
                    if name:
                        current_dir = pane.toolbar.path_entry.get_text() or "/"
                        if pane is self._left_pane:
                            target_dir = self._normalize_local_path(current_dir)
                            new_path = os.path.join(target_dir, name)
                        else:
                            new_path = posixpath.join(current_dir or "/", name)
                        if pane is self._left_pane:
                            try:
                                os.makedirs(new_path, exist_ok=False)
                            except FileExistsError:
                                pane.show_toast("Folder already exists")
                            except Exception as exc:
                                pane.show_toast(str(exc))
                            else:
                                # Refresh local listing
                                self._load_local(os.path.dirname(new_path) or "/")
                        else:
                            future = self._manager.mkdir(new_path)
                            self._attach_refresh(future, refresh_remote=pane)
                dialog.destroy()

            dialog.connect("response", _on_response)
            dialog.present()
        elif action == "rename" and isinstance(payload, dict):
            entry = payload.get("entry")
            directory = payload.get("directory") or pane.toolbar.path_entry.get_text() or "/"
            if not isinstance(entry, FileEntry):
                return

            if pane is self._left_pane:
                base_dir = self._normalize_local_path(directory)
                source = os.path.join(base_dir, entry.name)
                join = os.path.join
            else:
                base_dir = directory or "/"
                source = posixpath.join(base_dir, entry.name)
                join = posixpath.join

            dialog = Adw.MessageDialog.new(
                self,
                title="Rename Item",
                body=f"Enter a new name for {entry.name}",
            )
            name_entry = Gtk.Entry()
            name_entry.set_text(entry.name)
            dialog.set_extra_child(name_entry)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Rename")
            dialog.set_default_response("ok")
            dialog.set_close_response("cancel")

            def _on_rename(_dialog, response: str) -> None:
                if response != "ok":
                    dialog.destroy()
                    return
                new_name = name_entry.get_text().strip()
                if not new_name:
                    pane.show_toast("Name cannot be empty")
                    dialog.destroy()
                    return
                if new_name == entry.name:
                    dialog.destroy()
                    return
                target = join(base_dir, new_name)
                if pane is self._left_pane:
                    try:
                        os.rename(source, target)
                    except Exception as exc:
                        pane.show_toast(str(exc))
                    else:
                        pane.show_toast("Item renamed")
                        self._load_local(base_dir)
                else:
                    future = self._manager.rename(source, target)
                    self._attach_refresh(future, refresh_remote=pane)
                dialog.destroy()

            dialog.connect("response", _on_rename)
            dialog.present()
        elif action == "delete" and isinstance(payload, dict):
            entry = payload.get("entry")
            directory = payload.get("directory") or pane.toolbar.path_entry.get_text() or "/"
            if not isinstance(entry, FileEntry):
                return

            if pane is self._left_pane:
                base_dir = self._normalize_local_path(directory)
                target_path = os.path.join(base_dir, entry.name)
            else:
                base_dir = directory or "/"
                target_path = posixpath.join(base_dir, entry.name)

            dialog = Adw.MessageDialog.new(
                self,
                title="Delete Item",
                body=f"Delete {entry.name}?",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Delete")
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def _on_delete(_dialog, response: str) -> None:
                if response != "ok":
                    dialog.destroy()
                    return
                if pane is self._left_pane:
                    try:
                        if entry.is_dir:
                            shutil.rmtree(target_path)
                        else:
                            os.remove(target_path)
                    except FileNotFoundError:
                        pane.show_toast("Item no longer exists")
                    except Exception as exc:
                        pane.show_toast(str(exc))
                    else:
                        pane.show_toast("Item deleted")
                        self._load_local(base_dir)
                else:
                    future = self._manager.remove(target_path)
                    self._attach_refresh(future, refresh_remote=pane)
                dialog.destroy()

            dialog.connect("response", _on_delete)
            dialog.present()
        elif action == "upload":
            if pane is not self._right_pane:
                return

            remote_root = pane.toolbar.path_entry.get_text() or "/"
            raw_items: object | None = None

            if isinstance(payload, dict):
                destination = payload.get("destination")
                if isinstance(destination, pathlib.Path):
                    remote_root = destination.as_posix()
                elif isinstance(destination, str) and destination:
                    remote_root = destination
                raw_items = payload.get("paths")
            else:
                raw_items = payload

            paths: List[pathlib.Path] = []

            def _collect(item: object | None) -> None:
                if item is None:
                    return
                if isinstance(item, (list, tuple, set, frozenset)):
                    for value in item:
                        _collect(value)
                    return
                if isinstance(item, pathlib.Path):
                    paths.append(item)
                elif isinstance(item, Gio.File):
                    local_path = item.get_path()
                    if local_path:
                        paths.append(pathlib.Path(local_path))
                elif isinstance(item, str):
                    paths.append(pathlib.Path(item))

            _collect(raw_items)

            if not paths:
                pane.show_toast("No files selected for upload")
                return

            available_paths: List[pathlib.Path] = []
            missing: List[pathlib.Path] = []
            for candidate in paths:
                try:
                    if candidate.exists():
                        available_paths.append(candidate)
                    else:
                        missing.append(candidate)
                except OSError:
                    missing.append(candidate)

            if missing and not available_paths:
                pane.show_toast("Selected items are not accessible")
                return
            if missing and available_paths:
                pane.show_toast(f"Skipping inaccessible items: {missing[0].name}")

            for path_obj in available_paths:
                destination = posixpath.join(remote_root or "/", path_obj.name)
                toast_message = f"Uploading {path_obj.name}"
                self._toast_overlay.add_toast(Adw.Toast.new(toast_message))
                if path_obj.is_dir():
                    future = self._manager.upload_directory(path_obj, destination)
                else:
                    future = self._manager.upload(path_obj, destination)
                self._attach_refresh(future, refresh_remote=remote_pane)
        elif action == "download" and isinstance(payload, dict):
            source = payload.get("source")
            destination_base = payload.get("destination")
            is_dir = payload.get("is_dir", False)

            if not source or destination_base is None:
                pane.show_toast("Invalid download request")
                return

            if not isinstance(destination_base, pathlib.Path):
                destination_base = pathlib.Path(destination_base)

            name = os.path.basename(source.rstrip("/"))
            toast_message = f"Downloading {name}"
            self._toast_overlay.add_toast(Adw.Toast.new(toast_message))

            if is_dir:
                target_path = destination_base / name
                future = self._manager.download_directory(source, target_path)
            else:
                target_path = destination_base / name
                future = self._manager.download(source, target_path)

            self._attach_refresh(future, refresh_local_path=str(destination_base))

    def _on_window_resize(self, window, pspec) -> None:
        """Maintain proportional paned split when window is resized following GNOME HIG"""
        # Get current window width
        width = self.get_width()
        if width > 0:
            # Set paned position to half the window width (maintaining 50/50 split)
            self._panes.set_position(width // 2)

    def _attach_refresh(
        self,
        future: Optional[Future],
        *,
        refresh_remote: Optional[FilePane] = None,
        refresh_local_path: Optional[str] = None,
    ) -> None:
        if future is None:
            return

        def _on_done(completed: Future) -> None:
            try:
                completed.result()
            except Exception:
                return
            if refresh_remote is not None:
                GLib.idle_add(self._refresh_remote_listing, refresh_remote)
            if refresh_local_path:
                GLib.idle_add(self._refresh_local_listing, refresh_local_path)

        future.add_done_callback(_on_done)

    def _refresh_remote_listing(self, pane: FilePane) -> bool:
        path = pane.toolbar.path_entry.get_text() or "/"
        self._pending_paths[pane] = path
        self._manager.listdir(path)
        return False

    def _refresh_local_listing(self, path: str) -> bool:
        target = self._normalize_local_path(path)
        current = self._normalize_local_path(self._left_pane.toolbar.path_entry.get_text())
        if target == current:
            self._load_local(target)
        return False

    @staticmethod
    def _normalize_local_path(path: Optional[str]) -> str:
        expanded = os.path.expanduser(path or "/")
        return os.path.abspath(expanded)


def launch_file_manager_window(
    *,
    host: str,
    username: str,
    port: int = 22,
    path: str = "~",
    parent: Optional[Gtk.Window] = None,
) -> FileManagerWindow:
    """Create and present the :class:`FileManagerWindow`.

    The function obtains the default application instance (``Gtk.Application``)
    if available; otherwise the caller must ensure the returned window remains
    referenced for the duration of its lifetime.
    """

    app = Gtk.Application.get_default()
    if app is None:
        raise RuntimeError("An application instance is required to show the window")

    window = FileManagerWindow(
        application=app,
        host=host,
        username=username,
        port=port,
        initial_path=path,
    )
    if parent is not None:
        window.set_transient_for(parent)
        window.set_modal(True)
    window.present()
    return window


__all__ = [
    "AsyncSFTPManager",
    "FileEntry",
    "FileManagerWindow",
    "launch_file_manager_window",
]

