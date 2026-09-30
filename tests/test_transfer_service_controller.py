"""Transfer events racing the start reply must not be lost.

A small file can finish on the daemon before the ``transfers.start`` reply
reaches the main thread. Dropping that terminal event left the file on disk but
its future unresolved, freezing the file-manager progress bar mid-batch.
"""

from __future__ import annotations

from types import SimpleNamespace

from sshpilot.api.capabilities import Capability
from sshpilot.api.events import EventType
from sshpilot.api.models.common import ConnectionId, SftpServiceId, TransferId
from sshpilot.api.models.transfers import (
    StartTransferRequest,
    TransferConflictPolicy,
    TransferDirection,
    TransferState,
    TransferSummary,
)
from sshpilot.transfer_service_controller import TransferServiceController


class _QueuedBridge:
    """Runs submitted work only when the test drains it, in submit order."""

    def __init__(self) -> None:
        self.pending = []

    def submit(self, operation, *, on_success, on_error):
        self.pending.append((operation, on_success, on_error))

    def drain(self) -> None:
        while self.pending:
            operation, on_success, on_error = self.pending.pop(0)
            try:
                result = operation()
            except Exception as exc:  # noqa: BLE001 - mirror the bridge contract
                on_error(exc)
            else:
                on_success(result)


class _FakeClient:
    def __init__(self, reply_state: TransferState = TransferState.RUNNING) -> None:
        self.listener = None
        self.reply_state = reply_state
        self.calls = []

    def get_capabilities(self):
        return SimpleNamespace(supported=frozenset({
            Capability.TRANSFERS_READ,
            Capability.TRANSFERS_WRITE,
            Capability.TRANSFERS_UPLOAD,
            Capability.TRANSFERS_DOWNLOAD,
            Capability.TRANSFERS_BATCH,
        }))

    def subscribe_events(self, listener):
        self.calls.append("subscribe")
        self.listener = listener
        return SimpleNamespace(unsubscribe=lambda: None)

    def start_transfer(self, _request):
        self.calls.append("start")
        return _summary(self.reply_state)

    def emit(self, state: TransferState, event_type: EventType) -> None:
        self.listener(SimpleNamespace(type=event_type, payload=_summary(state)))


def _summary(state: TransferState, done: int = 0) -> TransferSummary:
    return TransferSummary(
        id=TransferId("transfer-1"),
        connection_id=ConnectionId("conn"),
        sftp_service_id=SftpServiceId("sftp-1"),
        direction=TransferDirection.UPLOAD,
        state=state,
        source_display="/local/a",
        destination_display="/remote/a",
        bytes_total=10,
        bytes_completed=10 if state is TransferState.COMPLETED else done,
    )


def _start(controller, done, progress=None):
    controller.start_transfer(
        StartTransferRequest(
            connection_id=ConnectionId("conn"),
            sftp_service_id=SftpServiceId("sftp-1"),
            direction=TransferDirection.UPLOAD,
            remote_path="/remote/a",
            local_path="/local/a",
            conflict_policy=TransferConflictPolicy.OVERWRITE,
        ),
        on_done=done.append,
        on_progress=(progress.append if progress is not None else None),
    )


def test_subscribes_before_starting_the_transfer():
    client, bridge = _FakeClient(), _QueuedBridge()
    controller = TransferServiceController(client, bridge)
    _start(controller, [])
    bridge.drain()
    assert client.calls == ["subscribe", "start"]


def test_completion_event_before_start_reply_still_completes():
    client, bridge = _FakeClient(), _QueuedBridge()
    controller = TransferServiceController(client, bridge)
    done = []
    _start(controller, done)
    # The daemon finishes the transfer before the start reply is handled.
    client.emit(TransferState.COMPLETED, EventType.TRANSFER_COMPLETED)
    operation, on_success, _on_error = bridge.pending.pop(0)
    event_op, event_success, _ = bridge.pending.pop(0)
    event_success(event_op())
    on_success(operation())

    assert [s.state for s in done] == [TransferState.COMPLETED]


def test_late_progress_does_not_mask_an_early_completion():
    client, bridge = _FakeClient(), _QueuedBridge()
    controller = TransferServiceController(client, bridge)
    done, progress = [], []
    _start(controller, done, progress)
    start = bridge.pending.pop(0)
    client.emit(TransferState.COMPLETED, EventType.TRANSFER_COMPLETED)
    client.emit(TransferState.RUNNING, EventType.TRANSFER_PROGRESS)
    bridge.drain()
    operation, on_success, _ = start
    on_success(operation())

    assert [s.state for s in done] == [TransferState.COMPLETED]
    assert progress == []


def test_progress_before_start_reply_is_replayed():
    client, bridge = _FakeClient(), _QueuedBridge()
    controller = TransferServiceController(client, bridge)
    done, progress = [], []
    _start(controller, done, progress)
    start = bridge.pending.pop(0)
    client.emit(TransferState.RUNNING, EventType.TRANSFER_PROGRESS)
    bridge.drain()
    operation, on_success, _ = start
    on_success(operation())
    client.emit(TransferState.COMPLETED, EventType.TRANSFER_COMPLETED)
    bridge.drain()

    assert [s.state for s in progress] == [TransferState.RUNNING]
    assert [s.state for s in done] == [TransferState.COMPLETED]


def _batch_request():
    from sshpilot.api.models.transfers import StartTransferBatchRequest, TransferItem

    return StartTransferBatchRequest(
        connection_id=ConnectionId("conn"),
        sftp_service_id=SftpServiceId("sftp-1"),
        direction=TransferDirection.UPLOAD,
        items=(TransferItem("/local/a", "/remote/a"), TransferItem("/local/b", "/remote/b")),
    )


def test_batch_start_shares_the_early_completion_path():
    client, bridge = _FakeClient(), _QueuedBridge()
    client.start_transfer_batch = lambda _request: (
        client.calls.append("start_batch") or _summary(TransferState.RUNNING)
    )
    controller = TransferServiceController(client, bridge)
    done = []
    controller.start_transfer_batch(_batch_request(), on_done=done.append)
    start = bridge.pending.pop(0)
    client.emit(TransferState.COMPLETED, EventType.TRANSFER_COMPLETED)
    bridge.drain()
    operation, on_success, _ = start
    on_success(operation())

    assert client.calls == ["subscribe", "start_batch"]
    assert [s.state for s in done] == [TransferState.COMPLETED]


def test_batch_start_requires_the_batch_capability():
    client, bridge = _FakeClient(), _QueuedBridge()
    client.get_capabilities = lambda: SimpleNamespace(supported=frozenset({
        Capability.TRANSFERS_READ,
        Capability.TRANSFERS_WRITE,
        Capability.TRANSFERS_UPLOAD,
    }))
    controller = TransferServiceController(client, bridge)
    errors = []
    controller.start_transfer_batch(_batch_request(), on_error=errors.append)

    assert bridge.pending == []
    assert errors and errors[0].details == {"capability": "transfers.batch"}
