"""Phase 10.1 lifecycle races and shutdown cleanup (real OpenSSH + ephemeral daemon)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sshpilot.api.models.operations import (
    CloseForwardRequest,
    CloseSftpRequest,
    ForwardState,
    ForwardType,
    OpenForwardRequest,
    OpenSftpRequest,
    SftpServiceState,
)
from sshpilot.api.models.transfers import (
    CancelTransferRequest,
    StartTransferRequest,
    TransferConflictPolicy,
    TransferDirection,
    TransferState,
)
from tests.daemon.phase10_helpers import (
    free_port,
    require_phase10_container,
    start_phase10_stack,
    wait_until,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def phase10_env(tmp_path_factory):
    env = require_phase10_container(tmp_path_factory.mktemp("phase10-race-sshd"))
    try:
        yield env
    finally:
        env.destroy()


def _ssh_children(pids: set[int]) -> set[int]:
    alive = set()
    for pid in pids:
        if Path(f"/proc/{pid}").exists():
            alive.add(pid)
    return alive


@pytest.mark.parametrize("attempt", range(5))
def test_sftp_open_close_during_startup_race(tmp_path, phase10_env, attempt):
    work = tmp_path / f"run-{attempt}"
    work.mkdir()
    stack = start_phase10_stack(work, env=phase10_env)
    try:
        opened = stack.client.open_sftp(
            OpenSftpRequest(connection_id=stack.connection_id)
        )
        assert opened.state is SftpServiceState.STARTING
        stack.client.close_sftp(CloseSftpRequest(service_id=opened.id))
        wait_until(
            lambda: stack.client.get_sftp_service(opened.id).state
            in {SftpServiceState.CLOSED, SftpServiceState.FAILED},
            timeout=30,
            message="SFTP did not close during startup race",
        )
    finally:
        stack.close(destroy_env=False)


@pytest.mark.parametrize("attempt", range(5))
def test_transfer_complete_cancel_race(tmp_path, phase10_env, attempt):
    work = tmp_path / f"run-{attempt}"
    work.mkdir()
    stack = start_phase10_stack(work, env=phase10_env)
    try:
        opened = stack.client.open_sftp(
            OpenSftpRequest(connection_id=stack.connection_id)
        )
        ready = stack.wait_sftp_ready(opened.id)
        payload = work / "race.bin"
        payload.write_bytes(os.urandom(256 * 1024))
        started = stack.client.start_transfer(
            StartTransferRequest(
                connection_id=stack.connection_id,
                sftp_service_id=ready.id,
                direction=TransferDirection.UPLOAD,
                remote_path=f"race-{attempt}.bin",
                local_path=str(payload),
                conflict_policy=TransferConflictPolicy.OVERWRITE,
            )
        )
        # Cancel may win or lose to completion; either terminal is fine.
        try:
            stack.client.cancel_transfer(
                CancelTransferRequest(transfer_id=started.id)
            )
        except Exception:
            pass
        final = stack.wait_transfer_terminal(started.id, timeout=60)
        assert final.state in {
            TransferState.COMPLETED,
            TransferState.CANCELLED,
            TransferState.FAILED,
        }
    finally:
        stack.close(destroy_env=False)


@pytest.mark.parametrize("attempt", range(5))
def test_forward_active_close_race(tmp_path, phase10_env, attempt):
    work = tmp_path / f"run-{attempt}"
    work.mkdir()
    stack = start_phase10_stack(work, env=phase10_env)
    try:
        bind_port = free_port()
        opened = stack.client.open_forward(
            OpenForwardRequest(
                connection_id=stack.connection_id,
                type=ForwardType.LOCAL,
                bind_host="127.0.0.1",
                bind_port=bind_port,
                destination_host="127.0.0.1",
                destination_port=stack.env.remote_echo_port,
            )
        )
        active = stack.wait_forward_active(opened.id)
        assert active.state is ForwardState.ACTIVE
        handle = stack.server._forward_runtime._records[opened.id].handle
        pid = handle._process.pid
        stack.client.close_forward(CloseForwardRequest(forward_id=opened.id))
        wait_until(
            lambda: stack.client.get_forward(opened.id).state is ForwardState.CLOSED,
            timeout=20,
            message="forward close race did not reach CLOSED",
        )
        wait_until(
            lambda: pid not in _ssh_children({pid}),
            timeout=15,
            message="ssh -N leaked after forward close race",
        )
    finally:
        stack.close(destroy_env=False)


def test_daemon_shutdown_cleans_sftp_transfer_and_forward(tmp_path, phase10_env):
    stack = start_phase10_stack(tmp_path, env=phase10_env)
    pids: set[int] = set()
    try:
        sftp = stack.wait_sftp_ready(
            stack.client.open_sftp(
                OpenSftpRequest(connection_id=stack.connection_id)
            ).id
        )
        sftp_pid = stack.server._sftp_runtime._records[sftp.id].handle._process.pid
        pids.add(sftp_pid)

        payload = tmp_path / "shutdown.bin"
        payload.write_bytes(os.urandom(1024 * 1024))
        transfer = stack.client.start_transfer(
            StartTransferRequest(
                connection_id=stack.connection_id,
                sftp_service_id=sftp.id,
                direction=TransferDirection.UPLOAD,
                remote_path="shutdown.bin",
                local_path=str(payload),
                conflict_policy=TransferConflictPolicy.OVERWRITE,
            )
        )
        wait_until(
            lambda: stack.client.get_transfer(transfer.id).state
            in {
                TransferState.RUNNING,
                TransferState.COMPLETED,
                TransferState.FAILED,
                TransferState.CANCELLED,
            },
            timeout=30,
            message="transfer never left queued",
        )

        bind_port = free_port()
        forward = stack.wait_forward_active(
            stack.client.open_forward(
                OpenForwardRequest(
                    connection_id=stack.connection_id,
                    type=ForwardType.DYNAMIC,
                    bind_host="127.0.0.1",
                    bind_port=bind_port,
                )
            ).id
        )
        fwd_pid = stack.server._forward_runtime._records[forward.id].handle._process.pid
        pids.add(fwd_pid)

        stack.server.shutdown()
        stack.server.wait_stopped()
        # Idempotent second shutdown must not raise.
        stack.server.shutdown()
        stack.server.wait_stopped()

        wait_until(
            lambda: not _ssh_children(pids),
            timeout=20,
            message=f"orphan ssh processes after shutdown: {_ssh_children(pids)}",
        )
        assert not Path(stack.server.socket_path).exists()
    finally:
        stack._stop_responder.set()
        for client in list(stack._clients):
            try:
                client.close()
            except Exception:
                pass
