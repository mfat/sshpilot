import types
import subprocess
import pytest

import sys
import types

# Stub out gi modules so window.py can be imported without GTK
gi_module = types.ModuleType("gi")
gi_module.require_version = lambda *args, **kwargs: None


class Module(types.SimpleNamespace):
    def __getattr__(self, name):
        return Module()

    def __call__(self, *args, **kwargs):
        return Module()


repo = Module()
repo.Gtk = Module(Button=type("Button", (), {}), Dialog=type("Dialog", (), {}), Label=type("Label", (), {}), CssProvider=type("CssProvider", (), {}))
repo.Adw = Module(ApplicationWindow=type("ApplicationWindow", (), {}), MessageDialog=type("MessageDialog", (), {}))
repo.Gio = Module(SimpleAction=type("SimpleAction", (), {}), ThemedIcon=type("ThemedIcon", (), {}))
repo.GLib = Module(idle_add=lambda *a, **k: None)
repo.GObject = Module(Object=type("Object", (), {}))
repo.Gdk = Module(Display=Module(get_default=lambda: None), RGBA=type("RGBA", (), {}))
repo.Pango = Module()
repo.PangoFT2 = Module()
repo.Vte = Module()

gi_module.repository = repo
original_gi = {name: sys.modules.get(name) for name in ["gi", "gi.repository"] + [f"gi.repository.{n}" for n in ["Gtk", "Adw", "Gio", "GLib", "GObject", "Gdk", "Pango", "PangoFT2", "Vte"]]}
sys.modules["gi"] = gi_module
sys.modules["gi.repository"] = repo
for name in ["Gtk", "Adw", "Gio", "GLib", "GObject", "Gdk", "Pango", "PangoFT2", "Vte"]:
    sys.modules[f"gi.repository.{name}"] = getattr(repo, name)

# Stub internal modules referenced by window.py that aren't needed for these tests
stub_modules = {
    "sshpilot.connection_manager": types.SimpleNamespace(ConnectionManager=object, Connection=object),
    "sshpilot.terminal": types.SimpleNamespace(TerminalWidget=object),
    "sshpilot.terminal_manager": types.SimpleNamespace(TerminalManager=object),
    "sshpilot.config": types.SimpleNamespace(Config=object),
    "sshpilot.key_manager": types.SimpleNamespace(KeyManager=object, SSHKey=object),
    "sshpilot.connection_dialog": types.SimpleNamespace(ConnectionDialog=object),
    "sshpilot.askpass_utils": types.SimpleNamespace(ensure_askpass_script=lambda: None),
    "sshpilot.preferences": types.SimpleNamespace(
        PreferencesWindow=object,
        should_hide_external_terminal_options=lambda: False,
        should_hide_file_manager_options=lambda: False,
    ),
    "sshpilot.sshcopyid_window": types.SimpleNamespace(SshCopyIdWindow=object),
    "sshpilot.groups": types.SimpleNamespace(GroupManager=object),
    "sshpilot.sidebar": types.SimpleNamespace(GroupRow=object, ConnectionRow=object, build_sidebar=lambda *a, **k: None),
    "sshpilot.file_manager": types.SimpleNamespace(
        launch_sftp_file_manager_for_connection=lambda *a, **k: types.SimpleNamespace(present=lambda: None)
    ),
    "sshpilot.welcome_page": types.SimpleNamespace(WelcomePage=object),
    "sshpilot.actions": types.SimpleNamespace(WindowActions=object, register_window_actions=lambda *a, **k: None),
    "sshpilot.shutdown": types.SimpleNamespace(),
    "sshpilot.search_utils": types.SimpleNamespace(connection_matches=lambda *a, **k: False),
    "sshpilot.shortcut_utils": types.SimpleNamespace(get_primary_modifier_label=lambda: "Ctrl"),
}

original_stubs = {}
for name, module in stub_modules.items():
    original_stubs[name] = sys.modules.get(name)
    sys.modules[name] = module

import sshpilot.window as window_mod
from sshpilot.window import MainWindow

# Restore original modules so other tests see real implementations
for name, mod in original_stubs.items():
    if mod is None:
        del sys.modules[name]
    else:
        sys.modules[name] = mod
for name, mod in original_gi.items():
    if mod is None:
        del sys.modules[name]
    else:
        sys.modules[name] = mod


class DummyConfig:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)


class DummyWindow:
    def __init__(self, settings=None):
        self.config = DummyConfig(settings)

    _get_user_preferred_terminal = MainWindow._get_user_preferred_terminal
    _get_default_terminal_command = MainWindow._get_default_terminal_command
    _open_connection_in_external_terminal = MainWindow._open_connection_in_external_terminal
    _open_system_terminal = MainWindow._open_system_terminal

    def _show_terminal_error_dialog(self):
        raise AssertionError("error dialog not expected")


