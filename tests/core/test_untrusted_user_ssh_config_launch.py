"""Launch -F fallback when ~/.ssh/config ownership is untrusted in a sandbox."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sshpilot.core.ssh_config_effective import (
    launch_config_file,
    trusted_default_user_ssh_config_owner,
)
from sshpilot.platform import paths

pytestmark = pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")


def test_trusted_owner_checks_uid_only(tmp_path):
    config = tmp_path / "config"
    config.write_text("Host t\n  HostName 127.0.0.1\n")
    assert trusted_default_user_ssh_config_owner(str(config)) is True


def test_untrusted_owner_uses_resolved_path_not_include_wrapper(tmp_path, monkeypatch):
    """OpenSSH checks Include targets; pass the store file directly to -F (#1384)."""
    store = tmp_path / "store" / "hm_.sshconfig"
    store.parent.mkdir(parents=True)
    store.write_text("Host plain\n    HostName 192.0.2.10\n")
    home_ssh = tmp_path / ".ssh"
    home_ssh.mkdir()
    root = home_ssh / "config"
    root.symlink_to(store)
    monkeypatch.setattr(paths, "openssh_user_dir", lambda: home_ssh)
    monkeypatch.setattr(
        "sshpilot.core.ssh_config_effective.trusted_default_user_ssh_config_owner",
        lambda _path: False,
    )

    launch_file = launch_config_file(root)
    assert launch_file == str(store.resolve())

    result = subprocess.run(
        ["ssh", "-G", "-F", launch_file, "plain"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert any(line == "hostname 192.0.2.10" for line in result.stdout.splitlines())


def test_include_of_same_file_still_subject_to_openssh_checks(tmp_path):
    """Document why launch must not wrap the store file in an Include (#1384)."""
    store = tmp_path / "hm_.sshconfig"
    store.write_text("Host plain\n    HostName 192.0.2.10\n", encoding="utf-8")
    wrapper = tmp_path / "wrapper.conf"
    wrapper.write_text(f'Include "{store}"\n', encoding="utf-8")

    direct = subprocess.run(
        ["ssh", "-G", "-F", str(store), "plain"],
        capture_output=True,
        text=True,
    )
    wrapped = subprocess.run(
        ["ssh", "-G", "-F", str(wrapper), "plain"],
        capture_output=True,
        text=True,
    )
    assert direct.returncode == 0, direct.stderr
    # Same file, same owner: both work here. Include path still checks included st_uid;
    # wrappers do not bypass UID 65534 on the store blob (see OpenSSH readconf.c).
    assert wrapped.returncode == 0, wrapped.stderr