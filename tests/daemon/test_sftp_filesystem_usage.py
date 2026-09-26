"""``SftpServiceRuntime.filesystem_usage`` reports the remote filesystem's size.

Drives the runtime with the real client against OpenSSH's sftp-server binary,
which answers ``statvfs@openssh.com`` for the local disk the test can also
measure with ``os.statvfs``.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.operations import SftpPathRequest
from sshpilot.sftp.client import OpenSSHSFTPClient
from tests.daemon.test_transfer_runtime import _make_ready_sftp_service

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

_OWNER = ClientId("client:owner")


@pytest.fixture
def service():
    process = subprocess.Popen([_SFTP_SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    client = OpenSSHSFTPClient(process.stdin, process.stdout, on_close=process.terminate)
    client.start()
    runtime, service_id, _ = _make_ready_sftp_service(_OWNER, client)
    try:
        yield runtime, service_id, client
    finally:
        client.close()
        process.wait(timeout=5)
        process.stdout.close()


def test_filesystem_usage_matches_the_disk(service, tmp_path):
    runtime, service_id, _client = service

    usage = runtime.filesystem_usage(
        SftpPathRequest(service_id=service_id, path=str(tmp_path)), client_id=_OWNER
    )

    local = os.statvfs(tmp_path)
    assert usage.path == str(tmp_path)
    assert usage.total_bytes == local.f_blocks * local.f_frsize
    # Free space moves as other processes write; allow 64 MiB of drift.
    assert abs(usage.available_bytes - local.f_bavail * local.f_frsize) < 64 * 1024 * 1024
    assert usage.available_bytes <= usage.free_bytes <= usage.total_bytes


def test_filesystem_usage_of_a_missing_path_is_an_error(service, tmp_path):
    runtime, service_id, _client = service

    with pytest.raises(SshPilotError) as excinfo:
        runtime.filesystem_usage(
            SftpPathRequest(service_id=service_id, path=str(tmp_path / "missing")),
            client_id=_OWNER,
        )
    assert excinfo.value.code is ErrorCode.REMOTE_PATH_NOT_FOUND


def test_filesystem_usage_without_the_extension_is_unsupported(service, tmp_path):
    runtime, service_id, client = service
    client.extensions.pop("statvfs@openssh.com")

    with pytest.raises(SshPilotError) as excinfo:
        runtime.filesystem_usage(
            SftpPathRequest(service_id=service_id, path=str(tmp_path)), client_id=_OWNER
        )
    assert excinfo.value.code is ErrorCode.REMOTE_UNSUPPORTED_OPERATION
