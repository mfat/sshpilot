"""Tests for the authorized_keys service layer.

Covers:
- ``LocalAuthorizedKeysService`` against a real tmp filesystem (load, save,
  backup-on-first-save, no-double-backup, atomic write, chmod 0600 / 0700).
- ``AuthorizedKeysService`` against a fake paramiko SFTP/SSH client that
  records calls, so we can assert backup, atomic posix_rename, chmod, and the
  ``mkdir -p && chmod 700`` exec_command without needing a real host.
"""

from __future__ import annotations

import io
import os
import stat
from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

# Some sshpilot modules import paramiko at import time. The service module
# itself does not, so import directly.
from sshpilot.authorized_keys_parser import AuthorizedKeyEntry, parse_file
from sshpilot.authorized_keys_service import (
    AuthorizedKeysService,
    LocalAuthorizedKeysService,
)


KEY_BLOB = (
    "AAAAC3NzaC1lZDI1NTE5AAAAIK1eKZmcG3zP7Q9XzYx0sLh9G6mP6Q3OwPgkqJx6r1Pq"
)

SAMPLE = (
    f"ssh-ed25519 {KEY_BLOB} alice\n"
    f'command="/bin/true",from="10.0.0.0/8" ssh-ed25519 {KEY_BLOB} bob\n'
    f'weird-future-option="x,y" ssh-ed25519 {KEY_BLOB} carol\n'
)


# ---------------------------------------------------------------------------
# LocalAuthorizedKeysService
# ---------------------------------------------------------------------------


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_local_load_returns_empty_when_ssh_dir_missing(tmp_path):
    path = tmp_path / "no-ssh" / "authorized_keys"
    svc = LocalAuthorizedKeysService(str(path))
    items = svc.load().result(timeout=2)
    assert items == []


