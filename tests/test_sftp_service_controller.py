"""DaemonSftpServiceController: not-ready errors go through on_error, not raises."""

import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId, SftpServiceId, utc_now
from sshpilot.api.models.operations import (
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
    SftpDirectorySizeResult,
)
from sshpilot.api.transport.codec import sftp_directory_size_result_to_wire
from sshpilot.daemon_sftp_backend import DaemonSftpManager
from sshpilot.file_manager.common import FileEntry
from sshpilot.file_manager.exceptions import TransferCancelledException
from sshpilot.sftp_service_controller import (
    DaemonSftpServiceController,
    SftpControllerState,
    required_daemon_sftp_capabilities,
)


@pytest.fixture
def mock_client():
    client = Mock()
    capabilities = Mock()
    capabilities.supported = required_daemon_sftp_capabilities()
    client.get_capabilities.return_value = capabilities
    return client


@pytest.fixture
def mock_bridge():
    return Mock()


@pytest.fixture
def controller(mock_client, mock_bridge):
    return DaemonSftpServiceController(
        client=mock_client,
        bridge=mock_bridge,
        connection_id=ConnectionId("conn-1"),
    )


def _mark_ready(controller, service_id="svc-1"):
    with controller._lock:
        controller._state = SftpControllerState.READY
        controller._service_id = SftpServiceId(service_id)


def test_list_directory_not_ready_calls_on_error(controller, mock_bridge):
    """Callback APIs must not raise when the service is gone (e.g. mid-shutdown)."""
    errors = []

    controller.list_directory(
        "/tmp",
        on_success=lambda _r: pytest.fail("should not succeed"),
        on_error=errors.append,
    )

    assert len(errors) == 1
    assert isinstance(errors[0], SshPilotError)
    assert errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY
    mock_bridge.submit.assert_not_called()


def test_detach_marks_not_ready_immediately(controller, mock_bridge):
    _mark_ready(controller)
    controller.detach()

    assert controller.state is SftpControllerState.DETACHED
    errors = []
    controller.list_directory(
        "/tmp",
        on_success=lambda _r: pytest.fail("should not succeed"),
        on_error=errors.append,
    )
    assert errors and errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY


def test_directory_size_starts_operation_and_resolves_result(controller, mock_client, mock_bridge):
    _mark_ready(controller)
    result = SftpDirectorySizeResult(
        path="/tree", size_bytes=123, file_count=3, directory_count=1
    )
    started = OperationSummary(
        operation_id=OperationId("operation-size-1"),
        kind=OperationKind.SFTP_DIRECTORY_SIZE,
        state=OperationState.QUEUED,
        message="Measuring /tree",
        created_at=utc_now(),
        owner_client_id=ClientId("client-1"),
    )
    succeeded = OperationSummary(
        operation_id=started.operation_id,
        kind=OperationKind.SFTP_DIRECTORY_SIZE,
        state=OperationState.SUCCEEDED,
        message="Measured 3 files, 1 directories",
        created_at=started.created_at,
        owner_client_id=ClientId("client-1"),
        result=sftp_directory_size_result_to_wire(result),
    )
    mock_client.sftp_directory_size.return_value = started
    mock_client.get_operation.return_value = succeeded
    mock_bridge.submit.side_effect = (
        lambda factory, on_success=None, on_error=None: on_success(factory())
    )

    seen = []
    controller.directory_size(
        "/tree",
        on_success=seen.append,
        on_error=lambda e: pytest.fail(f"unexpected error: {e}"),
    )
    request = mock_client.sftp_directory_size.call_args[0][0]
    assert request.service_id == SftpServiceId("svc-1")
    assert request.path == "/tree"
    controller._poll_operations()
    assert seen == [result]


