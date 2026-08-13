"""Daemon SFTP service API integration (not GTK file-manager widgets).

Proves DaemonClient.open_sftp / list_sftp_services. Does not open the
built-in file manager or populate GTK listing models.
"""
from __future__ import annotations

import pytest

from sshpilot.api import DaemonClient

pytestmark = pytest.mark.integration


class TestSftpDaemonPath:
    """Prove the daemon SFTP service API works."""

    def test_open_sftp_returns_service_id(self, daemon_factory):
        """DaemonClient.open_sftp accepts the request and returns a service ID."""
        from sshpilot.api.models.operations import OpenSftpRequest

        server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        opened = client.open_sftp(OpenSftpRequest(connection_id=connection.id))
        assert opened.id is not None

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_list_sftp_services(self, daemon_factory):
        """DaemonClient.list_sftp_services tracks opened services."""
        from sshpilot.api.models.operations import (
            OpenSftpRequest,
            SftpServiceState,
        )

        server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        opened = client.open_sftp(OpenSftpRequest(connection_id=connection.id))

        services = client.list_sftp_services()
        matching = [s for s in services if s.id == opened.id]
        assert len(matching) == 1
        assert matching[0].state in {
            SftpServiceState.CREATED,
            SftpServiceState.STARTING,
            SftpServiceState.READY,
            SftpServiceState.FAILED,
        }

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)


# ---------------------------------------------------------------------------
# Forward integration: daemon forward path
# ---------------------------------------------------------------------------
