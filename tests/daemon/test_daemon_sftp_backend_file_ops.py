"""Daemon-owned file-manager mutations (touch/remove) must not use local
temporary files or frontend-orchestrated recursion."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.daemon_sftp_backend import DaemonSftpManager, _localized_direct_error
from sshpilot.gtk import sftp_error_messages
from sshpilot.sftp_service_controller import SftpControllerState

SERVICE_ID = "sftp-1"


class _Controller:
    def __init__(self):
        self.state = SftpControllerState.READY
        self.service_id = SERVICE_ID
        self.create_calls = []
        self.remove_calls = []

    def create_file(self, path, *, on_success, on_error):
        self.create_calls.append(path)
        on_success(SimpleNamespace(path=path))

    def remove(
        self,
        path,
        *,
        recursive,
        on_success,
        on_error,
        on_operation_started=None,
        on_progress=None,
    ):
        self.remove_calls.append((path, recursive))
        on_success(None)


def _manager(controller=None):
    controller = controller or _Controller()
    manager = DaemonSftpManager.__new__(DaemonSftpManager)
    manager._sftp_controller = controller
    manager._client = None
    manager._username = "user"
    manager._host = "web"
    manager._connection_id = "conn-1"
    manager._closed = False
    manager._home = None
    manager._cancelled_operations = set()
    return manager


def test_touch_creates_empty_remote_file_via_daemon():
    controller = _Controller()
    manager = _manager(controller)

    future = manager.touch("/home/user/new.txt")

    assert future.result() is None
    assert controller.create_calls == ["/home/user/new.txt"]
    assert controller.remove_calls == []


def test_touch_expands_home_and_reports_file_exists():
    controller = _Controller()
    manager = _manager(controller)
    manager._home = "/home/user"

    def _on_error(exc):
        raise AssertionError(f"unexpected error: {exc}")

    controller.create_file = lambda path, *, on_success, on_error: (
        controller.create_calls.append(path)
        or on_error(
            SshPilotError(
                ErrorCode.REMOTE_PATH_EXISTS,
                "The remote file already exists",
            )
        )
    )

    future = manager.touch("~/existing.txt")
    try:
        future.result()
    except FileExistsError as exc:
        assert exc.filename == "/home/user/existing.txt"
    else:  # pragma: no cover
        raise AssertionError("FileExistsError was not raised")
    assert controller.create_calls == ["/home/user/existing.txt"]


def test_remove_delegates_recursive_delete_to_daemon():
    controller = _Controller()
    manager = _manager(controller)

    future = manager.remove("/home/user/tree")

    assert future.result() is None
    assert controller.remove_calls == [("/home/user/tree", True)]


def test_direct_future_error_is_localized_without_losing_error_code(monkeypatch):
    monkeypatch.setattr(sftp_error_messages, "_", lambda _msgid: "Accès refusé")
    error = SshPilotError(
        ErrorCode.REMOTE_PERMISSION_DENIED,
        ErrorCode.REMOTE_PERMISSION_DENIED.value,
        details={"sftp_status": 3, "server_message": "Permission denied"},
    )

    localized = _localized_direct_error(error)

    assert isinstance(localized, SshPilotError)
    assert localized.code is ErrorCode.REMOTE_PERMISSION_DENIED
    assert localized.message == "Accès refusé"
    assert localized.details == error.details


def test_service_failure_error_is_unchanged_by_direct_adapter(monkeypatch):
    monkeypatch.setattr(
        sftp_error_messages,
        "_",
        lambda _msgid: pytest.fail("ServiceFailure must not be translated here"),
    )
    error = SshPilotError(
        ErrorCode.SFTP_SERVICE_NOT_READY,
        "The SFTP session could not be established",
    )

    assert _localized_direct_error(error) is error


def test_list_error_signal_contains_frontend_translation(monkeypatch):
    controller = _Controller()
    manager = _manager(controller)
    emitted = []
    monkeypatch.setattr(sftp_error_messages, "_", lambda _msgid: "Chemin introuvable")
    monkeypatch.setattr(
        DaemonSftpManager,
        "emit",
        lambda _self, *args: emitted.append(args),
    )

    def _list_directory(_path, *, on_success, on_error):
        del on_success
        on_error(
            SshPilotError(
                ErrorCode.REMOTE_PATH_NOT_FOUND,
                ErrorCode.REMOTE_PATH_NOT_FOUND.value,
                details={"sftp_status": 2, "server_message": "No such file"},
            )
        )

    controller.list_directory = _list_directory

    manager.listdir("/missing")

    assert emitted == [("operation-error", "Chemin introuvable")]
