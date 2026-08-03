"""Daemon forward API integration (not GTK forward UI).

Proves open/claim/list forwards via DaemonClient, including orphan claim.
"""
from __future__ import annotations

import threading
import time

import pytest

from sshpilot.api import DaemonClient

pytestmark = pytest.mark.integration


class _BlockingSessionRunner:
    """Keep a session STARTING/RUNNING until terminate/kill."""

    def __init__(self) -> None:
        self.started = threading.Event()

    def start(self, spec, on_exit, on_output=None, on_eof=None):
        self.started.set()
        return _BlockingHandle(on_exit)


class _BlockingSessionRunnerTerminal(_BlockingSessionRunner):
    """Variant that advertises terminal capability for input/output tests."""

    terminal_capable = True


class _BlockingHandle:
    def __init__(self, on_exit) -> None:
        self._on_exit = on_exit
        self._exit_info = None
        self._exit_event = threading.Event()

    def terminate(self):
        from sshpilot.api.models.sessions import SessionExitInfo

        self._exit_info = SessionExitInfo(exit_code=0, reason="terminated")
        self._exit_event.set()
        self._on_exit(self._exit_info)

    def kill(self):
        from sshpilot.api.models.sessions import SessionExitInfo

        self._exit_info = SessionExitInfo(exit_code=-1, signal="KILL", reason="killed")
        self._exit_event.set()
        self._on_exit(self._exit_info)

    def wait(self, timeout=None):
        self._exit_event.wait(timeout=timeout)
        return self._exit_info

    def resize(self, *_args, **_kwargs):
        return None

    def write(self, data):
        return None

    def close_input(self):
        return None


class TestForwardDaemonPath:
    """Prove the daemon forward path works."""

    def test_open_forward_returns_id(self, daemon_factory):
        """DaemonClient.open_forward returns a forward with an ID."""
        from sshpilot.api.models import ForwardType, OpenForwardRequest
        from sshpilot.api.models.operations import CloseForwardRequest

        runner = _BlockingSessionRunner()
        server, _manager = daemon_factory(
            idle_shutdown_seconds=5.0,
            session_runner=runner,
        )
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        fwd = client.open_forward(
            OpenForwardRequest(
                connection_id=connection.id,
                type=ForwardType.LOCAL,
                bind_host="127.0.0.1",
                bind_port=0,
                destination_host="127.0.0.1",
                destination_port=1,
            )
        )
        assert fwd.id is not None

        client.close_forward(CloseForwardRequest(forward_id=fwd.id))

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_claim_orphaned_forward(self, daemon_factory):
        """Client B can claim a forward orphaned by client A."""
        from sshpilot.api.models import ForwardType, OpenForwardRequest
        from sshpilot.api.models.operations import (
            ClaimForwardRequest,
            CloseForwardRequest,
        )

        server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
        client_a = DaemonClient(
            socket_path=server.socket_path,
            client_id="client-a-p14",
        )
        fwd = client_a.open_forward(
            OpenForwardRequest(
                connection_id=client_a.list_connections()[0].id,
                type=ForwardType.LOCAL,
                bind_host="127.0.0.1",
                bind_port=0,
                destination_host="127.0.0.1",
                destination_port=1,
            )
        )

        client_a.close()
        time.sleep(0.3)

        client_b = DaemonClient(
            socket_path=server.socket_path,
            client_id="client-b-p14",
        )
        claimed = client_b.claim_forward(ClaimForwardRequest(forward_id=fwd.id))
        assert claimed.owner_client_id == "client-b-p14"

        client_b.close_forward(CloseForwardRequest(forward_id=fwd.id))

        client_b.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_list_forwards(self, daemon_factory):
        """DaemonClient.list_forwards tracks opened forwards."""
        from sshpilot.api.models import ForwardType, OpenForwardRequest
        from sshpilot.api.models.operations import (
            ForwardState,
            CloseForwardRequest,
        )

        server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        fwd = client.open_forward(
            OpenForwardRequest(
                connection_id=connection.id,
                type=ForwardType.LOCAL,
                bind_host="127.0.0.1",
                bind_port=0,
                destination_host="127.0.0.1",
                destination_port=1,
            )
        )

        forwards = client.list_forwards()
        matching = [f for f in forwards if f.id == fwd.id]
        assert len(matching) == 1
        assert matching[0].state in {
            ForwardState.ACTIVE,
            ForwardState.STARTING,
            ForwardState.CREATED,
        }

        client.close_forward(CloseForwardRequest(forward_id=fwd.id))

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)


# ---------------------------------------------------------------------------
# Session restore metadata
# ---------------------------------------------------------------------------
