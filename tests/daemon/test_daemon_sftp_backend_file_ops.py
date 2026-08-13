"""Daemon-owned file-manager mutations (touch/remove) must not use local
temporary files or frontend-orchestrated recursion."""

from __future__ import annotations

from types import SimpleNamespace

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.daemon_sftp_backend import DaemonSftpManager
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
