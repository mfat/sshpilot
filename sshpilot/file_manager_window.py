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
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional, Tuple


import paramiko
from gi.repository import Adw, Gio, GLib, GObject, Gdk, Gtk


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

    def connect(self) -> None:
        self._submit(self._connect_impl, on_success=lambda *_: self.emit("connected"))

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
            for attr in self._sftp.listdir_attr(path):
                entries.append(
                    FileEntry(
                        name=attr.filename,
                        is_dir=stat_isdir(attr),
                        size=attr.st_size,
                        modified=attr.st_mtime,
                    )
                )
            return path, entries

        self._submit(
            _impl,
            on_success=lambda result: self.emit("directory-loaded", *result),
            on_error=lambda exc: self.emit("operation-error", str(exc)),
        )

    def mkdir(self, path: str) -> None:
        self._submit(
            lambda: self._sftp.mkdir(path),
            on_success=lambda *_: self.listdir(os.path.dirname(path) or "/"),
        )

    def remove(self, path: str) -> None:
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
        self._submit(_impl, on_success=lambda *_: self.listdir(parent))

    def rename(self, source: str, target: str) -> None:
        self._submit(
            lambda: self._sftp.rename(source, target),
            on_success=lambda *_: self.listdir(os.path.dirname(target) or "/"),
        )

    def download(self, source: str, destination: pathlib.Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)

        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Starting download…")
            self._sftp.get(source, str(destination))
            self.emit("progress", 1.0, "Download complete")

        self._submit(_impl)

    def upload(self, source: pathlib.Path, destination: str) -> None:
        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Starting upload…")
            self._sftp.put(str(source), destination)
            self.emit("progress", 1.0, "Upload complete")

        self._submit(_impl)

    # Helpers for directory recursion – these are intentionally simplistic
    # and rely on Paramiko's high level API.

    def download_directory(self, source: str, destination: pathlib.Path) -> None:
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

        self._submit(_impl)

    def upload_directory(self, source: pathlib.Path, destination: str) -> None:
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

        self._submit(_impl)


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
        for widget in (self.back_button, self.up_button, self.new_folder_button):
            widget.set_valign(Gtk.Align.CENTER)
            widget.add_css_class("flat")
        self.append(self.back_button)
        self.append(self.up_button)
        self.append(self.new_folder_button)


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

        grid_factory = Gtk.SignalListItemFactory()
        grid_factory.connect("setup", self._on_grid_setup)
        grid_factory.connect("bind", self._on_grid_bind)
        grid_view = Gtk.GridView(
            model=self._selection_model,
            factory=grid_factory,
            max_columns=6,
        )
        grid_view.add_css_class("iconview")

        self._stack.add_named(list_view, "list")
        self._stack.add_named(grid_view, "grid")

        overlay = Adw.ToastOverlay()
        overlay.set_child(self._stack)
        self._overlay = overlay
        self.append(overlay)

        self.toolbar.list_toggle.connect("toggled", self._on_view_toggle, "list")
        self.toolbar.grid_toggle.connect("toggled", self._on_view_toggle, "grid")
        self.toolbar.path_entry.connect("activate", self._on_path_entry)
        self.toolbar.controls.new_folder_button.connect(
            "clicked", lambda *_: self.emit("request-operation", "mkdir", None)
        )

        self._history: List[str] = []
        self._current_path = "/"
        self._selection_model.connect("selection-changed", self._on_selection_changed)

        # Drag and drop controllers – these provide the visual affordance and
        # forward requests to the window which understands the context.
        drop_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop_target.connect("accept", lambda *_: True)
        drop_target.connect("drop", self._on_drop)
        self.add_controller(drop_target)

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
        label.set_hexpand(True)
        box.append(icon)
        box.append(label)
        item.set_child(box)

    def _on_list_bind(self, factory, item):
        box = item.get_child()
        label = box.get_last_child()
        value = item.get_item().get_string()
        label.set_text(value)

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

    def _on_selection_changed(self, model, position, n_items):
        pass  # Selection feedback handled by double click gestures (future)

    def _on_drop(self, target: Gtk.DropTarget, value: Gio.File, x: float, y: float):
        self.emit("request-operation", "upload", value)
        return True

    # -- public API -----------------------------------------------------

    def show_entries(self, path: str, entries: Iterable[FileEntry]) -> None:
        self._list_store.remove_all()
        for entry in entries:
            suffix = "/" if entry.is_dir else ""
            self._list_store.append(Gtk.StringObject.new(entry.name + suffix))
        self._current_path = path
        self.toolbar.path_entry.set_text(path)

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


