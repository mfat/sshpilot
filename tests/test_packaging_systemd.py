"""Packaging checks for the optional systemd user unit."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "packaging" / "systemd" / "sshpilot-daemon.service"
LAUNCHER = ROOT / "sshpilot-daemon"


def test_systemd_service_file_exists():
    assert SERVICE.is_file()


def test_systemd_service_has_runtime_directory():
    content = SERVICE.read_text(encoding="utf-8")
    assert "RuntimeDirectory=sshpilot" in content
    assert "ExecStart=sshpilot-daemon" in content


def test_daemon_launcher_exists_and_is_executable():
    assert LAUNCHER.is_file()
    assert LAUNCHER.stat().st_mode & 0o111
