from __future__ import annotations

import os
import sys

from sshpilot.platform import fs


def test_makedirs_private_posix_passes_0700(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(fs.sys, "platform", "linux")
    monkeypatch.setattr(fs.os, "makedirs", lambda path, **kw: seen.append((path, kw)))
    fs.makedirs_private(tmp_path / "d")
    assert seen == [(tmp_path / "d", {"mode": 0o700, "exist_ok": True})]


def test_makedirs_private_windows_inherits_the_parent_acl(monkeypatch, tmp_path):
    # mkdir(mode=0o700) on Windows adds an OWNER RIGHTS entry that OpenSSH
    # refuses on files written inside, so no mode is passed there.
    seen = []
    monkeypatch.setattr(fs.sys, "platform", "win32")
    monkeypatch.setattr(fs.os, "makedirs", lambda path, **kw: seen.append((path, kw)))
    fs.makedirs_private(tmp_path / "d")
    assert seen == [(tmp_path / "d", {"exist_ok": True})]


def test_makedirs_private_creates_nested_and_tolerates_existing(tmp_path):
    target = tmp_path / "a" / "b"
    fs.makedirs_private(target)
    fs.makedirs_private(target)
    assert target.is_dir()
    if sys.platform != "win32":
        assert (os.stat(target).st_mode & 0o777) == 0o700 & ~_umask()


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def test_fsync_directory_is_a_no_op_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(fs.sys, "platform", "win32")

    def must_not_open(*args, **kwargs):
        raise AssertionError("Windows cannot open a directory")

    monkeypatch.setattr(fs.os, "open", must_not_open)
    fs.fsync_directory(tmp_path)
