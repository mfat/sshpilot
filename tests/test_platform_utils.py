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
