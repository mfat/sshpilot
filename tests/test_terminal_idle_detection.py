import sys
import types
import pytest


def _stub_gi():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *args, **kwargs: None

    class Dummy(types.SimpleNamespace):
        def __getattr__(self, name):
            return Dummy()

        def __call__(self, *args, **kwargs):
            return Dummy()

    repo = types.SimpleNamespace(
        Gtk=types.SimpleNamespace(Box=type("Box", (), {})),
        Vte=Dummy(),
        Adw=Dummy(),
        GLib=Dummy(),
        GObject=Dummy(),
        Gdk=Dummy(),
        Pango=Dummy(),
        PangoFT2=Dummy(),
        Gio=Dummy(),
    )
    gi.repository = repo

    modules = {"gi": gi, "gi.repository": repo}
    for name in ["Gtk", "Vte", "Adw", "GLib", "GObject", "Gdk", "Pango", "PangoFT2", "Gio"]:
        modules[f"gi.repository.{name}"] = getattr(repo, name)
    return modules


@pytest.fixture
def TerminalWidget():
    modules = _stub_gi()
    old = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    from sshpilot.terminal import TerminalWidget as TW
    yield TW
    for name, mod in old.items():
        if mod is None:
            del sys.modules[name]
        else:
            sys.modules[name] = mod


def test_is_local_terminal_recognizes_loopback(TerminalWidget):
    class Conn:
        host = "localhost"

    term = object.__new__(TerminalWidget)
    term.connection = Conn()
    assert term._is_local_terminal()
    term.connection.host = "127.0.0.1"
    assert term._is_local_terminal()
    term.connection.host = "::1"
    assert term._is_local_terminal()
    term.connection.host = "example.com"
    assert not term._is_local_terminal()


def test_has_active_job_unknown_pgid(TerminalWidget, monkeypatch):
    class Conn:
        host = "localhost"

    term = object.__new__(TerminalWidget)
    term.connection = Conn()
    term.vte = types.SimpleNamespace(get_pty=lambda: types.SimpleNamespace(get_fd=lambda: 0))
    term._shell_pgid = None
    monkeypatch.setattr(sys.modules["os"], "tcgetpgrp", lambda fd: 123)
    assert term._is_terminal_idle_pty() is False
    term._job_status = "UNKNOWN"
    assert term.has_active_job()
