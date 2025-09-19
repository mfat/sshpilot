import types
import sys
import importlib


def setup_gi(monkeypatch):
    gi = types.ModuleType("gi")
    def require_version(*args, **kwargs):
        pass
    gi.require_version = require_version
    repository = types.ModuleType("repository")
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    for name in ["Gtk", "Adw", "Pango", "PangoFT2", "Gio", "GLib", "Gdk"]:
        module = types.ModuleType(name)
        setattr(repository, name, module)
        monkeypatch.setitem(sys.modules, f"gi.repository.{name}", module)
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
    fm_stub = types.ModuleType("sshpilot.file_manager")
    def open_connection_in_file_manager(*args, **kwargs):
        return True, None
    fm_stub.open_connection_in_file_manager = open_connection_in_file_manager
    monkeypatch.setitem(sys.modules, "sshpilot.file_manager", fm_stub)
    return reload_module("sshpilot.actions")


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
    monkeypatch.setattr(prefs, "is_macos", lambda: True)
    monkeypatch.setattr(prefs, "is_flatpak", lambda: False)
    assert prefs.should_hide_file_manager_options()
    monkeypatch.setattr(prefs, "is_macos", lambda: False)
    monkeypatch.setattr(prefs, "is_flatpak", lambda: True)
    assert prefs.should_hide_file_manager_options()
    monkeypatch.setattr(prefs, "is_macos", lambda: False)
    monkeypatch.setattr(prefs, "is_flatpak", lambda: False)
    assert not prefs.should_hide_file_manager_options()
