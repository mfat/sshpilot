"""Atomic known-hosts byte read/write tests."""

import os
import stat

import pytest

from sshpilot.core.errors import CoreError, ErrorCode
from sshpilot.core.known_hosts.file_io import (
    atomic_write_known_hosts_bytes,
    read_known_hosts_bytes,
)

CONTENT = b"example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI\n"


def _temp_files(path):
    return [p for p in path.parent.iterdir() if p.name.startswith(".known_hosts-")]


# ---------------------------------------------------------------------------
# Read behavior
# ---------------------------------------------------------------------------
def test_missing_file_reads_empty(tmp_path):
    assert read_known_hosts_bytes(tmp_path / "known_hosts") == b""


def test_normal_read(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_bytes(CONTENT)
    assert read_known_hosts_bytes(path) == CONTENT


def test_read_converts_os_errors(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_bytes(CONTENT)
    path.chmod(0o000)
    try:
        with pytest.raises(CoreError) as excinfo:
            read_known_hosts_bytes(path)
        assert excinfo.value.code is ErrorCode.KNOWN_HOST_IO_ERROR
    finally:
        path.chmod(0o600)


# ---------------------------------------------------------------------------
# Write behavior
# ---------------------------------------------------------------------------
def test_new_write_creates_with_0644(tmp_path):
    path = tmp_path / "sub" / "known_hosts"  # parent must be created
    atomic_write_known_hosts_bytes(path, CONTENT)
    assert path.read_bytes() == CONTENT
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_write_preserves_existing_mode(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_bytes(b"old\n")
    path.chmod(0o600)
    atomic_write_known_hosts_bytes(path, CONTENT)
    assert path.read_bytes() == CONTENT
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_refuses_symlink_destination(tmp_path):
    target = tmp_path / "real"
    target.write_bytes(b"keep")
    link = tmp_path / "known_hosts"
    link.symlink_to(target)
    with pytest.raises(CoreError) as excinfo:
        atomic_write_known_hosts_bytes(link, CONTENT)
    assert excinfo.value.code is ErrorCode.KNOWN_HOST_IO_ERROR
    assert target.read_bytes() == b"keep"


def test_write_replaces_atomically_and_leaves_no_temp(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_bytes(b"old\n")
    atomic_write_known_hosts_bytes(path, CONTENT)
    assert path.read_bytes() == CONTENT
    assert _temp_files(path) == []


def test_write_fsyncs_parent_directory(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    fsynced_fds = []
    real_fsync = os.fsync

    def fake_fsync(fd):
        fsynced_fds.append(fd)
        return real_fsync(fd)

    opened_dirs = []
    real_open = os.open

    def fake_open(target, flags, *args, **kwargs):
        result = real_open(target, flags, *args, **kwargs)
        try:
            if os.path.isdir(target):
                opened_dirs.append(result)
        except OSError:
            pass
        return result

    monkeypatch.setattr(os, "fsync", fake_fsync)
    monkeypatch.setattr(os, "open", fake_open)
    atomic_write_known_hosts_bytes(path, CONTENT)
    assert opened_dirs, "parent directory was never opened"
    # The directory fd must have been fsynced (its own fsync call recorded).
    assert any(fd in fsynced_fds for fd in opened_dirs)


def test_write_cleans_temp_after_simulated_failure(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    path.write_bytes(b"old\n")

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(CoreError) as excinfo:
        atomic_write_known_hosts_bytes(path, CONTENT)
    assert excinfo.value.code is ErrorCode.KNOWN_HOST_IO_ERROR
    # Original file untouched, no temp file left behind.
    assert path.read_bytes() == b"old\n"
    assert _temp_files(path) == []


def test_write_converts_parent_fsync_errors(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    real_fsync = os.fsync

    def fail_dir_fsync(fd):
        # Only break the parent-directory fsync; the temp-file fsync (a
        # regular file) must still succeed so the write reaches replace.
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_dir_fsync)
    with pytest.raises(CoreError) as excinfo:
        atomic_write_known_hosts_bytes(path, CONTENT)
    assert excinfo.value.code is ErrorCode.KNOWN_HOST_IO_ERROR
    # The replace already happened atomically before the dir fsync attempt.
    assert path.read_bytes() == CONTENT
    assert _temp_files(path) == []
