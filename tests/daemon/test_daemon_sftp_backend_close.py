"""Closing a file manager must end the daemon SFTP service it was using.

Detaching alone left the daemon holding a READY service with no view attached:
an SSH connection outliving the tab that opened it, and a sidebar indicator
that stayed green for a connection the user had closed (GH #1193).
"""

from __future__ import annotations

import pytest

from sshpilot import daemon_sftp_backend
from sshpilot.daemon_sftp_backend import DaemonSftpManager

SERVICE_ID = "sftp-1"


class _Controller:
    def __init__(self, service_id=SERVICE_ID):
        self.service_id = service_id
        self.closed = 0
        self.detached = 0

    def close(self):
        self.closed += 1

    def detach(self):
        self.detached += 1


class _Transfers:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def _manager(controller=None):
    controller = controller or _Controller()
    manager = DaemonSftpManager.__new__(DaemonSftpManager)
    manager._sftp_controller = controller
    manager._transfers = _Transfers()
    manager._interaction_dialogs = None
    manager._closed = False
    return manager


@pytest.fixture(autouse=True)
def clean_registry():
    daemon_sftp_backend._SERVICE_USERS.clear()
    yield
    daemon_sftp_backend._SERVICE_USERS.clear()


def test_only_view_closing_ends_the_service():
    manager = _manager()
    daemon_sftp_backend._register_service_user(SERVICE_ID, id(manager))

    manager.close()

    assert manager._sftp_controller.closed == 1
    assert manager._sftp_controller.detached == 0
    assert manager._transfers.closed == 1
    # Nothing is left holding the service.
    assert SERVICE_ID not in daemon_sftp_backend._SERVICE_USERS


def test_shared_service_survives_until_the_last_view_closes():
    """SSH multiplexing can share one service across views."""
    first = _manager(_Controller())
    second = _manager(_Controller())
    for manager in (first, second):
        daemon_sftp_backend._register_service_user(SERVICE_ID, id(manager))

    first.close()
    assert first._sftp_controller.detached == 1
    assert first._sftp_controller.closed == 0

    second.close()
    assert second._sftp_controller.closed == 1
    assert second._sftp_controller.detached == 0


def test_close_is_idempotent():
    manager = _manager()
    daemon_sftp_backend._register_service_user(SERVICE_ID, id(manager))

    manager.close()
    manager.close()

    assert manager._sftp_controller.closed == 1
    assert manager._transfers.closed == 1


def test_unregistered_service_is_closed_by_its_holder():
    """Torn down before the service came up: the holder still ends it."""
    manager = _manager()

    manager.close()

    assert manager._sftp_controller.closed == 1
    assert manager._sftp_controller.detached == 0


def test_disconnect_service_ends_it_regardless_of_other_views():
    first = _manager(_Controller())
    second = _manager(_Controller())
    for manager in (first, second):
        daemon_sftp_backend._register_service_user(SERVICE_ID, id(manager))

    first.disconnect_service()

    assert first._sftp_controller.closed == 1
    assert first._closed is True
    # The other view is no longer counted as keeping it alive.
    assert daemon_sftp_backend._SERVICE_USERS[SERVICE_ID] == {id(second)}


def test_service_users_are_tracked_per_service():
    first = _manager(_Controller("sftp-1"))
    second = _manager(_Controller("sftp-2"))
    daemon_sftp_backend._register_service_user("sftp-1", id(first))
    daemon_sftp_backend._register_service_user("sftp-2", id(second))

    first.close()

    assert first._sftp_controller.closed == 1
    assert "sftp-1" not in daemon_sftp_backend._SERVICE_USERS
    assert daemon_sftp_backend._SERVICE_USERS["sftp-2"] == {id(second)}
