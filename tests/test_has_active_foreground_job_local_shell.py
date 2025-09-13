import os
import sys
import types
import importlib


def test_has_active_foreground_job_uses_vte_get_pty(monkeypatch):
    gi_module = types.ModuleType("gi")
    gi_module.require_version = lambda *a, **k: None

    class Module(types.SimpleNamespace):
        def __getattr__(self, name):
            return Module()

        def __call__(self, *a, **k):
            return Module()

    class Gtk(Module):
        class Box:  # type: ignore[empty-body]
            pass

    class GObject(Module):
        class SignalFlags:
            RUN_FIRST = 0

    repo = Module(
        Gtk=Gtk(),
        Adw=Module(),
        Gio=Module(),
        GLib=Module(),
        GObject=GObject(),
        Gdk=Module(),
        Pango=Module(),
        Vte=Module(),
    )

    original_gi = {
        name: sys.modules.get(name)
        for name in ["gi", "gi.repository", *[f"gi.repository.{n}" for n in [
            "Gtk",
            "Adw",
            "Gio",
            "GLib",
            "GObject",
            "Gdk",
            "Pango",
            "Vte",
        ]]]
    }

    sys.modules["gi"] = gi_module
    sys.modules["gi.repository"] = repo
    for name in [
        "Gtk",
        "Adw",
        "Gio",
        "GLib",
        "GObject",
        "Gdk",
        "Pango",
        "Vte",
    ]:
        sys.modules[f"gi.repository.{name}"] = getattr(repo, name)

    try:
        terminal = importlib.import_module("sshpilot.terminal")

        calls = {"get_pty": False, "close": False}

        class DummyPTY:
            def get_slave_fd(self):
                return 1

        class DummyVte:
            def get_pty(self):
                calls["get_pty"] = True
                return DummyPTY()

        term = types.SimpleNamespace(vte=DummyVte(), process_pgid=7, pty=None)

        monkeypatch.setattr(os, "tcgetpgrp", lambda fd: 7)

        def fake_close(fd):
            calls["close"] = True
        monkeypatch.setattr(os, "close", fake_close)

        assert terminal.TerminalWidget.has_active_foreground_job(term) is False
        assert calls["get_pty"] is True
        assert calls["close"] is True
    finally:
        for name, mod in original_gi.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
