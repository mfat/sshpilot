"""Launch -F fallback when ~/.ssh/config ownership is untrusted in a sandbox."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sshpilot.core.ssh_config_effective import (
    launch_config_file,
    materialize_untrusted_user_config_launch_file,
    trusted_default_user_ssh_config_owner,
)
from sshpilot.platform import paths

pytestmark = pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")


def test_trusted_owner_checks_uid_only(tmp_path):
    config = tmp_path / "config"
    config.write_text("Host t\n  HostName 127.0.0.1\n")
    assert trusted_default_user_ssh_config_owner(str(config)) is True


def test_materialize_wrapper_includes_user_and_system(tmp_path, monkeypatch):
    store = tmp_path / "store" / "hm_.sshconfig"
    store.parent.mkdir(parents=True)
    store.write_text("Host plain\n    HostName 192.0.2.10\n")

    home_ssh = tmp_path / ".ssh"
    home_ssh.mkdir()
    link = home_ssh / "config"
    link.symlink_to(store)

    launch_dir = tmp_path / "cfg"
    monkeypatch.setattr(paths, "get_config_dir", lambda: launch_dir)

    wrapper = materialize_untrusted_user_config_launch_file(str(link))
    text = Path(wrapper).read_text(encoding="utf-8")
    assert f'Include "{store.resolve()}"' in text
    system = shutil.which("ssh")
    assert system is not None
    result = subprocess.run(
        ["ssh", "-G", "-F", wrapper, "plain"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("hostname ") for line in result.stdout.splitlines())


def test_untrusted_owner_launch_matches_openssh(tmp_path, monkeypatch):
    """Wrapper -F should resolve like plain ssh when ownership is mocked untrusted."""
    home_ssh = tmp_path / ".ssh"
    home_ssh.mkdir()
    root = home_ssh / "config"
    included = tmp_path / "included.conf"
    included.write_text("Host plain\n    HostName 192.0.2.10\n")
    root.write_text(f'Include "{included}"\n')
    monkeypatch.setattr(paths, "openssh_user_dir", lambda: home_ssh)
    launch_dir = tmp_path / "cfg"
    monkeypatch.setattr(paths, "get_config_dir", lambda: launch_dir)
    monkeypatch.setattr(
        "sshpilot.core.ssh_config_effective.trusted_default_user_ssh_config_owner",
        lambda _path: False,
    )

    launch_file = launch_config_file(root)
    assert launch_file is not None
    assert launch_file.endswith(".conf")
    assert "Include" in Path(launch_file).read_text(encoding="utf-8")

    direct = subprocess.run(
        ["ssh", "-G", "-F", str(root), "plain"],
        capture_output=True,
        text=True,
    )
    wrapped = subprocess.run(
        ["ssh", "-G", "-F", launch_file, "plain"],
        capture_output=True,
        text=True,
    )
    assert direct.returncode == 0, direct.stderr
    assert wrapped.returncode == 0, wrapped.stderr

    def hostname_map(stdout: str) -> str:
        for line in stdout.splitlines():
            if line.startswith("hostname "):
                return line.split(maxsplit=1)[1]
        return ""

    assert hostname_map(wrapped.stdout) == hostname_map(direct.stdout)