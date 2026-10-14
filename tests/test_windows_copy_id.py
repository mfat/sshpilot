"""Key deployment over plain ssh where Windows OpenSSH has no ssh-copy-id."""

from __future__ import annotations

import shlex
import shutil
import subprocess

import pytest

from sshpilot.platform.windows import copy_id

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFoo me@host \"q\" $x `y` 'z'"


def test_argv_drops_copy_id_flags_and_keeps_ssh_options():
    argv = copy_id.ssh_argv_from_copy_id(
        [
            "ssh-copy-id", "-F", "cfg", "-o", "User=root", "-p", "2222",
            "-f", "-i", "key.pub", "-o", "UserKnownHostsFile=kh", "root@host",
        ],
        "ssh.exe",
        KEY,
        force=True,
    )
    assert argv[:-1] == [
        "ssh.exe", "-F", "cfg", "-o", "User=root", "-p", "2222",
        "-o", "UserKnownHostsFile=kh", "-o", "RequestTTY=no", "--", "root@host",
    ]
    assert argv[-1] == copy_id.install_command(KEY, force=True)


def test_resolve_ssh_is_windows_only(monkeypatch):
    monkeypatch.setattr(copy_id.sys, "platform", "linux")
    assert copy_id.resolve_ssh() is None


def test_read_public_key_rejects_non_keys(tmp_path):
    path = tmp_path / "k.pub"
    path.write_text(KEY + "\n", encoding="utf-8")
    assert copy_id.read_public_key(str(path)) == KEY
    path.write_text("junk\n", encoding="utf-8")
    with pytest.raises(ValueError):
        copy_id.read_public_key(str(path))


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_install_script_appends_once_and_keeps_the_key_verbatim(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "LOGNAME": "me", "PATH": _sh_path()}

    def run(force):
        command = copy_id.install_command(KEY, force=force)
        return subprocess.run(
            ["sh", "-c", command], env=env, capture_output=True, text=True,
            check=True,
        ).stdout.strip()

    (home / ".ssh").mkdir()
    (home / ".ssh" / "authorized_keys").write_text("ssh-rsa AAAAB3 old", "utf-8")
    assert run(False) == copy_id.ADDED_MESSAGE
    assert run(False) == copy_id.SKIPPED_MESSAGE
    assert run(True) == copy_id.ADDED_MESSAGE
    content = (home / ".ssh" / "authorized_keys").read_text("utf-8")
    assert content == f"ssh-rsa AAAAB3 old\n{KEY}\n{KEY}\n"


def _sh_path():
    import os

    return os.environ.get("PATH", "")


def test_install_command_is_one_quoted_sh_invocation():
    words = shlex.split(copy_id.install_command(KEY))
    assert words[:3] == ["exec", "sh", "-c"] and len(words) == 4
