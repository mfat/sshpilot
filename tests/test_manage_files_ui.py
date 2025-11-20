import importlib
import sys
import types

from tests.test_sftp_utils_in_app_manager import setup_gi as _rich_setup_gi


def setup_gi(monkeypatch):
    _rich_setup_gi(monkeypatch)
    repository = sys.modules.get("gi.repository")
    if repository is None:
        return
    for name in ("Pango", "PangoFT2"):
        if not hasattr(repository, name):
            module = types.ModuleType(name)
            setattr(repository, name, module)
            monkeypatch.setitem(sys.modules, f"gi.repository.{name}", module)
    gtk_module = getattr(repository, "Gtk", None)
    if gtk_module is not None:
        if not hasattr(gtk_module, "ToggleButton"):
            class _DummyToggleButton:
                def __init__(self, *args, **kwargs):
                    self._active = False

                def connect(self, *args, **kwargs):
                    return None

                def set_active(self, value):
                    self._active = bool(value)

                def get_active(self):
                    return self._active

            gtk_module.ToggleButton = _DummyToggleButton

        class _GtkFallback:
            def __init__(self, *args, **kwargs):
                pass

            def __call__(self, *args, **kwargs):
                return _GtkFallback()

            def __getattr__(self, _name):
                return lambda *args, **kwargs: None

            def connect(self, *args, **kwargs):
                return None

            def set_active(self, *args, **kwargs):
                return None

            def get_active(self, *args, **kwargs):
                return False

        def _gtk_getattr(name):
            return _GtkFallback

        gtk_module.__getattr__ = _gtk_getattr
    gio_module = getattr(repository, "Gio", None)
    if gio_module is not None and not hasattr(gio_module, "SimpleAction"):
        class _SimpleAction:
            def __init__(self, name=None, parameter_type=None):
                self.name = name

            @classmethod
            def new(cls, name, parameter_type):
                return cls(name, parameter_type)

            def connect(self, *args, **kwargs):
                return None

        gio_module.SimpleAction = _SimpleAction
    glib_module = getattr(repository, "GLib", None)
    if glib_module is not None and not hasattr(glib_module, "VariantType"):
        class _VariantType:
            def __init__(self, signature):
                self.signature = signature

            @classmethod
            def new(cls, signature):
                return cls(signature)

        glib_module.VariantType = _VariantType


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
    sftp_stub._gvfs_supports_sftp = lambda: True
    monkeypatch.setitem(sys.modules, "sshpilot.sftp_utils", sftp_stub)
    prefs_stub = types.ModuleType("sshpilot.preferences")
    prefs_stub.should_hide_external_terminal_options = lambda: False
    prefs_stub.should_hide_file_manager_options = lambda: False
    monkeypatch.setitem(sys.modules, "sshpilot.preferences", prefs_stub)
    return reload_module("sshpilot.actions")


def prepare_file_manager_integration(monkeypatch):
    setup_gi(monkeypatch)
    sftp_stub = types.ModuleType("sshpilot.sftp_utils")
    def open_remote_in_file_manager(*args, **kwargs):
        return True, None
    sftp_stub.open_remote_in_file_manager = open_remote_in_file_manager
    sftp_stub._gvfs_supports_sftp = lambda: True
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

        def on_sort_connections_action(self, *args):
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


def test_launch_remote_file_manager_falls_back_after_mount_error(monkeypatch):
    integration = prepare_file_manager_integration(monkeypatch)

    monkeypatch.setattr(integration, "has_native_gvfs_support", lambda: True)
    monkeypatch.setattr(integration, "has_internal_file_manager", lambda: True)

    def fake_open_remote_in_file_manager(**_kwargs):
        return False, "Volume doesn't implement mount"

    sentinel = object()

    def fake_open_internal_file_manager(**kwargs):
        kwargs["check"] = "ok"
        return sentinel

    monkeypatch.setattr(
        integration, "open_remote_in_file_manager", fake_open_remote_in_file_manager
    )
    monkeypatch.setattr(
        integration, "open_internal_file_manager", fake_open_internal_file_manager
    )

    success, error, window = integration.launch_remote_file_manager(
        user="fallback",
        host="example.test",
        port=2200,
        nickname="Mountless",
    )

    assert success
    assert error is None
    assert window is sentinel