def test_local_load_returns_empty_when_file_missing(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    svc = LocalAuthorizedKeysService(str(ssh / "authorized_keys"))
    assert svc.load().result(timeout=2) == []


def test_local_load_parses_existing_file(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    ak = ssh / "authorized_keys"
    ak.write_text(SAMPLE, encoding="utf-8")
    svc = LocalAuthorizedKeysService(str(ak))
    items = svc.load().result(timeout=2)
    entries = [it for it in items if isinstance(it, AuthorizedKeyEntry)]
    assert len(entries) == 3
    assert entries[1].get_option("command") == "/bin/true"
    assert entries[2].unknown_options == ['weird-future-option="x,y"']


def test_local_save_creates_ssh_dir_and_writes_file(tmp_path):
    ssh = tmp_path / ".ssh"
    ak = ssh / "authorized_keys"
    svc = LocalAuthorizedKeysService(str(ak))
    items = parse_file(SAMPLE)
    svc.save(items, make_backup=True).result(timeout=2)

    assert ak.exists()
    assert ak.read_text(encoding="utf-8") == SAMPLE
    assert _mode(str(ak)) == 0o600
    assert _mode(str(ssh)) == 0o700


def test_local_save_backs_up_existing_file_once_per_session(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir(mode=0o700)
    ak = ssh / "authorized_keys"
    original = "ssh-ed25519 AAAA original\n"
    ak.write_text(original, encoding="utf-8")

    svc = LocalAuthorizedKeysService(str(ak))
    items = parse_file(SAMPLE)
    svc.save(items, make_backup=True).result(timeout=2)

    # Exactly one backup file exists with the original content.
    backups = list(ssh.glob("authorized_keys.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original

    # Save again — no second backup, file updated in place.
    svc.save(items, make_backup=True).result(timeout=2)
    backups_after = list(ssh.glob("authorized_keys.bak-*"))
    assert len(backups_after) == 1, "second save should not create another backup"


def test_local_save_backup_is_copy_not_rename(tmp_path):
    """The live authorized_keys must stay in place during backup; only
    the final atomic replace should swap it out."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir(mode=0o700)
    ak = ssh / "authorized_keys"
    original_ino = None
    ak.write_text("original\n", encoding="utf-8")
    original_ino = ak.stat().st_ino

    svc = LocalAuthorizedKeysService(str(ak))
    svc.save(parse_file(SAMPLE), make_backup=True).result(timeout=2)

    # The target was atomically replaced with a new file (different inode),
    # but the backup is a *new* file too — not the old inode renamed.
    backups = list(ssh.glob("authorized_keys.bak-*"))
    assert len(backups) == 1
    assert backups[0].stat().st_ino != original_ino
    # And the new live file has the new content + 600 mode.
    assert ak.read_text(encoding="utf-8") == SAMPLE
    assert _mode(str(ak)) == 0o600
    assert _mode(str(backups[0])) == 0o600


def test_local_save_no_backup_when_target_missing(tmp_path):
    ssh = tmp_path / ".ssh"
    ak = ssh / "authorized_keys"
    svc = LocalAuthorizedKeysService(str(ak))
    svc.save(parse_file(SAMPLE), make_backup=True).result(timeout=2)
    assert not list(ssh.glob("authorized_keys.bak-*"))


def test_local_save_leaves_no_tmp_file(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir(mode=0o700)
    ak = ssh / "authorized_keys"
    svc = LocalAuthorizedKeysService(str(ak))
    svc.save(parse_file(SAMPLE), make_backup=False).result(timeout=2)
    assert not list(ssh.glob("*.sshpilot.tmp"))


def test_local_load_propagates_unexpected_errors(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    ak = ssh / "authorized_keys"
    ak.write_text("ok\n", encoding="utf-8")
    ak.chmod(0o000)  # unreadable
    try:
        svc = LocalAuthorizedKeysService(str(ak))
        fut = svc.load()
        # PermissionError should surface via the Future.
        if os.geteuid() == 0:
            pytest.skip("root can read unreadable files; cannot test perms")
        with pytest.raises(PermissionError):
            fut.result(timeout=2)
    finally:
        ak.chmod(0o600)


# ---------------------------------------------------------------------------
# AuthorizedKeysService (SFTP) with mocks
# ---------------------------------------------------------------------------


class _FakeSFTPFile(io.BytesIO):
    """Mimics paramiko's SFTPFile context manager surface."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class FakeSFTP:
    """In-memory paramiko.SFTPClient stand-in for service-layer tests."""

    def __init__(self, home="/home/test"):
        self._home = home
        # filename -> bytes
        self.files: dict[str, bytes] = {}
        # filename -> mode
        self.modes: dict[str, int] = {}
        # directories that exist
        self.dirs = {home}
        self.calls: list[tuple[str, tuple]] = []

    def _record(self, name, *args):
        self.calls.append((name, args))

    def normalize(self, _path):
        self._record("normalize", _path)
        return self._home

    def stat(self, path):
        self._record("stat", path)
        if path in self.dirs or path in self.files:
            return MagicMock(st_mode=0)
        raise IOError(f"missing: {path}")

    def mkdir(self, path, mode=0o777):
        self._record("mkdir", path, mode)
        if path in self.dirs:
            raise IOError(f"already exists: {path}")
        self.dirs.add(path)
        self.modes[path] = mode

    def file(self, path, mode):
        self._record("file", path, mode)
        if "r" in mode:
            if path not in self.files:
                raise IOError(f"missing: {path}")
            return _FakeSFTPFile(self.files[path])

        # Write mode: create the file empty on open so chmod-before-write
        # has something to chmod. Capture bytes back to dict on close.
        outer = self
        outer.files.setdefault(path, b"")

        class _WriteHandle(_FakeSFTPFile):
            def close(self_inner):
                outer.files[path] = self_inner.getvalue()
                super().close()

        return _WriteHandle()

    def posix_rename(self, src, dst):
        self._record("posix_rename", src, dst)
        if src not in self.files:
            raise IOError(f"missing rename src: {src}")
        self.files[dst] = self.files.pop(src)
        # posix_rename preserves source mode.
        if src in self.modes:
            self.modes[dst] = self.modes.pop(src)

    def chmod(self, path, mode):
        self._record("chmod", path, mode)
        if path not in self.files and path not in self.dirs:
            raise IOError(f"chmod missing: {path}")
        self.modes[path] = mode


class FakeStdout:
    def __init__(self, status=0):
        self.channel = MagicMock()
        self.channel.recv_exit_status = MagicMock(return_value=status)


class FakeStderr:
    def read(self):
        return b""


class FakeSSH:
    """Minimal paramiko.SSHClient stand-in."""

    def __init__(self, exit_status=0):
        self.exit_status = exit_status
        self.commands: list[str] = []

    def exec_command(self, cmd):
        self.commands.append(cmd)
        return None, FakeStdout(self.exit_status), FakeStderr()


class FakeManager:
    """AsyncSFTPManager surface needed by AuthorizedKeysService."""

    def __init__(self, sftp: FakeSFTP, ssh: FakeSSH):
        self._sftp = sftp
        self._client = ssh

    def _submit(self, func, *, on_success=None, on_error=None):
        fut: Future = Future()
        try:
            fut.set_result(func())
        except Exception as exc:
            fut.set_exception(exc)
        return fut


def test_sftp_load_returns_empty_when_ssh_dir_missing():
    sftp = FakeSFTP()
    sftp.dirs.discard(sftp._home)  # nothing exists
    svc = AuthorizedKeysService(FakeManager(sftp, FakeSSH()))
    items = svc.load().result(timeout=2)
    assert items == []


def test_sftp_load_returns_empty_when_file_missing():
    sftp = FakeSFTP()
    sftp.dirs.add("/home/test/.ssh")
    svc = AuthorizedKeysService(FakeManager(sftp, FakeSSH()))
    assert svc.load().result(timeout=2) == []


def test_sftp_load_parses_existing_file():
    sftp = FakeSFTP()
    sftp.dirs.add("/home/test/.ssh")
    sftp.files["/home/test/.ssh/authorized_keys"] = SAMPLE.encode("utf-8")
    svc = AuthorizedKeysService(FakeManager(sftp, FakeSSH()))
    items = svc.load().result(timeout=2)
    entries = [it for it in items if isinstance(it, AuthorizedKeyEntry)]
    assert len(entries) == 3


def test_sftp_save_writes_via_atomic_rename_and_chmods_600():
    sftp = FakeSFTP()
    svc = AuthorizedKeysService(FakeManager(sftp, FakeSSH()))
    svc.save(parse_file(SAMPLE), make_backup=False).result(timeout=2)

    ak = "/home/test/.ssh/authorized_keys"
    tmp = ak + ".sshpilot.tmp"
    # Target file present, tmp gone.
    assert ak in sftp.files
    assert tmp not in sftp.files
    # 600 was set on the tmp before the rename, and posix_rename preserves
    # source mode — so the live file ends up 0600 without a final chmod.
    assert sftp.modes.get(ak) == 0o600
    # Content matches.
    assert sftp.files[ak].decode("utf-8") == SAMPLE
    # mkdir over SFTP was attempted with mode 0700 (not via exec_command).
    mkdir_calls = [c for c in sftp.calls if c[0] == "mkdir"]
    assert any(args == ("/home/test/.ssh", 0o700) for _, args in mkdir_calls)
    # posix_rename was called (atomic move from tmp to target).
    rename_calls = [c for c in sftp.calls if c[0] == "posix_rename"]
    assert any(args == (tmp, ak) for _, args in rename_calls)


def test_sftp_save_does_not_use_shell_exec():
    """Regression: must not depend on a POSIX login shell on the remote."""
    sftp = FakeSFTP()
    ssh = FakeSSH()
    svc = AuthorizedKeysService(FakeManager(sftp, ssh))
    svc.save(parse_file(SAMPLE), make_backup=False).result(timeout=2)
    assert ssh.commands == [], "save() must not invoke exec_command"


def test_sftp_save_chmod_happens_before_write():
    """The tmp file must be 0600 BEFORE its content is written, so the
    keys are never readable by other users on the remote."""
    sftp = FakeSFTP()
    svc = AuthorizedKeysService(FakeManager(sftp, FakeSSH()))
    svc.save(parse_file(SAMPLE), make_backup=False).result(timeout=2)

    tmp = "/home/test/.ssh/authorized_keys.sshpilot.tmp"
    ak = "/home/test/.ssh/authorized_keys"

    # Find the chmod of the tmp file and the file-open in write mode.
    open_idx = next(
        i for i, c in enumerate(sftp.calls)
        if c[0] == "file" and c[1][0] == tmp and "w" in c[1][1]
    )
    chmod_idx = next(
        i for i, c in enumerate(sftp.calls)
        if c[0] == "chmod" and c[1][0] == tmp and c[1][1] == 0o600
    )
    rename_idx = next(
        i for i, c in enumerate(sftp.calls)
        if c[0] == "posix_rename" and c[1] == (tmp, ak)
    )
    # chmod must come after open (you can't chmod a non-existent file) but
    # before the final rename — i.e. during the empty-file window.
    assert open_idx < chmod_idx < rename_idx


def test_sftp_save_backs_up_via_copy_not_rename():
    """Backup must be a copy: the live authorized_keys must never be
    absent between backup and final install (lockout safety)."""
    sftp = FakeSFTP()
    ak = "/home/test/.ssh/authorized_keys"
    sftp.dirs.add("/home/test/.ssh")
    sftp.files[ak] = b"ssh-ed25519 AAAA original\n"
    svc = AuthorizedKeysService(FakeManager(sftp, FakeSSH()))

    svc.save(parse_file(SAMPLE), make_backup=True).result(timeout=2)

    backups = [k for k in sftp.files if k.startswith(ak + ".bak-")]
    assert len(backups) == 1
    assert sftp.files[backups[0]] == b"ssh-ed25519 AAAA original\n"
    # No posix_rename from ak to bak — only the tmp→ak swap.
    rename_calls = [c for c in sftp.calls if c[0] == "posix_rename"]
    rename_args = [args for _, args in rename_calls]
    assert all(args[0] != ak for args in rename_args), (
        "live authorized_keys must not be renamed away during save"
    )
    # Backup got mode 0600 too.
    assert sftp.modes.get(backups[0]) == 0o600


def test_sftp_save_backup_only_once_per_session():
    sftp = FakeSFTP()
    ak = "/home/test/.ssh/authorized_keys"
    sftp.dirs.add("/home/test/.ssh")
    sftp.files[ak] = b"original\n"
    svc = AuthorizedKeysService(FakeManager(sftp, FakeSSH()))

    svc.save(parse_file(SAMPLE), make_backup=True).result(timeout=2)
    svc.save(parse_file(SAMPLE), make_backup=True).result(timeout=2)
    backups = [k for k in sftp.files if k.startswith(ak + ".bak-")]
    assert len(backups) == 1


def test_sftp_save_succeeds_when_ssh_dir_already_exists():
    """mkdir over SFTP raises IOError if the dir exists; save must
    swallow that and continue (host with pre-existing ~/.ssh)."""
    sftp = FakeSFTP()
    sftp.dirs.add("/home/test/.ssh")  # already there
    svc = AuthorizedKeysService(FakeManager(sftp, FakeSSH()))
    svc.save(parse_file(SAMPLE), make_backup=False).result(timeout=2)
    assert sftp.files["/home/test/.ssh/authorized_keys"].decode("utf-8") == SAMPLE


def test_sftp_save_raises_when_not_connected():
    class _Disconnected:
        _sftp = None
        _client = None

        def _submit(self, func, **_kw):
            fut: Future = Future()
            try:
                fut.set_result(func())
            except Exception as exc:
                fut.set_exception(exc)
            return fut

    svc = AuthorizedKeysService(_Disconnected())
    with pytest.raises(RuntimeError):
        svc.save([], make_backup=False).result(timeout=2)