def test_directory_size_operation_progress_is_polled_and_delivered(
    controller, mock_client, mock_bridge
):
    """Non-terminal polls must reach ``on_progress`` (not only the terminal
    ``on_terminal`` callback), so the file-manager progress dialog can show
    live daemon progress instead of just resolving at the end."""
    _mark_ready(controller)
    started = OperationSummary(
        operation_id=OperationId("operation-progress-1"),
        kind=OperationKind.SFTP_DIRECTORY_SIZE,
        state=OperationState.RUNNING,
        message="Measuring /tree",
        created_at=utc_now(),
        owner_client_id=ClientId("client-1"),
    )
    running_progress = OperationSummary(
        operation_id=started.operation_id,
        kind=OperationKind.SFTP_DIRECTORY_SIZE,
        state=OperationState.RUNNING,
        message="Measuring /tree",
        created_at=started.created_at,
        owner_client_id=ClientId("client-1"),
        progress=0.4,
    )
    mock_client.sftp_directory_size.return_value = started
    mock_bridge.submit.side_effect = (
        lambda factory, on_success=None, on_error=None: on_success(factory())
    )

    progress_events = []
    controller.directory_size(
        "/tree",
        on_success=lambda _s: None,
        on_error=lambda e: pytest.fail(f"unexpected error: {e}"),
        on_progress=progress_events.append,
    )

    mock_client.get_operation.return_value = running_progress
    controller._poll_operations()

    assert progress_events == [running_progress]
    # Still tracked -- not terminal, so it must stay in the watcher table.
    assert started.operation_id in controller._operation_watchers


def test_directory_size_not_ready_calls_on_error(controller, mock_bridge):
    errors = []
    controller.directory_size(
        "/tree",
        on_success=lambda _r: pytest.fail("should not succeed"),
        on_error=errors.append,
    )
    assert len(errors) == 1
    assert isinstance(errors[0], SshPilotError)
    assert errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY
    mock_bridge.submit.assert_not_called()


def test_daemon_manager_directory_size_delegates_to_daemon():
    """DaemonSftpManager.directory_size issues one controller call and resolves
    with the daemon-computed size — the frontend never walks the tree.

    Uses a SimpleNamespace self (same pattern as the other manager tests): the
    stubbed gi environment instantiates ``GObject.GObject`` subclasses as bare
    ``object()``, so the real manager cannot be constructed here.
    """
    controller = Mock()
    controller.state = SftpControllerState.READY
    controller.service_id = SftpServiceId("svc-1")
    manager = types.SimpleNamespace(
        _sftp_controller=controller,
        _home="/home/alice",
        _closed=False,
    )
    manager._expand = DaemonSftpManager._expand.__get__(manager)
    manager._require_ready_service_id = (
        DaemonSftpManager._require_ready_service_id.__get__(manager)
    )
    manager._safe_set = DaemonSftpManager._safe_set
    manager._operation_cancellable = (
        DaemonSftpManager._operation_cancellable.__get__(manager)
    )
    manager.emit = lambda *_args, **_kwargs: None
    result = SftpDirectorySizeResult(
        path="/home/alice/tree", size_bytes=777, file_count=2, directory_count=1
    )
    controller.directory_size.side_effect = (
        lambda path, on_success=None, on_error=None, on_operation_started=None, on_progress=None: on_success(
            result
        )
    )

    total = DaemonSftpManager.directory_size(manager, "/tree").result(timeout=5)
    assert total == 777
    # Only ``~``/``~/`` prefixes are expanded; absolute paths pass through.
    assert controller.directory_size.call_args[0][0] == "/tree"
    assert DaemonSftpManager.directory_size(
        manager, "~/tree"
    ).result(timeout=5) == 777
    assert controller.directory_size.call_args[0][0] == "/home/alice/tree"


def _bound_manager(controller):
    """Same SimpleNamespace-self pattern as the directory-size delegation
    test above, extended with the operation-cancel wiring bound methods."""
    manager = types.SimpleNamespace(
        _sftp_controller=controller,
        _home="/home/alice",
        _closed=False,
    )
    manager._expand = DaemonSftpManager._expand.__get__(manager)
    manager._require_ready_service_id = (
        DaemonSftpManager._require_ready_service_id.__get__(manager)
    )
    manager._safe_set = DaemonSftpManager._safe_set
    manager._operation_cancellable = (
        DaemonSftpManager._operation_cancellable.__get__(manager)
    )
    manager._resolve_operation_exception = DaemonSftpManager._resolve_operation_exception
    manager.emit = Mock()
    return manager


