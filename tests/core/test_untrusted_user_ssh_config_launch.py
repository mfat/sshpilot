"""Launch -F fallback when ~/.ssh/config ownership is untrusted in a sandbox."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import types
from pathlib import Path

import pytest

from sshpilot.core.ssh_config_effective import (
    direct_f_workaround_for_unmapped_user_ssh_config,
    launch_config_file,
    trusted_default_user_ssh_config_owner,
)
from sshpilot.platform import paths

pytestmark = pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")


def _stat_result(uid: int, mode: int) -> os.stat_result:
    return types.SimpleNamespace(st_uid=uid, st_mode=mode)  # type: ignore[return-value]


def test_trusted_owner_checks_uid_only(tmp_path):
    config = tmp_path / "config"
    config.write_text("Host t\n  HostName 127.0.0.1\n")
    assert trusted_default_user_ssh_config_owner(str(config)) is True


def test_overflow_uid_flatpak_triggers_resolved_dash_f(tmp_path, monkeypatch):
    store = tmp_path / "store" / "hm_.sshconfig"
    store.parent.mkdir(parents=True)
    store.write_text("Host plain\n    HostName 192.0.2.10\n")
    home_ssh = tmp_path / ".ssh"
    home_ssh.mkdir()
    root = home_ssh / "config"
    root.symlink_to(store)
    monkeypatch.setattr(paths, "openssh_user_dir", lambda: home_ssh)
    real_stat = os.stat

    def fake_stat(path, *, follow_symlinks=True):
        if os.path.abspath(str(path)) in {
            os.path.abspath(str(root)),
            os.path.abspath(str(store)),
        }:
            return _stat_result(65534, stat.S_IFREG | 0o444)
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(
        "sshpilot.platform.detect.is_flatpak",
        lambda: True,
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


def test_foreign_owner_does_not_enable_dash_f_bypass(tmp_path, monkeypatch):
    config = tmp_path / ".ssh" / "config"
    config.parent.mkdir()
    config.write_text("Host plain\n    HostName 192.0.2.10\n")
    monkeypatch.setattr(paths, "openssh_user_dir", lambda: tmp_path / ".ssh")
    real_stat = os.stat

    def fake_stat(path, *, follow_symlinks=True):
        if os.path.abspath(str(path)) == os.path.abspath(str(config)):
            return _stat_result(1234, stat.S_IFREG | 0o444)
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", fake_stat)
    assert launch_config_file(config) is None


def test_overflow_uid_world_writable_does_not_enable_bypass(tmp_path, monkeypatch):
    config = tmp_path / ".ssh" / "config"
    config.parent.mkdir()
    config.write_text("Host plain\n    HostName 192.0.2.10\n")
    monkeypatch.setattr(paths, "openssh_user_dir", lambda: tmp_path / ".ssh")
    real_stat = os.stat

    def fake_stat(path, *, follow_symlinks=True):
        if os.path.abspath(str(path)) == os.path.abspath(str(config)):
            return _stat_result(65534, stat.S_IFREG | 0o666)
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr("sshpilot.platform.detect.is_flatpak", lambda: True)
    assert direct_f_workaround_for_unmapped_user_ssh_config(str(config)) is False
    assert launch_config_file(config) is None


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bubblewrap")
def test_user_namespace_include_still_checks_included_owner():
    """Included configs keep ownership checks; direct -F on overflow UID file does not."""
    target = "/etc/ssh/ssh_config"
    if not os.path.isfile(target):
        pytest.skip("no system ssh_config")
    wrapper = Path(os.environ.get("TMPDIR", "/tmp")) / "sshpilot-6982-wrapper.conf"
    wrapper.write_text(f'Include "{target}"\n', encoding="utf-8")
    uid = os.getuid()
    gid = os.getgid()
    bwrap = [
        "bwrap",
        "--unshare-user",
        "--uid",
        str(uid),
        "--gid",
        str(gid),
        "--ro-bind",
        "/",
        "/",
        "--bind",
        str(wrapper.parent),
        str(wrapper.parent),
        "--proc",
        "/proc",
        "--dev",
        "/dev",
    ]
    in_ns = subprocess.run(
        [*bwrap, "python3", "-c", f"import os; print(os.stat({target!r}).st_uid)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert in_ns.returncode == 0, in_ns.stderr
    assert in_ns.stdout.strip() == "65534"

    direct = subprocess.run(
        [*bwrap, "ssh", "-G", "-F", target, "localhost"],
        capture_output=True,
        text=True,
    )
    wrapped = subprocess.run(
        [*bwrap, "ssh", "-G", "-F", str(wrapper), "localhost"],
        capture_output=True,
        text=True,
    )
    # Direct -F reads the overflow-UID file as the -F root (the #1384 workaround).
    # A wrapper that Include`s the same path hits OpenSSH's include ownership check.
    assert "bad owner" in (wrapped.stderr or "").lower()