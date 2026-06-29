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


def test_resolve_host_binary_prefers_sandbox(monkeypatch):
    monkeypatch.setattr(platform_utils.shutil, "which", lambda name: "/app/bin/bw" if name == "bw" else None)
    assert platform_utils.resolve_host_binary("bw") == ["/app/bin/bw"]


def test_resolve_host_binary_flatpak_host_fallback(monkeypatch):
    monkeypatch.setattr(platform_utils, "is_flatpak", lambda: True)
    monkeypatch.setattr(platform_utils.shutil, "which", lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None)

    def fake_run(argv, **kwargs):
        assert argv[:3] == ["/usr/bin/flatpak-spawn", "--host", "which"]
        class R:
            returncode = 0
            stdout = "/home/u/.npm-global/bin/bw\n"
        return R()

    monkeypatch.setattr(platform_utils.subprocess, "run", fake_run)
    assert platform_utils.resolve_host_binary("bw") == ["/usr/bin/flatpak-spawn", "--host", "bw"]


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

