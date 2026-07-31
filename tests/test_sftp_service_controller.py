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
