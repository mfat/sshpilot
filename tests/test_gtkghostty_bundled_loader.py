"""The gtkghostty backend loads the copy a Linux package installed privately."""

from __future__ import annotations

import importlib
import sys
import types

import pytest


@pytest.fixture()
def loader(monkeypatch):
    """terminal_backend_gtkghostty's loader, without importing GtkGhostty."""
    source = importlib.util.find_spec("sshpilot.terminal_backend_gtkghostty").origin
    text = open(source, encoding="utf-8").read()
    start = text.index("#: The bundled copy's libraries")
    end = text.index("_use_bundled_widget()\n")
    namespace = {"__name__": "sshpilot.terminal_backend_gtkghostty", "__package__": "sshpilot",
                 "os": __import__("os")}
    exec(compile(text[start:end], source, "exec"), namespace)
    calls = []
    fake = types.ModuleType("sshpilot.private_gi")
    fake.use_private_libdir = lambda libdir, libraries: calls.append((libdir, list(libraries)))
    monkeypatch.setitem(sys.modules, "sshpilot.private_gi", fake)
    return namespace["_use_bundled_widget"], calls


def _build_config(monkeypatch, libdir):
    module = types.ModuleType("sshpilot.build_config")
    module.GTKGHOSTTY_LIBDIR = libdir
    monkeypatch.setitem(sys.modules, "sshpilot.build_config", module)


def test_loads_libghostty_before_the_widget(loader, monkeypatch, tmp_path):
    use, calls = loader
    for name in ("libgtkghostty-1.so.0", "libghostty-vt.so.1"):
        (tmp_path / name).write_bytes(b"")
    _build_config(monkeypatch, str(tmp_path))
    use()
    assert calls == [(str(tmp_path), ["libghostty-vt.so.1", "libgtkghostty-1.so.0"])]


def test_a_system_libghostty_is_left_to_the_loader(loader, monkeypatch, tmp_path):
    use, calls = loader
    (tmp_path / "libgtkghostty-1.so.0").write_bytes(b"")  # bundle_libghostty_vt=false
    _build_config(monkeypatch, str(tmp_path))
    use()
    assert calls == [(str(tmp_path), ["libgtkghostty-1.so.0"])]


@pytest.mark.parametrize("libdir", ["", "/nonexistent/sshpilot"])
def test_builds_without_the_widget_change_nothing(loader, monkeypatch, libdir):
    use, calls = loader
    _build_config(monkeypatch, libdir)
    use()
    assert calls == []


def test_a_library_that_cannot_load_makes_the_backend_unavailable(loader, monkeypatch, tmp_path):
    use, _calls = loader
    (tmp_path / "libgtkghostty-1.so.0").write_bytes(b"")
    _build_config(monkeypatch, str(tmp_path))

    def broken(libdir, libraries):
        raise OSError("libgtk-4.so.1: cannot open shared object file")

    sys.modules["sshpilot.private_gi"].use_private_libdir = broken
    with pytest.raises(ImportError, match="bundled gtkghostty cannot load"):
        use()
