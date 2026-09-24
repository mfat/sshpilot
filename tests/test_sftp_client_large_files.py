"""Files larger than one SFTP packet round-trip through ``OpenSSHSFTPFile``.

OpenSSH's sftp-server answers a READ with at most ~255 KiB and drops the
session on a message over 256 KiB, so a single-request read silently truncated
the remote editor's content and a single-request write killed the service.
Runs against the real sftp-server binary.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from sshpilot.sftp.client import OpenSSHSFTPClient

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

_SIZE = 900_000  # well past sftp-server's 256 KiB packet limit


@pytest.fixture
def client():
    process = subprocess.Popen(
        [_SFTP_SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    sftp = OpenSSHSFTPClient(process.stdin, process.stdout, on_close=process.terminate)
    sftp.start()
    try:
        yield sftp
    finally:
        sftp.close()
        process.wait(timeout=5)
        process.stdout.close()


def test_sized_read_returns_the_whole_file(client, tmp_path):
    content = os.urandom(_SIZE)
    path = tmp_path / "big.bin"
    path.write_bytes(content)

    with client.file(str(path), "rb") as handle:
        assert handle.read(1024 * 1024 + 1) == content


def test_sized_read_stops_at_the_requested_size(client, tmp_path):
    content = os.urandom(_SIZE)
    path = tmp_path / "big.bin"
    path.write_bytes(content)

    with client.file(str(path), "rb") as handle:
        assert handle.read(300_000) == content[:300_000]
        assert handle.read(10) == content[300_000:300_010]


def test_write_larger_than_one_packet_lands_intact(client, tmp_path):
    content = os.urandom(_SIZE)
    path = tmp_path / "out.bin"

    with client.file(str(path), "wb", create_mode=0o600) as handle:
        handle.write(content)

    assert path.read_bytes() == content
