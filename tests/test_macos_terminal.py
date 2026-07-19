"""Tests for macOS external-terminal discovery (platform_utils)."""

import subprocess

import pytest

platform_utils = pytest.importorskip("sshpilot.platform_utils")


class DummyConfig:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)


def test_get_user_preferred_terminal_macos(monkeypatch):
    monkeypatch.setattr(platform_utils, "is_macos", lambda: True)
    config = DummyConfig({"external-terminal": "iTerm"})
    assert platform_utils.get_user_preferred_terminal(config) == ["open", "-a", "iTerm"]


def test_get_user_preferred_terminal_macos_with_command(monkeypatch):
    monkeypatch.setattr(platform_utils, "is_macos", lambda: True)
    config = DummyConfig({"external-terminal": "open -a Ghostty"})
    assert platform_utils.get_user_preferred_terminal(config) == ["open", "-a", "Ghostty"]


def test_get_default_terminal_command_macos(monkeypatch):
    monkeypatch.setattr(platform_utils, "is_macos", lambda: True)

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
    assert platform_utils.get_default_terminal_command() == ["open", "-a", "Terminal"]


def test_get_default_terminal_command_iterm(monkeypatch):
    monkeypatch.setattr(platform_utils, "is_macos", lambda: True)

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
    assert platform_utils.get_default_terminal_command() == ["open", "-a", "iTerm"]
