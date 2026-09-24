"""Remote files whose names are not valid UTF-8 stay usable.

The SFTP layer used to decode names with ``errors="replace"``, so ``caf\\xe9``
listed as ``caf\\ufffd`` and every action on it failed with "No such file".
Names now decode with ``surrogateescape`` and travel as lone surrogates, the
same convention ``os.fsdecode`` uses for local paths. Drives the runtimes with
the real client against OpenSSH's sftp-server binary.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import ListDirectoryRequest, SftpPathRequest
from sshpilot.api.models.transfers import (
    StartTransferRequest,
    TransferDirection,
    TransferState,
)
from sshpilot.api.transport.codec import (
    list_directory_result_from_wire,
    list_directory_result_to_wire,
)
from sshpilot.api.transport.framing import _decode_payload, encode_frame
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

_OWNER = ClientId("client:owner")
_RAW_NAME = b"caf\xe9.txt"  # Latin-1 "café.txt": not valid UTF-8
_NAME = os.fsdecode(_RAW_NAME)  # "caf\udce9.txt"


@pytest.fixture
def service():
    process = subprocess.Popen([_SFTP_SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    client = OpenSSHSFTPClient(process.stdin, process.stdout, on_close=process.terminate)
    client.start()
    runtime, service_id, _ = _make_ready_sftp_service(_OWNER, client)
    try:
        yield runtime, service_id
    finally:
        client.close()
        process.wait(timeout=5)
        process.stdout.close()


def _run(transfers, service_id, direction, local_path, remote_path):
    prepared = transfers.prepare_start_transfer(
        StartTransferRequest(
            connection_id=ConnectionId("demo"),
            sftp_service_id=service_id,
            direction=direction,
            remote_path=remote_path,
            local_path=local_path,
        ),
        client_id=_OWNER,
    )
    transfers.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfers, prepared.id, timeout=10.0)
    assert summary.state is TransferState.COMPLETED, summary


def test_listed_name_survives_the_wire_and_names_the_real_file(service, tmp_path):
    runtime, service_id = service
    remote_dir = os.fsencode(tmp_path / "remote")
    os.mkdir(remote_dir)
    with open(os.path.join(remote_dir, _RAW_NAME), "wb") as handle:
        handle.write(b"bonjour")

    listing = runtime.list_directory(
        ListDirectoryRequest(
            connection_id=ConnectionId("demo"),
            service_id=service_id,
            path=os.fsdecode(remote_dir),
        ),
        client_id=_OWNER,
    )
    # Through the daemon frame and back, as the GUI receives it.
    frame = encode_frame({"result": list_directory_result_to_wire(listing)})
    received = list_directory_result_from_wire(_decode_payload(frame[4:])["result"])
    (entry,) = received.entries
    assert entry.name == _NAME

    stat = runtime.stat_path(
        SftpPathRequest(service_id=service_id, path=entry.path), client_id=_OWNER
    )
    assert stat.size == len(b"bonjour")


def test_download_and_upload_keep_the_raw_name(service, tmp_path):
    runtime, service_id = service
    transfers = TransferRuntime(runtime)
    remote_dir = tmp_path / "remote"
    local_dir = tmp_path / "local"
    remote_dir.mkdir()
    local_dir.mkdir()
    with open(os.path.join(os.fsencode(remote_dir), _RAW_NAME), "wb") as handle:
        handle.write(b"bonjour")

    _run(
        transfers,
        service_id,
        TransferDirection.DOWNLOAD,
        str(local_dir / _NAME),
        str(remote_dir / _NAME),
    )
    assert os.listdir(os.fsencode(local_dir)) == [_RAW_NAME]

    uploaded = remote_dir / f"copy-{_NAME}"
    _run(transfers, service_id, TransferDirection.UPLOAD, str(local_dir / _NAME), str(uploaded))
    with open(os.fsencode(uploaded), "rb") as handle:
        assert handle.read() == b"bonjour"
    assert b"copy-" + _RAW_NAME in os.listdir(os.fsencode(remote_dir))