class FileManagerWindow(Adw.ApplicationWindow):
    """Top-level window hosting two :class:`FilePane` instances."""

    def __init__(
        self,
        application: Adw.Application,
        *,
        host: str,
        username: str,
        port: int = 22,
        initial_path: str = "/",
    ) -> None:
        super().__init__(application=application, title="Remote Files")
        self.set_default_size(1000, 640)

        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self._toast_overlay.set_child(toolbar_view)

        header_bar = Adw.HeaderBar()
        header_bar.set_title_widget(Gtk.Label(label=f"{username}@{host}"))
        toolbar_view.add_top_bar(header_bar)

        panes = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        panes.set_wide_handle(True)
        toolbar_view.set_content(panes)

        self._left_pane = FilePane("Left Pane")
        self._right_pane = FilePane("Right Pane")
        panes.set_start_child(self._left_pane)
        panes.set_end_child(self._right_pane)

        self._manager = AsyncSFTPManager(host, username, port)
        self._manager.connect()
        self._manager.connect("connected", self._on_connected)
        self._manager.connect(
            "connection-error", lambda *_: self._toast_overlay.add_toast(Adw.Toast.new("Connection failed"))
        )
        self._manager.connect("progress", self._on_progress)
        self._manager.connect("operation-error", self._on_operation_error)
        self._manager.connect("directory-loaded", self._on_directory_loaded)

        for pane in (self._left_pane, self._right_pane):
            pane.connect("path-changed", self._on_path_changed, pane)
            pane.connect("request-operation", self._on_request_operation, pane)

        self._pending_paths: Dict[FilePane, Optional[str]] = {
            self._left_pane: initial_path,
            self._right_pane: None,
        }

        self._show_progress(0.1, "Connecting…")

    # -- signal handlers ------------------------------------------------

    def _show_progress(self, fraction: float, message: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast.new(message))

    def _on_connected(self, *_args) -> None:
        self._show_progress(0.4, "Connected")
        initial_path = self._pending_paths.get(self._left_pane)
        if initial_path:
            self._manager.listdir(initial_path)


    def _on_progress(self, _manager, fraction: float, message: str) -> None:
        self._show_progress(fraction, message)

    def _on_operation_error(self, _manager, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_priority(Adw.ToastPriority.HIGH)
        self._toast_overlay.add_toast(toast)

    def _on_directory_loaded(
        self, _manager, path: str, entries: Iterable[FileEntry]
    ) -> None:
        target = next(
            (pane for pane, pending in self._pending_paths.items() if pending == path),
            None,
        )
        if target is None:
            target = self._left_pane
        else:
            self._pending_paths[target] = None

        target.show_entries(path, entries)
        target.push_history(path)
        target.show_toast(f"Loaded {path}")

    def _on_path_changed(self, pane: FilePane, path: str) -> None:
        self._pending_paths[pane] = path

        pane.push_history(path)
        self._manager.listdir(path)

    def _on_request_operation(self, pane: FilePane, action: str, payload) -> None:
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
                        new_path = os.path.join(pane.toolbar.path_entry.get_text(), name)
                        self._manager.mkdir(new_path)
                dialog.destroy()

            dialog.connect("response", _on_response)
            dialog.present()
        elif action == "upload" and isinstance(payload, Gio.File):
            self._toast_overlay.add_toast(Adw.Toast.new("Uploading file…"))
            self._manager.upload(
                pathlib.Path(payload.get_path()),
                os.path.join(pane.toolbar.path_entry.get_text(), payload.get_basename()),
            )


def launch_file_manager_window(
    *,
    host: str,
    username: str,
    port: int = 22,
    path: str = "/",
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

