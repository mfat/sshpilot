from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.operations import (
    SftpFileTarget,
    SftpReadFileRequest,
    SftpReplaceFileRequest,
)
from sshpilot.daemon.sftp_runtime import SftpServiceRuntime


def test_local_authorized_keys_read_is_bounded_and_revisioned(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    path = ssh_dir / "authorized_keys"
    path.write_text("comment\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    runtime = SftpServiceRuntime(SimpleNamespace())
    result = runtime.read_file(
        SftpReadFileRequest(SftpFileTarget.LOCAL_AUTHORIZED_KEYS, "~/.ssh/authorized_keys"),
        client_id=ClientId("client:test"),
    )

    assert result.exists is True
    assert result.content == "comment\n"
    assert result.revision
    assert result.size == len(result.content.encode())


def test_local_replace_rejects_stale_revision(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    path = ssh_dir / "authorized_keys"
    path.write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    runtime = SftpServiceRuntime(SimpleNamespace())
    with pytest.raises(SshPilotError) as raised:
        runtime.replace_file(
            SftpReplaceFileRequest(
                SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
                "~/.ssh/authorized_keys",
                "new\n",
                "wrong-revision",
                backup=True,
            ),
            client_id=ClientId("client:test"),
        )
    assert raised.value.code is ErrorCode.FILE_REVISION_CONFLICT
    assert path.read_text(encoding="utf-8") == "old\n"


def test_local_replace_is_atomic_secure_and_backed_up(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o755)
    path = ssh_dir / "authorized_keys"
    path.write_text("old\n", encoding="utf-8")
    os.chmod(path, 0o644)
    monkeypatch.setenv("HOME", str(tmp_path))

    runtime = SftpServiceRuntime(SimpleNamespace())
    old = runtime.read_file(
        SftpReadFileRequest(SftpFileTarget.LOCAL_AUTHORIZED_KEYS, "~/.ssh/authorized_keys"),
        client_id=ClientId("client:test"),
    )
    result = runtime.replace_file(
        SftpReplaceFileRequest(
            SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
            "~/.ssh/authorized_keys",
            "new\n",
            old.revision,
            backup=True,
        ),
        client_id=ClientId("client:test"),
    )

    assert path.read_text(encoding="utf-8") == "new\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert ssh_dir.stat().st_mode & 0o777 == 0o700
    assert result.backup_path
    assert Path(result.backup_path).read_text(encoding="utf-8") == "old\n"
    assert not list(ssh_dir.glob("*.sshpilot.tmp-*"))


def test_local_target_rejects_arbitrary_paths():
    with pytest.raises(ValueError):
        SftpReadFileRequest(SftpFileTarget.LOCAL_AUTHORIZED_KEYS, "/etc/passwd")
