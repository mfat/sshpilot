import importlib
import sys
import types


def setup_gi(monkeypatch):
    gi = types.ModuleType("gi")
    def require_version(*args, **kwargs):
        pass
    gi.require_version = require_version
    repository = types.ModuleType("repository")
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    for name in ["Gtk", "Adw", "Pango", "PangoFT2", "Gio", "GLib", "Gdk", "Vte"]:
        module = types.ModuleType(name)
        setattr(repository, name, module)
        monkeypatch.setitem(sys.modules, f"gi.repository.{name}", module)
    repository.Gtk.Window = type("Window", (), {})
    repository.Adw.Toast = types.SimpleNamespace(new=lambda *_args, **_kwargs: object())
    repository.Adw.Window = type("Window", (), {})
    repository.Adw.PreferencesWindow = type("PreferencesWindow", (), {})
    class SimpleAction:
        def __init__(self, name=None, parameter_type=None):
            self.name = name
        @classmethod
        def new(cls, name, parameter_type):
            return cls(name, parameter_type)
        def connect(self, *args, **kwargs):
            pass
    repository.Gio.SimpleAction = SimpleAction


def reload_module(name):
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)


def prepare_actions(monkeypatch):
    setup_gi(monkeypatch)
    sftp_stub = types.ModuleType("sshpilot.sftp_utils")
    def open_remote_in_file_manager(*args, **kwargs):
        return True, None
    sftp_stub.open_remote_in_file_manager = open_remote_in_file_manager
    monkeypatch.setitem(sys.modules, "sshpilot.sftp_utils", sftp_stub)
    return reload_module("sshpilot.actions")


def prepare_file_manager_integration(monkeypatch):
    setup_gi(monkeypatch)
    sftp_stub = types.ModuleType("sshpilot.sftp_utils")
    def open_remote_in_file_manager(*args, **kwargs):
        return True, None
    sftp_stub.open_remote_in_file_manager = open_remote_in_file_manager
    monkeypatch.setitem(sys.modules, "sshpilot.sftp_utils", sftp_stub)
    return reload_module("sshpilot.file_manager_integration")


def create_window():
    class DummyWindow:
        def add_action(self, action):
            pass
        def on_open_new_connection_action(self, *args):
            pass
        def on_open_new_connection_tab_action(self, *args):
            pass
        def on_manage_files_action(self, *args):
            pass
        def on_edit_connection_action(self, *args):
            pass
        def on_delete_connection_action(self, *args):
            pass
        def on_open_in_system_terminal_action(self, *args):
            pass
        def on_broadcast_command_action(self, *args):
             pass
        def on_create_group_action(self, *args):
             pass
        def on_edit_group_action(self, *args):
             pass
        def on_delete_group_action(self, *args):
             pass
        def on_move_to_ungrouped_action(self, *args):
             pass
        def on_move_to_group_action(self, *args):
             pass
        def on_toggle_sidebar_action(self, *args):
             pass
    return DummyWindow()


def test_manage_files_action_hidden_on_macos(monkeypatch):
    actions = prepare_actions(monkeypatch)
    monkeypatch.setattr(actions, "should_hide_file_manager_options", lambda: True)
    monkeypatch.setattr(actions, "should_hide_external_terminal_options", lambda: True)
    window = create_window()
    actions.register_window_actions(window)
    assert not hasattr(window, "manage_files_action")


def test_manage_files_action_visible_on_other_platforms(monkeypatch):
    actions = prepare_actions(monkeypatch)
    monkeypatch.setattr(actions, "should_hide_file_manager_options", lambda: False)
    monkeypatch.setattr(actions, "should_hide_external_terminal_options", lambda: True)
    window = create_window()
    actions.register_window_actions(window)
    assert hasattr(window, "manage_files_action")


def test_should_hide_file_manager_options(monkeypatch):
    setup_gi(monkeypatch)
    prefs = reload_module("sshpilot.preferences")
    monkeypatch.setattr(prefs, "has_native_gvfs_support", lambda: False)
    monkeypatch.setattr(prefs, "has_internal_file_manager", lambda: False)
    assert prefs.should_hide_file_manager_options()

    monkeypatch.setattr(prefs, "has_internal_file_manager", lambda: True)
    assert not prefs.should_hide_file_manager_options()

    monkeypatch.setattr(prefs, "has_internal_file_manager", lambda: False)
    monkeypatch.setattr(prefs, "has_native_gvfs_support", lambda: True)
    assert not prefs.should_hide_file_manager_options()


def test_launch_remote_file_manager_prefers_gvfs(monkeypatch):
    integration = prepare_file_manager_integration(monkeypatch)

    calls = {}

    def fake_open_remote_in_file_manager(**kwargs):
        calls["kwargs"] = kwargs
        return True, None

    monkeypatch.setattr(integration, "open_remote_in_file_manager", fake_open_remote_in_file_manager)
    monkeypatch.setattr(integration, "has_native_gvfs_support", lambda: True)
    monkeypatch.setattr(integration, "has_internal_file_manager", lambda: False)

    success, error, window = integration.launch_remote_file_manager(
        user="alice",
        host="example.com",
        port=2022,
        nickname="Example",
    )

    assert success
    assert error is None
    assert window is None
    assert calls["kwargs"]["user"] == "alice"
    assert calls["kwargs"]["port"] == 2022


def test_launch_remote_file_manager_uses_internal(monkeypatch):
    integration = prepare_file_manager_integration(monkeypatch)

    monkeypatch.setattr(integration, "has_native_gvfs_support", lambda: False)
    monkeypatch.setattr(integration, "has_internal_file_manager", lambda: True)

    sentinel = object()
    captured = {}

    def fake_open_internal_file_manager(**kwargs):
        captured["kwargs"] = kwargs
        return sentinel

    def fail_remote(**_kwargs):
        raise AssertionError("GVFS backend should not be used when native support is disabled")

    monkeypatch.setattr(integration, "open_internal_file_manager", fake_open_internal_file_manager)
    monkeypatch.setattr(integration, "open_remote_in_file_manager", fail_remote)

    success, error, window = integration.launch_remote_file_manager(
        user="bob",
        host="example.net",
        port=2222,
        nickname="Internal",
    )

    assert success
    assert error is None
    assert window is sentinel
    assert captured["kwargs"]["user"] == "bob"
    assert captured["kwargs"]["host"] == "example.net"
    assert captured["kwargs"]["nickname"] == "Internal"
    assert "connection" in captured["kwargs"]
    assert captured["kwargs"]["connection"] is None
    assert captured["kwargs"].get("connection_manager") is None
    assert captured["kwargs"].get("ssh_config") is None


def test_launch_remote_file_manager_no_backend(monkeypatch):
    integration = prepare_file_manager_integration(monkeypatch)

    monkeypatch.setattr(integration, "has_native_gvfs_support", lambda: False)
    monkeypatch.setattr(integration, "has_internal_file_manager", lambda: False)

    captured = {}

    def error_callback(message):
        captured["message"] = message

    success, error, window = integration.launch_remote_file_manager(
        user="carol",
        host="example.org",
        error_callback=error_callback,
    )

    assert not success
    assert "No compatible" in error
    assert captured["message"] == error
    assert window is None