def test_directory_size_future_cancel_calls_operations_cancel():
    """``future.cancel()`` must reach ``operations.cancel``, not just mark the
    local ``Future`` -- otherwise the daemon keeps measuring/copying/deleting
    after the user presses Cancel."""
    controller = Mock()
    controller.state = SftpControllerState.READY
    controller.service_id = SftpServiceId("svc-1")
    manager = _bound_manager(controller)
    captured = {}

    def _directory_size(path, *, on_success=None, on_error=None, on_operation_started=None, on_progress=None):
        captured["on_operation_started"] = on_operation_started
        on_operation_started(OperationId("operation-1"))

    controller.directory_size.side_effect = _directory_size

    future = DaemonSftpManager.directory_size(manager, "/tree")
    assert future.cancel() is True
    controller.cancel_operation.assert_called_once_with(OperationId("operation-1"))


def test_directory_size_future_cancel_before_operation_id_is_known_still_cancels():
    """Cancel pressed before the start RPC returns its operation id must not
    be silently dropped -- it must fire as soon as the id becomes known."""
    controller = Mock()
    controller.state = SftpControllerState.READY
    controller.service_id = SftpServiceId("svc-1")
    manager = _bound_manager(controller)
    captured = {}

    def _directory_size(path, *, on_success=None, on_error=None, on_operation_started=None, on_progress=None):
        # The start RPC is still in flight: don't deliver the operation id yet.
        captured["on_operation_started"] = on_operation_started

    controller.directory_size.side_effect = _directory_size

    future = DaemonSftpManager.directory_size(manager, "/tree")
    assert future.cancel() is True
    controller.cancel_operation.assert_not_called()

    captured["on_operation_started"](OperationId("operation-2"))
    controller.cancel_operation.assert_called_once_with(OperationId("operation-2"))


def test_directory_size_progress_is_surfaced_through_progress_signal():
    controller = Mock()
    controller.state = SftpControllerState.READY
    controller.service_id = SftpServiceId("svc-1")
    manager = _bound_manager(controller)

    def _directory_size(path, *, on_success=None, on_error=None, on_operation_started=None, on_progress=None):
        on_progress(SimpleNamespace(progress=0.5, message="Measuring directory"))

    controller.directory_size.side_effect = _directory_size
    DaemonSftpManager.directory_size(manager, "/tree")
    manager.emit.assert_called_once_with("progress", 0.5, "Measuring directory")


def test_directory_size_cancelled_operation_resolves_as_transfer_cancelled():
    """A CANCELLED terminal operation must resolve the future the same way a
    cancelled transfer does, not as an opaque ``sftp_command_failed``."""
    controller = Mock()
    controller.state = SftpControllerState.READY
    controller.service_id = SftpServiceId("svc-1")
    manager = _bound_manager(controller)

    def _directory_size(path, *, on_success=None, on_error=None, on_operation_started=None, on_progress=None):
        on_error(SshPilotError(ErrorCode.OPERATION_CANCELLED, "The operation was cancelled"))

    controller.directory_size.side_effect = _directory_size
    future = DaemonSftpManager.directory_size(manager, "/tree")
    with pytest.raises(TransferCancelledException):
        future.result(timeout=1)


def test_recursive_remove_future_cancel_calls_operations_cancel():
    """Cancelling a recursive-remove future must reach ``operations.cancel``
    -- this is the highest-risk case, since an uncancelled daemon delete
    keeps destroying the tree after the user gives up on it."""
    controller = Mock()
    controller.state = SftpControllerState.READY
    controller.service_id = SftpServiceId("svc-1")
    manager = _bound_manager(controller)

    def _remove(path, *, recursive, on_success=None, on_error=None, on_operation_started=None, on_progress=None):
        on_operation_started(OperationId("operation-remove-1"))

    controller.remove.side_effect = _remove

    future = DaemonSftpManager.remove(manager, "/tree")
    assert future.cancel() is True
    controller.cancel_operation.assert_called_once_with(OperationId("operation-remove-1"))


