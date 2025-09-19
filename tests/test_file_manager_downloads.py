import importlib
import subprocess
import sys
import types
from pathlib import Path

import pytest


def setup_gi(monkeypatch):
    gi = types.ModuleType("gi")

    def require_version(*args, **kwargs):
        return None

    gi.require_version = require_version
    repository = types.ModuleType("repository")
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)

    for name in ["Gtk", "Adw", "Pango", "PangoFT2", "Gio", "GLib", "Gdk", "GObject"]:
        module = types.ModuleType(name)
        setattr(repository, name, module)
        monkeypatch.setitem(sys.modules, f"gi.repository.{name}", module)

    repository.GLib.idle_add = lambda func, *args, **kwargs: func(*args, **kwargs)
    repository.Adw.ApplicationWindow = type("ApplicationWindow", (), {})
    repository.Adw.Application = type("Application", (), {})


@pytest.fixture
def file_manager_module(monkeypatch):
    setup_gi(monkeypatch)
    original = sys.modules.pop("sshpilot.file_manager", None)
    module = importlib.import_module("sshpilot.file_manager")
    yield module
    sys.modules.pop("sshpilot.file_manager", None)
    if original is not None:
        sys.modules["sshpilot.file_manager"] = original


class ImmediateThread:
    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _make_tree_view(filename: str):
    iterator = object()

    def get_value(_iterator, index):
        if index == 0:
            return filename
        if index == 4:
            return False
        raise AssertionError(f"Unexpected index {index}")

    model = types.SimpleNamespace(get_value=get_value)
    selection = types.SimpleNamespace(get_selected=lambda: (model, iterator))
    return types.SimpleNamespace(get_selection=lambda: selection)


def test_download_file_key_auth(monkeypatch, tmp_path, file_manager_module):
    fm = file_manager_module

    monkeypatch.setattr(fm.threading, "Thread", lambda target=None, daemon=None: ImmediateThread(target, daemon))

    idle_calls = []

    def fake_idle_add(func, *args):
        idle_calls.append((func, args))
        return func(*args)

    monkeypatch.setattr(fm.GLib, "idle_add", fake_idle_add)

    captured = {}

    def fake_get_env(requirement):
        captured["askpass"] = requirement
        return {"SSH_ASKPASS_REQUIRE": requirement}

    monkeypatch.setattr(fm, "get_ssh_env_with_askpass", fake_get_env)

    def fake_run(cmd, env, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(fm.subprocess, "run", fake_run)

    messages = {"info": [], "error": []}

    manager = types.SimpleNamespace()
    manager.remote_tree_view = _make_tree_view("example.txt")
    manager.current_remote_path = "/var/log"
    manager.current_local_path = Path(tmp_path)
    manager.connection_info = {
        "auth_method": "key",
        "key_file": "/keys/id_ed25519",
        "port": 2022,
        "username": "alice",
        "host": "example.com",
    }

    def refresh_local_view():
        messages["info"].append("refreshed")

    def show_info(message):
        messages["info"].append(message)

    def show_error(message):
        messages["error"].append(message)
        raise AssertionError(f"show_error invoked: {message}")

    manager.refresh_local_view = refresh_local_view
    manager.show_info = show_info
    manager.show_error = show_error

    fm.SftpFileManager.download_file(manager, None)

    assert messages["error"] == []
    assert any(msg == "Downloaded example.txt" for msg in messages["info"])
    assert captured["askpass"] == "force"
    assert captured["cmd"][0] == "scp"
    assert f"alice@example.com:{manager.current_remote_path}/example.txt" in captured["cmd"]
    assert str(manager.current_local_path / "example.txt") in captured["cmd"]
    assert captured["env"]["SSH_ASKPASS_REQUIRE"] == "force"
    assert any(func is manager.refresh_local_view for func, _ in idle_calls)


def test_download_file_password_auth(monkeypatch, tmp_path, file_manager_module):
    fm = file_manager_module

    monkeypatch.setattr(fm.threading, "Thread", lambda target=None, daemon=None: ImmediateThread(target, daemon))

    def fake_idle_add(func, *args):
        return func(*args)

    monkeypatch.setattr(fm.GLib, "idle_add", fake_idle_add)

    calls = {}

    def fake_run_scp(host, user, password, sources, destination, *, direction, port):
        calls["host"] = host
        calls["user"] = user
        calls["password"] = password
        calls["sources"] = sources
        calls["destination"] = destination
        calls["direction"] = direction
        calls["port"] = port
        return subprocess.CompletedProcess(["scp"], 0, stdout="", stderr="")

    monkeypatch.setattr(fm, "run_scp_with_password", fake_run_scp)

    messages = {"info": [], "error": []}

    manager = types.SimpleNamespace()
    manager.remote_tree_view = _make_tree_view("report.csv")
    manager.current_remote_path = "/data"
    manager.current_local_path = Path(tmp_path)
    manager.connection_info = {
        "auth_method": "password",
        "password": "secret",
        "port": 2200,
        "username": "bob",
        "host": "files.example.com",
    }

    def refresh_local_view():
        messages["info"].append("refreshed")

    def show_info(message):
        messages["info"].append(message)

    def show_error(message):
        messages["error"].append(message)
        raise AssertionError(f"show_error invoked: {message}")

    manager.refresh_local_view = refresh_local_view
    manager.show_info = show_info
    manager.show_error = show_error

    fm.SftpFileManager.download_file(manager, None)

    assert messages["error"] == []
    assert any(msg == "Downloaded report.csv" for msg in messages["info"])
    assert calls["host"] == "files.example.com"
    assert calls["user"] == "bob"
    assert calls["password"] == "secret"
    assert calls["sources"] == [f"{manager.current_remote_path}/report.csv"]
    assert calls["destination"] == str(manager.current_local_path / "report.csv")
    assert calls["direction"] == "download"
    assert calls["port"] == 2200

