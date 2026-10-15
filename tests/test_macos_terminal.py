"""Tests for macOS external-terminal discovery (system_terminal)."""

import subprocess

import pytest

system_terminal = pytest.importorskip("sshpilot.system_terminal")


@pytest.fixture(autouse=True)
def _not_windows(monkeypatch):
    monkeypatch.setattr(system_terminal, "is_windows", lambda: False)


class DummyConfig:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)


def test_get_user_preferred_terminal_macos(monkeypatch):
    monkeypatch.setattr(system_terminal, "is_macos", lambda: True)
    config = DummyConfig({"external-terminal": "iTerm"})
    assert system_terminal.get_user_preferred_terminal(config) == ["open", "-a", "iTerm"]


def test_get_user_preferred_terminal_macos_with_command(monkeypatch):
    monkeypatch.setattr(system_terminal, "is_macos", lambda: True)
    config = DummyConfig({"external-terminal": "open -a Ghostty"})
    assert system_terminal.get_user_preferred_terminal(config) == ["open", "-a", "Ghostty"]


def test_get_default_terminal_command_macos(monkeypatch):
    monkeypatch.setattr(system_terminal, "is_macos", lambda: True)

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
    assert system_terminal.get_default_terminal_command() == ["open", "-a", "Terminal"]


def test_get_default_terminal_command_iterm(monkeypatch):
    monkeypatch.setattr(system_terminal, "is_macos", lambda: True)

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
    assert system_terminal.get_default_terminal_command() == ["open", "-a", "iTerm"]