def test_recursive_remove_cancel_propagates_end_to_end_through_controller(
    controller, mock_client, mock_bridge
):
    """Full round trip through the real ``DaemonSftpServiceController``:

    start recursive remove -> Future returned -> ``future.cancel()`` ->
    ``operations.cancel`` called -> daemon reaches CANCELLED -> the frontend
    Future terminates as cancelled (``TransferCancelledException``), matching
    a cancelled transfer.
    """
    _mark_ready(controller)
    manager = _bound_manager(controller)

    started = OperationSummary(
        operation_id=OperationId("operation-remove-9"),
        kind=OperationKind.SFTP_REMOVE_TREE,
        state=OperationState.RUNNING,
        message="Deleting /tree",
        created_at=utc_now(),
        owner_client_id=ClientId("client-1"),
    )
    cancelled = OperationSummary(
        operation_id=started.operation_id,
        kind=OperationKind.SFTP_REMOVE_TREE,
        state=OperationState.CANCELLED,
        message="The operation was cancelled",
        created_at=started.created_at,
        owner_client_id=ClientId("client-1"),
    )
    mock_client.sftp_remove.return_value = started
    mock_bridge.submit.side_effect = (
        lambda factory, on_success=None, on_error=None: on_success(factory())
    )

    future = DaemonSftpManager.remove(manager, "/tree")

    assert future.cancel() is True
    mock_client.cancel_operation.assert_called_once_with(started.operation_id)

    mock_client.get_operation.return_value = cancelled
    controller._poll_operations()

    with pytest.raises(TransferCancelledException):
        future.result(timeout=1)


def test_recursive_move_future_cancel_calls_operations_cancel():
    controller = Mock()
    controller.state = SftpControllerState.READY
    controller.service_id = SftpServiceId("svc-1")
    manager = _bound_manager(controller)

    def _copy(
        source_path,
        destination_path,
        *,
        recursive,
        move,
        on_success=None,
        on_error=None,
        on_operation_started=None,
        on_progress=None,
    ):
        assert recursive is True
        assert move is True
        on_operation_started(OperationId("operation-move-1"))

    controller.copy.side_effect = _copy

    future = DaemonSftpManager.copy_remote(
        manager, "/tree", "/dest", recursive=True, move=True
    )
    assert future.cancel() is True
    controller.cancel_operation.assert_called_once_with(OperationId("operation-move-1"))


def test_count_pass_skips_when_closed():
    """A closed manager must not start further directory-count RPCs."""
    controller = Mock()
    fake = types.SimpleNamespace(
        _closed=True,
        _sftp_controller=controller,
        emit=Mock(),
    )
    folders = [
        FileEntry(name="a", is_dir=True, size=0, modified=0),
        FileEntry(name="b", is_dir=True, size=0, modified=0),
    ]
    DaemonSftpManager._start_count_pass(fake, "/home/user", folders)
    controller.list_directory.assert_not_called()


def test_count_pass_abandons_on_service_not_ready():
    """Not-ready mid-pass must stop chaining (quit race), not raise."""
    calls = []

    def _list(path, *, on_success, on_error, cursor=None, limit=None):
        calls.append(path)
        on_error(SshPilotError(ErrorCode.SFTP_SERVICE_NOT_READY, "The SFTP service is not ready"))

    fake = types.SimpleNamespace(
        _closed=False,
        _sftp_controller=types.SimpleNamespace(list_directory=_list),
        emit=Mock(),
    )
    folders = [
        FileEntry(name="a", is_dir=True, size=0, modified=0),
        FileEntry(name="b", is_dir=True, size=0, modified=0),
    ]
    DaemonSftpManager._start_count_pass(fake, "/home/user", folders)
    assert calls == ["/home/user/a"]
    fake.emit.assert_not_called()


