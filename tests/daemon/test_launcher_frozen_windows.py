"""A frozen Windows build starts its daemon from the console executable."""

from __future__ import annotations

import os

from sshpilot.daemon import launcher


def test_frozen_windows_prefers_the_console_exe(monkeypatch, tmp_path):
    gui = tmp_path / "sshPilot.exe"
    cli = tmp_path / launcher.FROZEN_WINDOWS_CONSOLE_EXE
    gui.write_bytes(b"")
    cli.write_bytes(b"")
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(gui))
    assert launcher._frozen_console_executable() == os.path.join(str(tmp_path), cli.name)


def test_without_the_console_exe_the_own_exe_is_used(monkeypatch, tmp_path):
    gui = tmp_path / "sshPilot.exe"
    gui.write_bytes(b"")
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(gui))
    assert launcher._frozen_console_executable() is None


def test_source_runs_and_other_platforms_are_unaffected(monkeypatch):
    monkeypatch.delattr(launcher.sys, "frozen", raising=False)
    assert launcher._frozen_console_executable() is None
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    assert launcher._frozen_console_executable() is None
