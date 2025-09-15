import os

from sshpilot import platform_utils


def test_is_flatpak_env(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "io.github.mfat.sshpilot")
    monkeypatch.setattr(platform_utils.os.path, "exists", lambda path: False)
    assert platform_utils.is_flatpak() is True


def test_is_flatpak_file(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(platform_utils.os.path, "exists", lambda path: path == "/.flatpak-info")
    assert platform_utils.is_flatpak() is True


def test_is_flatpak_false(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(platform_utils.os.path, "exists", lambda path: False)
    assert platform_utils.is_flatpak() is False


def test_get_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(
        platform_utils.GLib,
        "get_user_config_dir",
        lambda: str(tmp_path / "conf"),
        raising=False,
    )
    expected = os.path.join(str(tmp_path / "conf"), "sshpilot")
    assert platform_utils.get_config_dir() == expected


def test_get_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(
        platform_utils.GLib,
        "get_user_data_dir",
        lambda: str(tmp_path / "data"),
        raising=False,
    )
    expected = os.path.join(str(tmp_path / "data"), "sshpilot")
    assert platform_utils.get_data_dir() == expected


def test_get_ssh_dir_default(monkeypatch, tmp_path):
    monkeypatch.delenv("SSHPILOT_SSH_DIR", raising=False)
    monkeypatch.setattr(
        platform_utils.GLib,
        "get_home_dir",
        lambda: str(tmp_path),
        raising=False,
    )
    expected = os.path.join(str(tmp_path), ".ssh")
    assert platform_utils.get_ssh_dir() == expected


def test_get_ssh_dir_override(monkeypatch, tmp_path):
    override = tmp_path / "custom_ssh"
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(override))
    monkeypatch.setattr(
        platform_utils.GLib,
        "get_home_dir",
        lambda: "ignored",
        raising=False,
    )
    assert platform_utils.get_ssh_dir() == str(override)

