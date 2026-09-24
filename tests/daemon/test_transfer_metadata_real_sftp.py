"""Transfers keep file permissions and modification times.

Uploads land through a temp file renamed over the target, so the target's
inode (and its mode) is replaced; downloads land through a ``0600`` mkstemp.
Before these were handled, re-uploading a private ``.env`` made it ``0664`` and
every download came out ``0600`` without ``+x``. Drives the transfer runtime
with the real client against OpenSSH's sftp-server binary.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest

from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.transfers import (
    StartTransferRequest,
    TransferConflictPolicy,
    TransferDirection,
    TransferState,
)
from sshpilot.daemon.transfer_runtime import TransferRuntime
from sshpilot.sftp.client import OpenSSHSFTPClient
from tests.daemon.test_transfer_runtime import (
    _make_ready_sftp_service,
    _wait_for_terminal_state,
)

_SFTP_SERVER = next(
    (
        path
        for path in (
            "/usr/lib/openssh/sftp-server",
            "/usr/libexec/openssh/sftp-server",
            "/usr/lib/ssh/sftp-server",
            "/usr/libexec/sftp-server",
        )
        if os.access(path, os.X_OK)
    ),
    shutil.which("sftp-server"),
)

pytestmark = pytest.mark.skipif(_SFTP_SERVER is None, reason="OpenSSH sftp-server not installed")

_UMASK = os.umask(0)
os.umask(_UMASK)
_OWNER = ClientId("client:owner")
_MTIME = 1_600_000_000


@pytest.fixture
def transfers():
    process = subprocess.Popen([_SFTP_SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    client = OpenSSHSFTPClient(process.stdin, process.stdout, on_close=process.terminate)
    client.start()
    sftp_runtime, service_id, _ = _make_ready_sftp_service(_OWNER, client)
    runtime = TransferRuntime(sftp_runtime)

    def run(direction, local_path, remote_path):
        prepared = runtime.prepare_start_transfer(
            StartTransferRequest(
                connection_id=ConnectionId("demo"),
                sftp_service_id=service_id,
                direction=direction,
                remote_path=str(remote_path),
                local_path=str(local_path),
                conflict_policy=TransferConflictPolicy.OVERWRITE,
            ),
            client_id=_OWNER,
        )
        runtime.run_transfer(prepared.id)
        summary = _wait_for_terminal_state(runtime, prepared.id, timeout=10.0)
        assert summary.state is TransferState.COMPLETED, summary

    try:
        yield run
    finally:
        client.close()
        process.wait(timeout=5)
        process.stdout.close()


def _make(path, mode, content=b"data\n"):
    path.write_bytes(content)
    os.chmod(path, mode)
    os.utime(path, (_MTIME, _MTIME))
    return path


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.parametrize("mode", [0o600, 0o755, 0o640])
def test_upload_over_existing_file_keeps_its_mode(transfers, tmp_path, mode):
    remote = _make(tmp_path / "remote.env", mode, b"OLD\n")
    local = _make(tmp_path / "local.env", 0o644, b"NEW\n")

    transfers(TransferDirection.UPLOAD, local, remote)

    assert remote.read_bytes() == b"NEW\n"
    assert _mode(remote) == mode


def test_new_upload_takes_the_local_mode_and_mtime(transfers, tmp_path):
    local = _make(tmp_path / "deploy.sh", 0o750, b"#!/bin/sh\n")
    remote = tmp_path / "remote" / "deploy.sh"
    remote.parent.mkdir()

    transfers(TransferDirection.UPLOAD, local, remote)

    assert remote.read_bytes() == b"#!/bin/sh\n"
    # The server's umask narrows a new file's mode, as with `sftp put`.
    assert _mode(remote) == 0o750 & ~_UMASK
    assert int(os.stat(remote).st_mtime) == _MTIME


def test_new_download_takes_the_remote_mode_and_mtime(transfers, tmp_path):
    remote = _make(tmp_path / "tool", 0o755, b"#!/bin/sh\n")
    local = tmp_path / "downloads" / "tool"
    local.parent.mkdir()

    transfers(TransferDirection.DOWNLOAD, local, remote)

    assert local.read_bytes() == b"#!/bin/sh\n"
    assert _mode(local) == 0o755 & ~_UMASK
    assert int(os.stat(local).st_mtime) == _MTIME


def test_download_over_existing_file_keeps_its_mode(transfers, tmp_path):
    remote = _make(tmp_path / "remote.txt", 0o644, b"NEW\n")
    local = _make(tmp_path / "local.txt", 0o600, b"OLD\n")

    transfers(TransferDirection.DOWNLOAD, local, remote)

    assert local.read_bytes() == b"NEW\n"
    assert _mode(local) == 0o600
