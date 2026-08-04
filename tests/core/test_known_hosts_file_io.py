"""Atomic known-hosts byte read/write tests."""

import os
import stat
import tempfile
from pathlib import Path

import pytest

from sshpilot.core.errors import CoreError, ErrorCode
from sshpilot.core.known_hosts.file_io import (
    atomic_write_known_hosts_bytes,
    read_known_hosts_bytes,
)

CONTENT = b"example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI\n"


class _BytesSubclass(bytes):
    pass


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


def test_read_converts_os_errors(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    path.write_bytes(CONTENT)

    def fail_read_bytes(*_args, **_kwargs):
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    with pytest.raises(CoreError) as excinfo:
        read_known_hosts_bytes(path)
    assert excinfo.value.code is ErrorCode.KNOWN_HOST_IO_ERROR
    assert str(tmp_path) not in str(excinfo.value)


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


# ---------------------------------------------------------------------------
# Hardening: input validation, safe errors, and failure cleanup
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad", [bytearray(b"x"), "text", 123, None, _BytesSubclass(b"x")]
)
def test_write_rejects_non_bytes_content(tmp_path, bad):
    path = tmp_path / "known_hosts"
    with pytest.raises(TypeError):
        atomic_write_known_hosts_bytes(path, bad)
    assert not path.exists()


def test_mkstemp_failure_becomes_core_error(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"

    def fail_mkstemp(*args, **kwargs):
        raise OSError("simulated mkstemp failure")

    monkeypatch.setattr(tempfile, "mkstemp", fail_mkstemp)
    with pytest.raises(CoreError) as excinfo:
        atomic_write_known_hosts_bytes(path, CONTENT)
    assert excinfo.value.code is ErrorCode.KNOWN_HOST_IO_ERROR
    assert not path.exists()
    assert _temp_files(path) == []


def test_existing_target_metadata_failure_is_error(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    path.write_bytes(b"old\n")
    real_lstat = os.lstat

    def fail_lstat(target, *args, **kwargs):
        if os.fspath(target) == os.fspath(path):
            raise OSError("simulated lstat failure")
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", fail_lstat)
    with pytest.raises(CoreError) as excinfo:
        atomic_write_known_hosts_bytes(path, CONTENT)
    assert excinfo.value.code is ErrorCode.KNOWN_HOST_IO_ERROR
    assert path.read_bytes() == b"old\n"
    assert _temp_files(path) == []


def test_error_message_has_no_paths(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    path.write_bytes(b"old\n")

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(CoreError) as excinfo:
        atomic_write_known_hosts_bytes(path, CONTENT)
    message = str(excinfo.value)
    assert str(tmp_path) not in message
    assert str(path) not in message


def test_symlink_error_has_no_path(tmp_path):
    target = tmp_path / "real"
    target.write_bytes(b"keep")
    link = tmp_path / "known_hosts"
    link.symlink_to(target)
    with pytest.raises(CoreError) as excinfo:
        atomic_write_known_hosts_bytes(link, CONTENT)
    assert str(link) not in str(excinfo.value)
    assert str(target) not in str(excinfo.value)


def test_symlink_swap_detected_before_replacement(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    path.write_bytes(b"old\n")
    real_islink = os.path.islink
    calls = {"n": 0}

    def fake_islink(target, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:  # the pre-replace re-check sees a symlink
            return True
        return real_islink(target, *args, **kwargs)

    monkeypatch.setattr(os.path, "islink", fake_islink)
    with pytest.raises(CoreError) as excinfo:
        atomic_write_known_hosts_bytes(path, CONTENT)
    assert excinfo.value.code is ErrorCode.KNOWN_HOST_IO_ERROR
    assert path.read_bytes() == b"old\n"  # target never replaced
    assert _temp_files(path) == []  # temp file cleaned up


def test_temp_descriptor_and_file_cleaned_after_write_failure(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    path.write_bytes(b"old\n")
    real_fsync = os.fsync

    def fail_file_fsync(fd):
        # Only break the temp-file fsync; the parent-dir fsync is irrelevant
        # here because the failure happens before replace.
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("simulated write fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_file_fsync)
    with pytest.raises(CoreError):
        atomic_write_known_hosts_bytes(path, CONTENT)
    assert path.read_bytes() == b"old\n"
    assert _temp_files(path) == []


def test_parent_dir_opened_with_o_directory(tmp_path, monkeypatch):
    if not hasattr(os, "O_DIRECTORY"):
        pytest.skip("os.O_DIRECTORY not supported on this platform")
    path = tmp_path / "known_hosts"
    opened_flags = []
    real_open = os.open

    def fake_open(target, flags, *args, **kwargs):
        result = real_open(target, flags, *args, **kwargs)
        try:
            if os.path.isdir(target):
                opened_flags.append(flags)
        except OSError:
            pass
        return result

    monkeypatch.setattr(os, "open", fake_open)
    atomic_write_known_hosts_bytes(path, CONTENT)
    assert opened_flags
    assert all(flags & os.O_DIRECTORY for flags in opened_flags)
