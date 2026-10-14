"""Windows directory resolution in the headless path helpers."""

from __future__ import annotations

from pathlib import Path

from sshpilot.platform import paths


def test_windows_dirs_use_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert paths.get_config_dir() == tmp_path / "Roaming" / "sshpilot"
    assert paths.get_data_dir() == tmp_path / "Local" / "sshpilot"
    assert paths.get_state_dir() == tmp_path / "Local" / "sshpilot" / "state"
    assert paths.get_runtime_dir() == tmp_path / "Local" / "sshpilot" / "run"


def test_xdg_override_wins_everywhere(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert paths.get_config_dir() == Path(tmp_path) / "sshpilot"


def test_posix_default_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(paths, "get_home_dir", lambda: tmp_path)
    assert paths.get_config_dir() == tmp_path / ".config" / "sshpilot"


def test_dbus_autolaunch_is_disabled_on_windows_only(monkeypatch):
    from sshpilot.platform.windows import environment

    monkeypatch.setattr(environment.sys, "platform", "win32")
    env = {}
    assert environment.disable_dbus_autolaunch(env)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "disabled:"
    kept = {"DBUS_SESSION_BUS_ADDRESS": "tcp:host=localhost,port=1"}
    assert not environment.disable_dbus_autolaunch(kept)
    assert kept["DBUS_SESSION_BUS_ADDRESS"].startswith("tcp:")
    monkeypatch.setattr(environment.sys, "platform", "linux")
    other = {}
    assert not environment.disable_dbus_autolaunch(other) and other == {}
