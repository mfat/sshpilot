"""The Windows installer's update-check choice reaches the config."""

from __future__ import annotations

import sys
import types

import pytest

from sshpilot import installer_update_choice as choice


class FakeConfig:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get_setting(self, key, default=None):
        return self.data.get(key, default)

    def set_setting(self, key, value):
        self.data[key] = value


@pytest.fixture
def registry(monkeypatch):
    """A fake winreg holding HKCU\\Software\\sshPilot as a dict, or None."""
    state = {"key": {}}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, path, reserved, access):
        assert path == r"Software\sshPilot"
        if state["key"] is None:
            raise FileNotFoundError(path)
        return Key()

    def query(key, name):
        if name not in state["key"]:
            raise FileNotFoundError(name)
        return state["key"][name], 4

    def set_value(key, name, reserved, kind, value):
        state["key"][name] = value

    def delete_value(key, name):
        del state["key"][name]

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(), KEY_READ=1, KEY_SET_VALUE=2, REG_DWORD=4,
        OpenKey=open_key, QueryValueEx=query, SetValueEx=set_value,
        DeleteValue=delete_value)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(choice, "is_windows", lambda: True)
    return state


def test_pending_installer_choice_is_applied_once(registry):
    registry["key"] = {"CheckForUpdates": 0, "CheckForUpdatesPending": 1}
    config = FakeConfig({choice.SETTING: True})

    choice.apply_installer_choice(config)

    assert config.data[choice.SETTING] is False
    assert "CheckForUpdatesPending" not in registry["key"]


def test_without_pending_flag_the_setting_is_mirrored(registry):
    registry["key"] = {"CheckForUpdates": 1}
    config = FakeConfig({choice.SETTING: False})

    choice.apply_installer_choice(config)

    assert config.data[choice.SETTING] is False
    assert registry["key"]["CheckForUpdates"] == 0


def test_portable_install_without_key_is_left_alone(registry):
    registry["key"] = None
    config = FakeConfig()

    choice.apply_installer_choice(config)
    choice.mirror_choice(False)

    assert config.data == {}


def test_preferences_change_is_mirrored(registry):
    choice.mirror_choice(False)
    assert registry["key"]["CheckForUpdates"] == 0
    choice.mirror_choice(True)
    assert registry["key"]["CheckForUpdates"] == 1


def test_other_platforms_never_touch_the_registry(monkeypatch):
    monkeypatch.setattr(choice, "is_windows", lambda: False)
    monkeypatch.delitem(sys.modules, "winreg", raising=False)
    config = FakeConfig()

    choice.apply_installer_choice(config)
    choice.mirror_choice(True)

    assert config.data == {}
