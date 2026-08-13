"""Transfer policy model tests."""
from __future__ import annotations


import pytest

from sshpilot.core.errors import CoreError
from sshpilot.core.transfers import (
    ConflictDecision,
    OverwritePolicy,
    PathRef,
    Progress,
    TransferDirection,
    TransferErrorKind,
    TransferQueuePolicy,
    TransferRequest,
    TransferState,
    TransferSummary,
    atomic_temp_name,
    decide_conflict,
    transition,
    ui_conflict_response_to_policy,
)


def test_upload_download_validation(tmp_path):
    local = tmp_path / "file.txt"
    local.write_text("hi")
    upload = TransferRequest(
        direction=TransferDirection.UPLOAD,
        source=PathRef(str(local), is_remote=False),
        destination=PathRef("/remote/file.txt", is_remote=True),
        overwrite=OverwritePolicy.OVERWRITE,
    )
    upload.validate()
    download = TransferRequest(
        direction=TransferDirection.DOWNLOAD,
        source=PathRef("/remote/file.txt", is_remote=True),
        destination=PathRef(str(tmp_path / "out.txt"), is_remote=False),
    )
    download.validate()


def test_invalid_paths_and_recursive():
    with pytest.raises(CoreError):
        TransferRequest(
            direction=TransferDirection.UPLOAD,
            source=PathRef("/no/such", is_remote=False),
            destination=PathRef("/r", is_remote=True),
        ).validate()
    with pytest.raises(CoreError):
        TransferRequest(
            direction=TransferDirection.DOWNLOAD,
            source=PathRef("/r\x00", is_remote=True),
            destination=PathRef("/tmp/x", is_remote=False),
        ).validate()
    with pytest.raises(CoreError):
        TransferRequest(
            direction=TransferDirection.UPLOAD,
            source=PathRef("/tmp/x", is_remote=False),
            destination=PathRef("/r", is_remote=True),
            recursive=True,
        ).validate()


def test_atomic_cancel_progress_queue():
    name = atomic_temp_name("/tmp/dest.bin")
    assert "sshpilot-tmp" in name
    summary = TransferSummary(
        id="t1",
        direction=TransferDirection.UPLOAD,
        state=TransferState.QUEUED,
        source="a",
        destination="b",
    )
    summary = transition(summary, TransferState.STARTING)
    summary = transition(summary, TransferState.RUNNING)
    summary = transition(summary, TransferState.CANCELLING)
    summary = transition(summary, TransferState.CANCELLED)
    progress = Progress(0, 100).advance(10).advance(5)
    assert progress.bytes_completed == 10
    assert TransferQueuePolicy(max_queued=1).admit(1, 0) == TransferErrorKind.QUEUE_FULL
    done = TransferSummary(
        id="t2",
        direction=TransferDirection.DOWNLOAD,
        state=TransferState.RUNNING,
        source="a",
        destination="b",
        progress=Progress(100, 100),
    )
    done = transition(done, TransferState.COMPLETED)
    assert done.state == TransferState.COMPLETED
    failed = TransferSummary(
        id="t3",
        direction=TransferDirection.UPLOAD,
        state=TransferState.RUNNING,
        source="a",
        destination="b",
        error_kind=TransferErrorKind.IO,
        message="boom",
    )
    failed = transition(failed, TransferState.FAILED)
    assert failed.error_kind == TransferErrorKind.IO


def test_decide_conflict_and_ui_mapping():
    assert decide_conflict(False, OverwritePolicy.FAIL) is ConflictDecision.PROCEED
    assert decide_conflict(True, OverwritePolicy.OVERWRITE) is ConflictDecision.PROCEED
    assert decide_conflict(True, OverwritePolicy.SKIP) is ConflictDecision.SKIP
    assert decide_conflict(True, OverwritePolicy.RENAME) is ConflictDecision.RENAME
    assert decide_conflict(True, OverwritePolicy.FAIL) is ConflictDecision.FAIL
    assert ui_conflict_response_to_policy("replace") is OverwritePolicy.OVERWRITE
    assert ui_conflict_response_to_policy("skip") is OverwritePolicy.SKIP
    assert ui_conflict_response_to_policy("cancel") is OverwritePolicy.FAIL
