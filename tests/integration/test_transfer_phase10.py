"""Phase 10.1 real-OpenSSH transfer integration via ephemeral DaemonServer."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.operations import (
    CloseSftpRequest,
    ListDirectoryRequest,
    OpenSftpRequest,
)
from sshpilot.api.models.transfers import (
    CancelTransferRequest,
    StartTransferRequest,
    TransferConflictPolicy,
    TransferDirection,
    TransferState,
)
from sshpilot.daemon.transfer_runtime import _TEMP_PREFIX
from tests.daemon.phase10_helpers import (
    require_phase10_container,
    start_phase10_stack,
    wait_until,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def phase10_env(tmp_path_factory):
    env = require_phase10_container(tmp_path_factory.mktemp("phase10-xfer-sshd"))
    try:
        yield env
    finally:
        env.destroy()


@pytest.fixture
def stack(tmp_path, phase10_env):
    started = start_phase10_stack(tmp_path, env=phase10_env)
    try:
        yield started
    finally:
        started.close(destroy_env=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ready_sftp(stack):
    opened = stack.client.open_sftp(OpenSftpRequest(connection_id=stack.connection_id))
    return stack.wait_sftp_ready(opened.id)


def _start_upload(stack, service_id, local_path, remote_path, *, conflict=None):
    request = StartTransferRequest(
        connection_id=stack.connection_id,
        sftp_service_id=service_id,
        direction=TransferDirection.UPLOAD,
        remote_path=remote_path,
        local_path=str(local_path),
        conflict_policy=conflict or TransferConflictPolicy.FAIL,
    )
    return stack.client.start_transfer(request)


def _start_download(stack, service_id, remote_path, local_path, *, conflict=None):
    request = StartTransferRequest(
        connection_id=stack.connection_id,
        sftp_service_id=service_id,
        direction=TransferDirection.DOWNLOAD,
        remote_path=remote_path,
        local_path=str(local_path),
        conflict_policy=conflict or TransferConflictPolicy.FAIL,
    )
    return stack.client.start_transfer(request)


def test_transfer_roundtrips_zero_small_binary_and_multimb(stack, tmp_path):
    ready = _ready_sftp(stack)
    sid = ready.id
    cases = {
        "zero.bin": b"",
        "small.txt": b"phase10 hello\n",
        "binary.bin": bytes(range(256)) * 16,
        "multi.bin": os.urandom(2 * 1024 * 1024),
    }
    for name, payload in cases.items():
        local_up = tmp_path / f"up-{name}"
        local_up.write_bytes(payload)
        remote = f"xfer-{name}"
        started = _start_upload(stack, sid, local_up, remote)
        done = stack.wait_transfer_terminal(started.id)
        assert done.state is TransferState.COMPLETED, done

        local_down = tmp_path / f"down-{name}"
        started = _start_download(stack, sid, remote, local_down)
        done = stack.wait_transfer_terminal(started.id)
        assert done.state is TransferState.COMPLETED, done
        assert local_down.read_bytes() == payload
        assert _sha256(local_down) == _sha256(local_up)


def test_transfer_overwrite_reject_and_accept(stack, tmp_path):
    ready = _ready_sftp(stack)
    sid = ready.id
    first = tmp_path / "first.txt"
    first.write_text("one")
    remote = "overwrite.txt"
    done = stack.wait_transfer_terminal(_start_upload(stack, sid, first, remote).id)
    assert done.state is TransferState.COMPLETED

    second = tmp_path / "second.txt"
    second.write_text("two")
    rejected = stack.wait_transfer_terminal(_start_upload(stack, sid, second, remote).id)
    assert rejected.state is TransferState.FAILED

    accepted = stack.wait_transfer_terminal(
        _start_upload(
            stack,
            sid,
            second,
            remote,
            conflict=TransferConflictPolicy.OVERWRITE,
        ).id
    )
    assert accepted.state is TransferState.COMPLETED

    downloaded = tmp_path / "got.txt"
    stack.wait_transfer_terminal(_start_download(stack, sid, remote, downloaded).id)
    assert downloaded.read_text() == "two"


def test_cancel_mid_upload_cleans_remote_temp(stack, tmp_path):
    ready = _ready_sftp(stack)
    sid = ready.id
    payload = tmp_path / "big-up.bin"
    payload.write_bytes(os.urandom(4 * 1024 * 1024))
    remote = "cancel-upload.bin"
    started = _start_upload(stack, sid, payload, remote)

    def _running() -> bool:
        return stack.client.get_transfer(started.id).state is TransferState.RUNNING

    wait_until(_running, timeout=30, message="upload never started running")
    stack.client.cancel_transfer(CancelTransferRequest(transfer_id=started.id))
    final = stack.wait_transfer_terminal(started.id, timeout=60)
    assert final.state is TransferState.CANCELLED
    listing = stack.client.sftp_list_directory(
        ListDirectoryRequest(
            connection_id=stack.connection_id,
            service_id=sid,
            path=".",
        )
    )
    names = {entry.name for entry in listing.entries}
    assert remote not in names
    assert not any(name.startswith(_TEMP_PREFIX) for name in names)


def test_cancel_mid_download_cleans_local_temp(stack, tmp_path):
    ready = _ready_sftp(stack)
    sid = ready.id
    remote = "cancel-download.bin"
    source = tmp_path / "src.bin"
    source.write_bytes(os.urandom(3 * 1024 * 1024))
    stack.wait_transfer_terminal(_start_upload(stack, sid, source, remote).id)

    dest = tmp_path / "dest.bin"
    started = _start_download(stack, sid, remote, dest)

    def _running() -> bool:
        return stack.client.get_transfer(started.id).state is TransferState.RUNNING

    wait_until(_running, timeout=30, message="download never started running")
    stack.client.cancel_transfer(CancelTransferRequest(transfer_id=started.id))
    final = stack.wait_transfer_terminal(started.id, timeout=60)
    assert final.state is TransferState.CANCELLED
    assert not dest.exists()
    leftovers = list(tmp_path.glob(f"{_TEMP_PREFIX}*"))
    assert leftovers == []


def test_transfer_concurrency_queue_and_server_busy(stack, tmp_path):
    ready = _ready_sftp(stack)
    sid = ready.id
    runtime = stack.server._transfer_runtime
    runtime._max_concurrent_transfers = 1
    runtime._max_queued_transfers = 1

    payload = tmp_path / "q.bin"
    payload.write_bytes(b"x" * 64)
    first = _start_upload(stack, sid, payload, "q1.bin")
    second = _start_upload(stack, sid, payload, "q2.bin")
    with pytest.raises(SshPilotError) as excinfo:
        _start_upload(stack, sid, payload, "q3.bin")
    assert excinfo.value.code is ErrorCode.SERVER_BUSY

    stack.wait_transfer_terminal(first.id)
    stack.wait_transfer_terminal(second.id)


def test_sftp_close_while_transfer_active(stack, tmp_path):
    ready = _ready_sftp(stack)
    sid = ready.id
    payload = tmp_path / "active.bin"
    payload.write_bytes(os.urandom(2 * 1024 * 1024))
    started = _start_upload(stack, sid, payload, "active-close.bin")
    wait_until(
        lambda: stack.client.get_transfer(started.id).state is TransferState.RUNNING,
        timeout=30,
        message="transfer never running",
    )
    stack.client.close_sftp(CloseSftpRequest(service_id=sid))
    final = stack.wait_transfer_terminal(started.id, timeout=60)
    assert final.state in {TransferState.FAILED, TransferState.CANCELLED}
