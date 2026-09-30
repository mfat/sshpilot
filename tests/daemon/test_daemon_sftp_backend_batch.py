"""The file-manager backend runs a selection as one daemon batch transfer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.operations import SftpFailure, SftpFailureCode
from sshpilot.api.models.transfers import (
    TransferDirection,
    TransferItemFailure,
    TransferState,
    TransferSummary,
)
from sshpilot.daemon_sftp_backend import TransferBatchResult
from tests.daemon.test_daemon_sftp_backend_file_ops import SERVICE_ID, _manager

_FAILURE = SftpFailure(SftpFailureCode.PATH_NOT_FOUND, ErrorCode.REMOTE_PATH_NOT_FOUND)


class _Transfers:
    def __init__(self):
        self.requests = []
        self.callbacks = {}

    def start_transfer_batch(self, request, **callbacks):
        self.requests.append(request)
        self.callbacks = callbacks


def _summary(state, *, done, failures=(), bytes_done=0):
    return TransferSummary(
        id="transfer-1",
        connection_id="conn-1",
        sftp_service_id=SERVICE_ID,
        direction=TransferDirection.UPLOAD,
        state=state,
        source_display="a",
        destination_display="/remote",
        bytes_total=30,
        bytes_completed=bytes_done,
        created_at=datetime.now(timezone.utc),
        items_total=3,
        items_done=done,
        item_failures=tuple(failures),
        failure=failures[0].failure if failures else None,
    )


@pytest.fixture
def batch():
    manager = _manager()
    manager._transfers = _Transfers()
    manager._operation_seq = 0
    emitted = []
    manager.emit = lambda *args: emitted.append(args)
    items = [("/l/a", "~/a", False), ("/l/b", "/r/b", False), ("/l/t", "/r/t", True)]
    manager._home = "/home/pilot"
    future = manager.transfer_batch(TransferDirection.UPLOAD, items)
    return manager, future, emitted


def test_one_request_carries_every_item(batch):
    manager, _future, _emitted = batch
    (request,) = manager._transfers.requests
    assert [(i.local_path, i.remote_path, i.recursive) for i in request.items] == [
        ("/l/a", "/home/pilot/a", False),
        ("/l/b", "/r/b", False),
        ("/l/t", "/r/t", True),
    ]


def test_progress_emits_bytes_and_item_counts_under_one_key(batch):
    manager, future, emitted = batch
    manager._transfers.callbacks["on_progress"](
        _summary(TransferState.RUNNING, done=1, bytes_done=10)
    )
    key = f"fut-{id(future)}"
    assert ("progress-bytes", 10, 30, key) in emitted
    assert ("progress-items", 1, 3, key) in emitted


def test_partial_failure_resolves_with_which_items_failed(batch):
    manager, future, _emitted = batch
    manager._transfers.callbacks["on_done"](
        _summary(
            TransferState.FAILED,
            done=3,
            failures=[TransferItemFailure(index=1, failure=_FAILURE)],
            bytes_done=30,
        )
    )
    result = future.result(timeout=1)
    assert isinstance(result, TransferBatchResult)
    assert list(result.failures) == [1]
    assert [result.succeeded(i) for i in range(3)] == [True, False, True]


def test_completed_batch_has_no_failures(batch):
    manager, future, _emitted = batch
    manager._transfers.callbacks["on_done"](
        _summary(TransferState.COMPLETED, done=3, bytes_done=30)
    )
    result = future.result(timeout=1)
    assert result.failures == {} and all(result.succeeded(i) for i in range(3))


def test_cancelled_batch_keeps_the_items_that_landed(batch):
    manager, future, _emitted = batch
    manager._transfers.callbacks["on_done"](_summary(TransferState.CANCELLED, done=2))
    result = future.result(timeout=1)
    assert result.cancelled and result.error is None
    assert [result.succeeded(i) for i in range(3)] == [True, True, False]


def test_batch_stopped_short_reports_the_batch_error_not_item_failures(batch):
    manager, future, _emitted = batch
    lost = SftpFailure(SftpFailureCode.CONNECTION_LOST, ErrorCode.SFTP_PROTOCOL_LOST)
    summary = _summary(
        TransferState.FAILED,
        done=2,
        failures=[TransferItemFailure(index=0, failure=_FAILURE)],
    )
    summary = TransferSummary(**{**summary.__dict__, "failure": lost})
    manager._transfers.callbacks["on_done"](summary)
    result = future.result(timeout=1)
    assert result.error and result.error != result.failures[0]
    assert [result.succeeded(i) for i in range(3)] == [False, True, False]


def test_batch_that_ran_to_the_end_has_no_batch_error(batch):
    manager, future, _emitted = batch
    manager._transfers.callbacks["on_done"](
        _summary(
            TransferState.FAILED,
            done=3,
            failures=[TransferItemFailure(index=2, failure=_FAILURE)],
        )
    )
    result = future.result(timeout=1)
    assert result.error is None and list(result.failures) == [2]


def test_move_cleanup_only_removes_sources_of_items_that_landed():
    from concurrent.futures import Future

    from sshpilot.file_manager_window import FileManagerWindow

    succeeded = FileManagerWindow._transfer_item_succeeded
    future = Future()
    future.set_result(TransferBatchResult(items_total=3, items_done=2, failures={0: "gone"}))
    # 0 failed, 1 landed, 2 never ran (cancelled or aborted after item 1).
    assert [succeeded(future, i) for i in range(3)] == [False, True, False]

    failed = Future()
    failed.set_exception(OSError("could not start"))
    assert succeeded(failed, 0) is False

    # Cancelled or cut off after two items: those two sources may go.
    for ended in (
        TransferBatchResult(items_total=3, items_done=2, cancelled=True),
        TransferBatchResult(items_total=3, items_done=2, error="Connection lost"),
    ):
        stopped = Future()
        stopped.set_result(ended)
        assert [succeeded(stopped, i) for i in range(3)] == [True, True, False]
