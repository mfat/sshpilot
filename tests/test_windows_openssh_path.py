"""The built-in OpenSSH for Windows must win over MSYS copies on PATH."""

from __future__ import annotations

import os

from sshpilot.platform.windows import openssh


def _install_fake_builtin(monkeypatch, tmp_path):
    root = tmp_path / "Windows"
    directory = root / "System32" / "OpenSSH"
    directory.mkdir(parents=True)
    (directory / "ssh.exe").write_bytes(b"")
    monkeypatch.setattr(openssh.sys, "platform", "win32")
    return str(root), str(directory)


def test_builtin_moves_ahead_of_git_for_windows(monkeypatch, tmp_path):
    root, directory = _install_fake_builtin(monkeypatch, tmp_path)
    git = str(tmp_path / "Git" / "usr" / "bin")
    other = str(tmp_path / "other")
    env = {"SystemRoot": root, "PATH": os.pathsep.join([git, directory, other])}
    assert openssh.prefer_builtin_openssh(env) is True
    assert env["PATH"].split(os.pathsep) == [directory, git, other]
    assert openssh.prefer_builtin_openssh(env) is False  # idempotent


def test_builtin_added_when_missing_from_path(monkeypatch, tmp_path):
    root, directory = _install_fake_builtin(monkeypatch, tmp_path)
    env = {"SystemRoot": root, "PATH": str(tmp_path / "other")}
    assert openssh.prefer_builtin_openssh(env) is True
    assert env["PATH"].split(os.pathsep)[0] == directory


def test_no_change_without_builtin_client(monkeypatch, tmp_path):
    monkeypatch.setattr(openssh.sys, "platform", "win32")
    env = {"SystemRoot": str(tmp_path), "PATH": "C:\\Git\\usr\\bin"}
    assert openssh.prefer_builtin_openssh(env) is False
    assert env["PATH"] == "C:\\Git\\usr\\bin"


def test_no_change_off_windows(monkeypatch):
    monkeypatch.setattr(openssh.sys, "platform", "linux")
    env = {"PATH": "/usr/bin"}
    assert openssh.prefer_builtin_openssh(env) is False
    assert env["PATH"] == "/usr/bin"
