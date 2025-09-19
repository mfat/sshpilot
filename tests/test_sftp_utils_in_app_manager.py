import importlib
import sys
import types


def setup_gi(monkeypatch):
    gi = types.ModuleType("gi")

    def require_version(*_args, **_kwargs):
        return None

    gi.require_version = require_version
    repository = types.ModuleType("repository")
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)

    class DummyWidget:
        def __init__(self, *args, **kwargs):
            pass

        def __bool__(self):
            return True

        def __call__(self, *args, **kwargs):
            return None

        def __getattr__(self, _name):
            return lambda *args, **kwargs: DummyWidget()

        def connect(self, *args, **kwargs):
            return None

        def present(self, *args, **kwargs):
            return None

        def close(self, *args, **kwargs):
            return None

        def destroy(self, *args, **kwargs):
            return None

        def add_css_class(self, *args, **kwargs):
            return None

        def append(self, *args, **kwargs):
            return None

        def pack_start(self, *args, **kwargs):
            return None

        def pack_end(self, *args, **kwargs):
            return None

        def set_child(self, *args, **kwargs):
            return None

        def set_content(self, *args, **kwargs):
            return None

        def set_default_size(self, *args, **kwargs):
            return None

        def set_resizable(self, *args, **kwargs):
            return None

        def set_modal(self, *args, **kwargs):
            return None

        def set_transient_for(self, *args, **kwargs):
            return None

        def set_title(self, *args, **kwargs):
            return None

        def set_markup(self, *args, **kwargs):
            return None

        def set_text(self, *args, **kwargs):
            return None

        def set_placeholder_text(self, *args, **kwargs):
            return None

        def set_hexpand(self, *args, **kwargs):
            return None

        def set_vexpand(self, *args, **kwargs):
            return None

        def set_halign(self, *args, **kwargs):
            return None

        def set_valign(self, *args, **kwargs):
            return None

        def set_tooltip_text(self, *args, **kwargs):
            return None

        def set_group(self, *args, **kwargs):
            return None

        def set_active(self, *args, **kwargs):
            return None

        def set_visible_child_name(self, *args, **kwargs):
            return None

        def set_default_response(self, *args, **kwargs):
            return None

        def set_close_response(self, *args, **kwargs):
            return None

        def set_extra_child(self, *args, **kwargs):
            return None

        def get_text(self):
            return "/"

        def get_last_child(self):
            return DummyWidget()

        def get_child(self):
            return DummyWidget()

        def get_item(self):
            return DummyWidget()

        def get_string(self):
            return ""

        def get_basename(self):
            return "dummy"

        def get_path(self):
            return "/tmp"

        @classmethod
        def new_with_label(cls, *_args, **_kwargs):
            return cls()

        @classmethod
        def new_from_icon_name(cls, *_args, **_kwargs):
            return cls()

    gtk = types.ModuleType("Gtk")
    gtk.Box = DummyWidget
    gtk.Label = DummyWidget
    gtk.ProgressBar = DummyWidget
    gtk.Button = DummyWidget
    gtk.Entry = DummyWidget
    gtk.Stack = DummyWidget
    gtk.ListView = DummyWidget
    gtk.GridView = DummyWidget
    gtk.SingleSelection = DummyWidget
    gtk.StringObject = DummyWidget
    gtk.DropTarget = DummyWidget
    gtk.Application = types.SimpleNamespace(get_default=lambda: DummyWidget())
    gtk.Orientation = types.SimpleNamespace(VERTICAL=0, HORIZONTAL=1)
    gtk.Align = types.SimpleNamespace(START=0, CENTER=1)
    repository.Gtk = gtk
    monkeypatch.setitem(sys.modules, "gi.repository.Gtk", gtk)

    adw = types.ModuleType("Adw")
    adw.Window = DummyWidget
    adw.ApplicationWindow = DummyWidget
    adw.HeaderBar = DummyWidget
    adw.ToastOverlay = DummyWidget
    adw.Toast = types.SimpleNamespace(new=lambda *_args, **_kwargs: DummyWidget())
    adw.ToolbarView = DummyWidget
    adw.MessageDialog = DummyWidget
    adw.ToastPriority = types.SimpleNamespace(HIGH=1)
    repository.Adw = adw
    monkeypatch.setitem(sys.modules, "gi.repository.Adw", adw)

    glib = types.ModuleType("GLib")

    def idle_add(func, *args, **kwargs):
        func(*args, **kwargs)
        return 1

    def timeout_add(_interval, func, *args, **kwargs):
        if callable(func):
            func(*args, **kwargs)
        return 1

    glib.idle_add = idle_add
    glib.timeout_add = timeout_add
    glib.source_remove = lambda *_args, **_kwargs: None
    glib.Error = Exception
    repository.GLib = glib
    monkeypatch.setitem(sys.modules, "gi.repository.GLib", glib)

    gio = types.ModuleType("Gio")

    class DummyVolumeMonitor:
        @staticmethod
        def get():
            return DummyVolumeMonitor()

        def get_mounts(self):
            return []

    class DummyFile(DummyWidget):
        @staticmethod
        def new_for_uri(_uri):
            return DummyWidget()

    gio.VolumeMonitor = DummyVolumeMonitor
    gio.File = DummyFile
    gio.AppInfo = types.SimpleNamespace(launch_default_for_uri=lambda *_a, **_k: None)
    gio.MountOperation = DummyWidget
    repository.Gio = gio
    monkeypatch.setitem(sys.modules, "gi.repository.Gio", gio)

    gdk = types.ModuleType("Gdk")
    gdk.DragAction = types.SimpleNamespace(COPY=1)
    repository.Gdk = gdk
    monkeypatch.setitem(sys.modules, "gi.repository.Gdk", gdk)

    gobject = types.ModuleType("GObject")

    class DummyGObject:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, *args, **kwargs):
            return None

        def emit(self, *args, **kwargs):
            return None

    gobject.GObject = DummyGObject
    gobject.SignalFlags = types.SimpleNamespace(RUN_FIRST=0)
    repository.GObject = gobject
    monkeypatch.setitem(sys.modules, "gi.repository.GObject", gobject)


