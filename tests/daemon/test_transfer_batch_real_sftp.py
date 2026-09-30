"""A file-manager selection runs as one daemon transfer.

One byte total, one terminal state, per-item failures that do not stop the
rest, and one ``transfer.item_completed`` per item (or pipelined window).
Drives the transfer runtime with the real client against OpenSSH's
sftp-server binary.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from sshpilot.api.events import EventType
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import SftpFailureCode
from sshpilot.api.models.transfers import (
    StartTransferBatchRequest,
    TransferConflictPolicy,
    TransferDirection,
    TransferItem,
    TransferState,
)
from sshpilot.daemon.transfer_runtime import TransferRuntime
from sshpilot.sftp.client import OpenSSHSFTPClient
from tests.daemon.test_transfer_metadata_real_sftp import _SFTP_SERVER
from tests.daemon.test_transfer_runtime import (
    _make_ready_sftp_service,
    _wait_for_terminal_state,
)

pytestmark = pytest.mark.skipif(_SFTP_SERVER is None, reason="OpenSSH sftp-server not installed")

_OWNER = ClientId("client:owner")


@pytest.fixture
def batch():
    process = subprocess.Popen([_SFTP_SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    client = OpenSSHSFTPClient(process.stdin, process.stdout, on_close=process.terminate)
    client.start()
    sftp_runtime, service_id, _ = _make_ready_sftp_service(_OWNER, client)
    runtime = TransferRuntime(sftp_runtime, progress_min_interval_seconds=0.0)
    events = []
    runtime.subscribe_events(events.append)

    def run(direction, items, *, policy=TransferConflictPolicy.OVERWRITE, cancel_at=None):
        prepared = runtime.prepare_start_transfer_batch(
            StartTransferBatchRequest(
                connection_id=ConnectionId("demo"),
                sftp_service_id=service_id,
                direction=direction,
                items=tuple(items),
                conflict_policy=policy,
            ),
            client_id=_OWNER,
        )
        runtime.run_transfer(prepared.id)
        summary = _wait_for_terminal_state(runtime, prepared.id, timeout=20.0)
        mine = [e for e in events if e.payload.id == prepared.id]
        return summary, mine

    run.runtime = runtime
    run.service_id = service_id
    run.process = process
    try:
        yield run
    finally:
        client.close()
        process.wait(timeout=5)
        process.stdout.close()


def _tree(root, spec):
    for rel, size in spec.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(size))
    return root


_SPEC = {
    "a.txt": 10,
    "b.txt": 3000,
    "c.bin": 600_000,
    "empty": 0,
    "dir/x": 50,
    "dir/sub/y": 70_000,
}


def _items(src, dst, names, *, download=False):
    items = []
    for name in names:
        local, remote = (dst / name, src / name) if download else (src / name, dst / name)
        items.append(
            TransferItem(
                local_path=str(local),
                remote_path=str(remote),
                recursive=(src / name).is_dir(),
            )
        )
    return items


@pytest.mark.parametrize("direction", [TransferDirection.UPLOAD, TransferDirection.DOWNLOAD])
def test_mixed_batch_is_one_transfer(batch, tmp_path, direction):
    src = _tree(tmp_path / "src", _SPEC)
    dst = tmp_path / "dst"
    dst.mkdir()
    names = ["a.txt", "b.txt", "c.bin", "empty", "dir"]
    download = direction is TransferDirection.DOWNLOAD
    summary, events = batch(direction, _items(src, dst, names, download=download))

    assert summary.state is TransferState.COMPLETED, summary
    assert summary.items_total == 5 and summary.items_done == 5
    assert summary.item_failures == ()
    assert summary.bytes_total == sum(_SPEC.values())
    assert summary.bytes_completed == summary.bytes_total
    for rel in _SPEC:
        assert (dst / rel).read_bytes() == (src / rel).read_bytes(), rel
    terminal = [e for e in events if e.type is EventType.TRANSFER_COMPLETED]
    assert len(terminal) == 1
    items_seen = [
        e.payload.items_done for e in events if e.type is EventType.TRANSFER_ITEM_COMPLETED
    ]
    assert items_seen and items_seen[-1] == 5
    assert items_seen == sorted(items_seen)


@pytest.mark.parametrize("direction", [TransferDirection.UPLOAD, TransferDirection.DOWNLOAD])
def test_missing_item_fails_alone_and_the_rest_land(batch, tmp_path, direction):
    src = _tree(tmp_path / "src", {"one": 5, "three": 5, "big": 500_000})
    dst = tmp_path / "dst"
    dst.mkdir()
    download = direction is TransferDirection.DOWNLOAD
    items = _items(src, dst, ["one", "two-missing", "three", "big"], download=download)
    summary, events = batch(direction, items)

    assert summary.state is TransferState.FAILED
    assert summary.items_done == 4
    assert [f.index for f in summary.item_failures] == [1]
    assert summary.failure == summary.item_failures[0].failure
    assert summary.item_failures[0].failure.code in {
        SftpFailureCode.PATH_NOT_FOUND,
        SftpFailureCode.LOCAL_SOURCE_FILE_NOT_FOUND,
    }
    for name in ("one", "three", "big"):
        assert (dst / name).read_bytes() == (src / name).read_bytes()
    assert summary.bytes_completed == summary.bytes_total
    assert [e.type for e in events].count(EventType.TRANSFER_FAILED) == 1


def test_unwritable_destination_in_a_pipelined_window_is_attributed(batch, tmp_path):
    src = _tree(tmp_path / "src", {"f1": 5, "f2": 5, "f3": 5})
    dst = tmp_path / "dst"
    dst.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0o500)
    try:
        items = _items(src, dst, ["f1", "f2", "f3"])
        items[1] = TransferItem(local_path=str(src / "f2"), remote_path=str(blocked / "f2"))
        summary, _events = batch(TransferDirection.UPLOAD, items)
    finally:
        os.chmod(blocked, 0o700)

    assert summary.state is TransferState.FAILED
    assert [f.index for f in summary.item_failures] == [1]
    assert (dst / "f1").exists() and (dst / "f3").exists()
    assert not (blocked / "f2").exists()


def test_cancel_mid_batch_reports_how_far_it_got(batch, tmp_path):
    src = _tree(tmp_path / "src", {f"f{i}": 2_000_000 for i in range(6)})
    dst = tmp_path / "dst"
    dst.mkdir()
    runtime = batch.runtime
    from sshpilot.api.models.transfers import CancelTransferRequest

    cancelled = []

    def _cancel_after_first(event):
        if event.type is EventType.TRANSFER_ITEM_COMPLETED and not cancelled:
            cancelled.append(True)
            runtime.prepare_cancel_transfer(
                CancelTransferRequest(transfer_id=event.payload.id), client_id=_OWNER
            )

    runtime.subscribe_events(_cancel_after_first)
    summary, _events = batch(
        TransferDirection.UPLOAD, _items(src, dst, [f"f{i}" for i in range(6)])
    )

    assert summary.state is TransferState.CANCELLED
    assert 1 <= summary.items_done < 6
    for i in range(summary.items_done):
        assert (dst / f"f{i}").read_bytes() == (src / f"f{i}").read_bytes()
    leftovers = [p.name for p in dst.iterdir() if p.name.startswith(".sshpilot-tmp-")]
    assert leftovers == []


def test_batch_takes_one_queue_slot(batch, tmp_path):
    src = _tree(tmp_path / "src", {f"f{i:02d}": 1 for i in range(40)})
    dst = tmp_path / "dst"
    dst.mkdir()
    summary, _events = batch(
        TransferDirection.UPLOAD, _items(src, dst, sorted(p.name for p in src.iterdir()))
    )
    assert summary.state is TransferState.COMPLETED
    assert len(list(dst.iterdir())) == 40


def test_losing_the_server_mid_batch_stops_short_with_what_landed(batch, tmp_path):
    src = _tree(tmp_path / "src", {f"f{i}": 2_000_000 for i in range(5)})
    dst = tmp_path / "dst"
    dst.mkdir()
    killed = []

    def _kill_after_first(event):
        if event.type is EventType.TRANSFER_ITEM_COMPLETED and not killed:
            killed.append(True)
            batch.process.kill()

    batch.runtime.subscribe_events(_kill_after_first)
    summary, _events = batch(
        TransferDirection.UPLOAD, _items(src, dst, [f"f{i}" for i in range(5)])
    )

    assert summary.state is TransferState.FAILED
    # Stopped short: the frontend reads that as a batch-level failure.
    assert 1 <= summary.items_done < summary.items_total
    assert summary.failure is not None
    assert summary.item_failures == ()
    assert (dst / "f0").read_bytes() == (src / "f0").read_bytes()