def test_open_attaches_ready_service_when_controlmaster_enabled(
    controller, mock_client, monkeypatch
):
    """With multiplexing on, a second open reuses a READY daemon SFTP service."""
    from datetime import datetime, timezone

    from sshpilot.api.models.operations import SftpServiceState, SftpServiceSummary

    existing = SftpServiceSummary(
        id=SftpServiceId("svc-ready"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.READY,
        created_at=datetime.now(timezone.utc),
    )
    mock_client.list_sftp_services.return_value = [existing]
    mock_client.attach_sftp.return_value = existing
    monkeypatch.setattr(controller, "_controlmaster_reuse_enabled", lambda: True)

    def _bridge_submit(op, on_success=None, on_error=None):
        try:
            result = op()
        except Exception as exc:  # pragma: no cover
            on_error(exc)
            return
        on_success(result)

    controller._submit = (
        lambda op, on_success=None, on_error=None: _bridge_submit(
            op, on_success=on_success, on_error=on_error
        )
    )

    controller.open(ConnectionId("conn-1"))

    mock_client.attach_sftp.assert_called_once()
    mock_client.open_sftp.assert_not_called()
    assert controller.service_id == SftpServiceId("svc-ready")


def test_open_creates_service_when_controlmaster_disabled(
    controller, mock_client, monkeypatch
):
    from datetime import datetime, timezone

    from sshpilot.api.models.operations import (
        OpenSftpRequest,
        SftpServiceState,
        SftpServiceSummary,
    )

    existing = SftpServiceSummary(
        id=SftpServiceId("svc-ready"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.READY,
        created_at=datetime.now(timezone.utc),
    )
    created = SftpServiceSummary(
        id=SftpServiceId("svc-new"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.READY,
        created_at=datetime.now(timezone.utc),
    )
    mock_client.list_sftp_services.return_value = [existing]
    mock_client.open_sftp.return_value = created
    monkeypatch.setattr(controller, "_controlmaster_reuse_enabled", lambda: False)

    def _bridge_submit(op, on_success=None, on_error=None):
        on_success(op())

    controller._submit = (
        lambda op, on_success=None, on_error=None: _bridge_submit(
            op, on_success=on_success, on_error=on_error
        )
    )

    controller.open(ConnectionId("conn-1"))

    mock_client.open_sftp.assert_called_once()
    assert isinstance(mock_client.open_sftp.call_args.args[0], OpenSftpRequest)
    mock_client.attach_sftp.assert_not_called()
    assert controller.service_id == SftpServiceId("svc-new")


def test_late_starting_event_does_not_regress_ready(controller):
    """CREATED/STARTING after READY must not flip the controller back to opening.

    Reproduces the GoogleRouter file-manager race: open RPC reports READY,
    UI starts listdir, then a queued STARTING event used to set state=opening
    and surface "SFTP connection is not available".
    """
    from datetime import datetime, timezone

    from sshpilot.api.models.operations import SftpServiceState, SftpServiceSummary

    ready_events = []
    controller._on_ready = ready_events.append
    generation = 1
    with controller._lock:
        controller._generation = generation

    ready = SftpServiceSummary(
        id=SftpServiceId("svc-1"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.READY,
        created_at=datetime.now(timezone.utc),
    )
    starting = SftpServiceSummary(
        id=SftpServiceId("svc-1"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.STARTING,
        created_at=datetime.now(timezone.utc),
    )

    controller._on_open_accepted(ready, generation)
    assert controller.state is SftpControllerState.READY
    assert len(ready_events) == 1

    controller._on_open_accepted(starting, generation)
    assert controller.state is SftpControllerState.READY
    assert controller.service_id == SftpServiceId("svc-1")
    assert len(ready_events) == 1  # on_ready fires only on transition into READY


def test_closing_event_leaves_ready_and_rejects_ops(controller, mock_bridge):
    """READY → CLOSING must leave READY so local ops are rejected immediately.

    CREATED/STARTING must not regress READY, but CLOSING is shutdown: keeping
    READY lets is_connected stay true and sends list/upload/download to a
    service already shutting down.
    """
    from datetime import datetime, timezone

    from sshpilot.api.models.operations import SftpServiceState, SftpServiceSummary

    generation = 1
    with controller._lock:
        controller._generation = generation

    ready = SftpServiceSummary(
        id=SftpServiceId("svc-1"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.READY,
        created_at=datetime.now(timezone.utc),
    )
    closing = SftpServiceSummary(
        id=SftpServiceId("svc-1"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.CLOSING,
        created_at=datetime.now(timezone.utc),
    )

    controller._on_open_accepted(ready, generation)
    assert controller.state is SftpControllerState.READY

    controller._on_open_accepted(closing, generation)
    assert controller.state is not SftpControllerState.READY
    assert controller.state is SftpControllerState.CLOSING
    assert controller.service_id == SftpServiceId("svc-1")

    errors = []
    controller.list_directory(
        "/tmp",
        on_success=lambda _r: pytest.fail("should not succeed while closing"),
        on_error=errors.append,
    )
    assert len(errors) == 1
    assert isinstance(errors[0], SshPilotError)
    assert errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY
    mock_bridge.submit.assert_not_called()


def test_closed_event_fires_on_error_and_leaves_service_stuck(controller, mock_bridge):
    """READY → CLOSED must fire on_error, same as FAILED (see companion
    test_failed_event_fires_on_error below).

    A CLOSED event reaching here (as opposed to a self-initiated close/
    detach, which sets local state via _mark() and never reaches
    _on_open_accepted for its own close) means the service went away for a
    reason this tab did not request -- e.g. another attached client closed a
    shared/reused service. Previously this fired no signal at all, leaving
    the file manager tab's connection-error UI
    (FileManagerWindow._on_connection_error) permanently silent.
    """
    from datetime import datetime, timezone

    from sshpilot.api.models.operations import SftpServiceState, SftpServiceSummary

    errors = []
    controller._on_error = errors.append

    generation = 1
    with controller._lock:
        controller._generation = generation

    ready = SftpServiceSummary(
        id=SftpServiceId("svc-1"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.READY,
        created_at=datetime.now(timezone.utc),
    )
    closed = SftpServiceSummary(
        id=SftpServiceId("svc-1"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.CLOSED,
        created_at=datetime.now(timezone.utc),
    )

    controller._on_open_accepted(ready, generation)
    assert controller.state is SftpControllerState.READY

    controller._on_open_accepted(closed, generation)
    assert controller.state is SftpControllerState.CLOSED

    op_errors = []
    controller.list_directory(
        "/tmp",
        on_success=lambda _r: pytest.fail("must not succeed once closed"),
        on_error=op_errors.append,
    )
    assert len(op_errors) == 1
    assert op_errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY
    mock_bridge.submit.assert_not_called()

    assert len(errors) == 1
    assert errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY
    assert "closed" in str(errors[0]).lower()


def test_failed_event_fires_on_error(controller, mock_bridge):
    """READY → FAILED must fire on_error — same signal CLOSED fires above."""
    from datetime import datetime, timezone

    from sshpilot.api.models.operations import (
        SftpFailure,
        SftpFailureCode,
        SftpServiceState,
        SftpServiceSummary,
    )

    errors = []
    controller._on_error = errors.append

    generation = 1
    with controller._lock:
        controller._generation = generation

    ready = SftpServiceSummary(
        id=SftpServiceId("svc-1"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.READY,
        created_at=datetime.now(timezone.utc),
    )
    failed = SftpServiceSummary(
        id=SftpServiceId("svc-1"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.FAILED,
        created_at=datetime.now(timezone.utc),
        failure=SftpFailure(
            code=SftpFailureCode.CONNECTION_LOST,
            error_code=ErrorCode.SFTP_PROTOCOL_LOST,
        ),
    )

    controller._on_open_accepted(ready, generation)
    controller._on_open_accepted(failed, generation)

    assert controller.state is SftpControllerState.FAILED
    assert len(errors) == 1
    assert errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY
    assert "SFTP connection was lost" in str(errors[0])


def test_created_event_keeps_opening_before_ready(controller):
    from datetime import datetime, timezone

    from sshpilot.api.models.operations import SftpServiceState, SftpServiceSummary

    generation = 1
    with controller._lock:
        controller._generation = generation
        controller._state = SftpControllerState.OPENING

    created = SftpServiceSummary(
        id=SftpServiceId("svc-1"),
        connection_id=ConnectionId("conn-1"),
        state=SftpServiceState.CREATED,
        created_at=datetime.now(timezone.utc),
    )
    controller._on_open_accepted(created, generation)
    assert controller.state is SftpControllerState.OPENING
    assert controller.service_id == SftpServiceId("svc-1")


def test_event_for_other_connection_ignored_while_unbound(controller):
    from datetime import datetime, timezone

    from sshpilot.api.events import CoreEvent, EventType
    from sshpilot.api.models.operations import SftpServiceState, SftpServiceSummary

    events = []

    def _subscribe(cb):
        events.append(cb)
        return Mock(unsubscribe=Mock())

    controller._client.subscribe_events = _subscribe
    controller._ensure_events()
    assert events

    foreign = SftpServiceSummary(
        id=SftpServiceId("svc-other"),
        connection_id=ConnectionId("conn-other"),
        state=SftpServiceState.STARTING,
        created_at=datetime.now(timezone.utc),
    )
    events[0](
        CoreEvent(
            type=EventType.SFTP_STATE_CHANGED,
            payload=foreign,
            sequence=1,
            timestamp=datetime.now(timezone.utc),
        )
    )
    assert controller.service_id is None
    assert controller.state is SftpControllerState.IDLE