def import_sftp_utils(monkeypatch):
    setup_gi(monkeypatch)
    module = types.ModuleType("sshpilot.file_manager_window")
    module.launch_file_manager_window = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "sshpilot.file_manager_window", module)
    return importlib.reload(importlib.import_module("sshpilot.sftp_utils"))


def test_open_remote_uses_in_app_manager_when_flatpak(monkeypatch):
    called = {}
    setup_gi(monkeypatch)
    stub = types.ModuleType("sshpilot.file_manager_window")

    def fake_launch(**kwargs):
        called["kwargs"] = kwargs

    stub.launch_file_manager_window = fake_launch
    monkeypatch.setitem(sys.modules, "sshpilot.file_manager_window", stub)
    sftp_utils = importlib.reload(importlib.import_module("sshpilot.sftp_utils"))
    monkeypatch.setattr(sftp_utils, "is_flatpak", lambda: True)

    success, message = sftp_utils.open_remote_in_file_manager(
        "alice", "example.com", port=2222, path="/srv", parent_window=None
    )

    assert success
    assert message is None
    assert called["kwargs"]["path"] == "/srv"
    assert called["kwargs"]["port"] == 2222


def test_open_remote_uses_gvfs_flow_when_available(monkeypatch):
    sftp_utils = import_sftp_utils(monkeypatch)
    monkeypatch.setattr(sftp_utils, "_should_use_in_app_file_manager", lambda: False)

    class DummyProgress:
        def __init__(self, *_args, **_kwargs):
            self.is_cancelled = False

        def present(self):
            return None

        def start_progress_updates(self):
            return None

        def update_progress(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(sftp_utils, "MountProgressDialog", DummyProgress)

    recorded = {}

    def fake_mount(uri, user, host, error_callback=None, progress_dialog=None):
        recorded["uri"] = uri
        recorded["user"] = user
        recorded["host"] = host
        return True, None

    monkeypatch.setattr(sftp_utils, "_mount_and_open_sftp", fake_mount)
    monkeypatch.setattr(
        sftp_utils,
        "_verify_ssh_connection_async",
        lambda user, host, port, callback: callback(True),
    )

    success, message = sftp_utils.open_remote_in_file_manager(
        "bob", "remote", port=2222, path="/home", parent_window=None
    )

    assert success
    assert message is None
    assert recorded["uri"] == "sftp://bob@remote:2222/home"


def test_gvfs_supports_sftp_false_when_gio_missing(monkeypatch):
    sftp_utils = import_sftp_utils(monkeypatch)
    monkeypatch.setattr(sftp_utils.shutil, "which", lambda *_args, **_kwargs: None)
    assert not sftp_utils._gvfs_supports_sftp()
