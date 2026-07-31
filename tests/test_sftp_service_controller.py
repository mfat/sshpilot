"""DaemonSftpServiceController: not-ready errors go through on_error, not raises."""

import types
from unittest.mock import Mock

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, SftpServiceId
from sshpilot.daemon_sftp_backend import DaemonSftpManager
from sshpilot.file_manager.common import FileEntry
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