def test_get_user_preferred_terminal_macos(monkeypatch):
    monkeypatch.setattr(window_mod, "is_macos", lambda: True)
    win = DummyWindow({"external-terminal": "iTerm"})
    assert win._get_user_preferred_terminal() == ["open", "-a", "iTerm"]


def test_get_user_preferred_terminal_macos_with_command(monkeypatch):
    monkeypatch.setattr(window_mod, "is_macos", lambda: True)
    win = DummyWindow({"external-terminal": "open -a Ghostty"})
    assert win._get_user_preferred_terminal() == ["open", "-a", "Ghostty"]



def test_get_default_terminal_command_macos(monkeypatch):
    monkeypatch.setattr(window_mod, "is_macos", lambda: True)

    def fake_run(cmd, capture_output=False, text=False, timeout=None):
        class R:
            pass
        r = R()
        if cmd[0] == "osascript":
            # simulate only Terminal being installed
            r.stdout = "com.apple.Terminal" if "Terminal" in cmd[-1] else ""
            r.returncode = 0 if "Terminal" in cmd[-1] else 1
        elif cmd[0] == "mdfind":
            r.stdout = ""  # mdfind not used in this test
            r.returncode = 1
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)
    win = DummyWindow()
    assert win._get_default_terminal_command() == ["open", "-a", "Terminal"]


def test_get_default_terminal_command_iterm(monkeypatch):
    monkeypatch.setattr(window_mod, "is_macos", lambda: True)

    def fake_run(cmd, capture_output=False, text=False, timeout=None):
        class R:
            pass
        r = R()
        if cmd[0] == "osascript":
            if "Terminal" in cmd[-1]:
                r.stdout = ""
                r.returncode = 1
            elif "iTerm" in cmd[-1]:
                r.stdout = "com.googlecode.iterm2"
                r.returncode = 0
            else:
                r.stdout = ""
                r.returncode = 1
        elif cmd[0] == "mdfind":
            r.stdout = ""
            r.returncode = 1
        else:
            r.stdout = ""
            r.returncode = 1
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)
    win = DummyWindow()
    assert win._get_default_terminal_command() == ["open", "-a", "iTerm"]


def test_open_connection_terminal_app(monkeypatch):
    monkeypatch.setattr(window_mod, "is_macos", lambda: True)
    win = DummyWindow({"external-terminal": "Terminal"})

    captured = {"cmds": []}

    def fake_popen(cmd, start_new_session=False):
        captured["cmds"].append(cmd)
        class P:
            pass
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    connection = types.SimpleNamespace(username="user", host="example.com", port=22)
    win._open_connection_in_external_terminal(connection)

    expected_script = 'tell app "Terminal" to do script "ssh user@example.com"\ntell app "Terminal" to activate'
    assert captured["cmds"][0] == ["osascript", "-e", expected_script]


def test_open_connection_iterm_app(monkeypatch):
    monkeypatch.setattr(window_mod, "is_macos", lambda: True)
    win = DummyWindow({"external-terminal": "iTerm"})

    captured = {"cmds": []}

    def fake_popen(cmd, start_new_session=False):
        captured["cmds"].append(cmd)
        class P:
            pass
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    connection = types.SimpleNamespace(username="user", host="example.com", port=22)
    win._open_connection_in_external_terminal(connection)

    expected_script = (
        'tell application "iTerm"\n'
        '    if (count of windows) = 0 then\n'
        '        create window with default profile\n'
        '    end if\n'
        '    tell current window\n'
        '        create tab with default profile\n'
        f'        tell current session to write text "ssh user@example.com"\n'
        '    end tell\n'
        '    activate\n'
        'end tell'
    )
    assert captured["cmds"][0] == ["osascript", "-e", expected_script]


def test_open_connection_alacritty(monkeypatch):
    monkeypatch.setattr(window_mod, "is_macos", lambda: True)
    win = DummyWindow({"external-terminal": "Alacritty"})

    captured = {"cmds": []}

    def fake_popen(cmd, start_new_session=False):
        captured["cmds"].append(cmd)
        class P:
            pass
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    connection = types.SimpleNamespace(username="user", host="example.com", port=22)
    win._open_connection_in_external_terminal(connection)

    expected_cmd = [
        "open",
        "-a",
        "Alacritty",
        "--args",
        "-e",
        "bash",
        "-lc",
        "ssh user@example.com; exec bash",
    ]
    assert captured["cmds"][0] == expected_cmd


def test_open_connection_ghostty(monkeypatch):
    monkeypatch.setattr(window_mod, "is_macos", lambda: True)
    win = DummyWindow({"external-terminal": "Ghostty"})

    captured = {"cmds": []}

    def fake_popen(cmd, start_new_session=False):
        captured["cmds"].append(cmd)
        class P:
            pass
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    connection = types.SimpleNamespace(username="user", host="example.com", port=22)
    win._open_connection_in_external_terminal(connection)

    expected_cmd = [
        "open",
        "-na",
        "Ghostty",
        "--args",
        "-e",
        "ssh user@example.com",
    ]
    assert captured["cmds"][0] == expected_cmd

