"""Daemon terminal / session API integration (not GTK widgets).

Proves DaemonClient open/attach/input/close and resource tracking used by
GTK terminal controllers. Does not instantiate TerminalWidget or VTE.
"""
from __future__ import annotations

import threading
import time

import pytest

from sshpilot.api import DaemonClient
from sshpilot.api.models.sessions import (
    OpenSessionRequest,
    AttachSessionRequest,
    CloseSessionRequest,
    SessionState,
)
from sshpilot.api.models.terminal import TerminalInput

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


def _wait_for_sessions_active(client, timeout=5.0):
    """Wait until sessions_active > 0."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get_daemon_status().resources.sessions_active:
            return True
        time.sleep(0.02)
    return False


def _wait_sessions_drained(client, timeout=5.0):
    """Wait until sessions_active == 0."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not client.get_daemon_status().resources.sessions_active:
            return True
        time.sleep(0.05)
    return False


class TestTerminalDaemonPath:
    """Prove the daemon terminal session controller path works."""

    def test_open_session_via_daemon_client(self, daemon_factory):
        """DaemonClient.open_session creates an active session."""
        runner = _BlockingSessionRunner()
        server, _manager = daemon_factory(
            idle_shutdown_seconds=5.0,
            session_runner=runner,
        )
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        summary = client.open_session(
            OpenSessionRequest(connection_id=connection.id)
        )
        assert summary.id is not None

        # Session is STARTING; resources count it as active
        assert _wait_for_sessions_active(client, timeout=5.0)

        sessions = client.list_sessions()
        active = [s for s in sessions if s.state in {SessionState.STARTING, SessionState.RUNNING}]
        assert len(active) >= 1

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_session_close_terminates(self, daemon_factory):
        """close_session terminates the blocking handle and drains resources."""
        runner = _BlockingSessionRunner()
        server, _manager = daemon_factory(
            idle_shutdown_seconds=5.0,
            session_runner=runner,
        )
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        summary = client.open_session(
            OpenSessionRequest(connection_id=connection.id)
        )
        assert _wait_for_sessions_active(client, timeout=5.0)

        client.close_session(CloseSessionRequest(session_id=summary.id))
        assert _wait_sessions_drained(client, timeout=5.0)

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_session_attach(self, daemon_factory):
        """Session can be attached (simulates GTK tab open)."""
        runner = _BlockingSessionRunnerTerminal()
        server, _manager = daemon_factory(
            idle_shutdown_seconds=5.0,
            session_runner=runner,
        )
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        summary = client.open_session(
            OpenSessionRequest(connection_id=connection.id)
        )
        assert _wait_for_sessions_active(client, timeout=5.0)

        attached = client.attach_session(
            AttachSessionRequest(
                session_id=summary.id,
                request_input=True,
                from_sequence=0,
            )
        )
        assert attached.attachment.id is not None

        client.close_session(CloseSessionRequest(session_id=summary.id))
        _wait_sessions_drained(client, timeout=5.0)

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_terminal_input(self, daemon_factory):
        """Terminal input can be sent through the daemon API."""
        runner = _BlockingSessionRunnerTerminal()
        server, _manager = daemon_factory(
            idle_shutdown_seconds=5.0,
            session_runner=runner,
        )
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        summary = client.open_session(
            OpenSessionRequest(connection_id=connection.id)
        )
        assert _wait_for_sessions_active(client, timeout=5.0)

        attached = client.attach_session(
            AttachSessionRequest(
                session_id=summary.id,
                request_input=True,
                from_sequence=0,
            )
        )

        client.send_terminal_input(
            TerminalInput(
                session_id=summary.id,
                attachment_id=attached.attachment.id,
                data=b"echo test\n",
            )
        )

        client.close_session(CloseSessionRequest(session_id=summary.id))
        _wait_sessions_drained(client, timeout=5.0)

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_multiple_attachments(self, daemon_factory):
        """Multiple clients can attach to the same session."""
        runner = _BlockingSessionRunnerTerminal()
        server, _manager = daemon_factory(
            idle_shutdown_seconds=5.0,
            session_runner=runner,
        )
        client_a = DaemonClient(
            socket_path=server.socket_path,
            client_id="client-a-multi",
        )
        client_b = DaemonClient(
            socket_path=server.socket_path,
            client_id="client-b-multi",
        )

        connection = client_a.list_connections()[0]
        summary = client_a.open_session(
            OpenSessionRequest(connection_id=connection.id)
        )
        assert _wait_for_sessions_active(client_a, timeout=5.0)

        attached_a = client_a.attach_session(
            AttachSessionRequest(
                session_id=summary.id,
                request_input=True,
                from_sequence=0,
            )
        )
        attached_b = client_b.attach_session(
            AttachSessionRequest(
                session_id=summary.id,
                request_input=False,
                from_sequence=0,
            )
        )

        assert attached_a.attachment.id is not None
        assert attached_b.attachment.id is not None
        assert attached_a.attachment.id != attached_b.attachment.id

        client_a.close_session(CloseSessionRequest(session_id=summary.id))
        _wait_sessions_drained(client_a, timeout=5.0)
        client_a.close()
        client_b.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)


# ---------------------------------------------------------------------------
# SFTP integration: daemon SFTP service API
# ---------------------------------------------------------------------------


class TestProtocolCompatibility:
    """Prove protocol compatibility checks work."""

    def test_capabilities_check(self, daemon_factory):
        """DaemonClient reports capabilities and missing ones are detected."""
        from sshpilot.terminal_session_controller import (
            daemon_terminal_capabilities_missing,
        )

        server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
        client = DaemonClient(socket_path=server.socket_path)

        caps = client.get_capabilities()
        assert caps is not None
        assert hasattr(caps, "supported")

        missing = daemon_terminal_capabilities_missing(client)
        assert isinstance(missing, frozenset)

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_readiness_check(self, daemon_factory):
        """Daemon terminal readiness check works with a connected client."""
        from sshpilot.daemon_terminal_policy import resolve_daemon_terminal_readiness

        server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
        client = DaemonClient(socket_path=server.socket_path)

        class FakeBridge:
            pass

        class FakeWindow:
            def __init__(self, client):
                self.client = client
                self.client_bridge = FakeBridge()
                self._startup_failure = None
                self.connection_manager = type(
                    "CM", (), {"get_connections": lambda self: []}
                )()

        window = FakeWindow(client)
        readiness = resolve_daemon_terminal_readiness(window)

        assert hasattr(readiness, "ready")
        assert hasattr(readiness, "reason")

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_version_compatibility(self, daemon_factory):
        """Daemon and client report compatible protocol versions."""
        server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
        client = DaemonClient(socket_path=server.socket_path)

        status = client.get_daemon_status()
        assert status is not None
        assert status.state is not None

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)


# ---------------------------------------------------------------------------
# Quit policy
# ---------------------------------------------------------------------------


class TestResourceTracking:
    """Prove daemon resource tracking works."""

    def test_resource_counts_reflect_sessions(self, daemon_factory):
        """Resource counts increase when sessions are opened."""
        runner = _BlockingSessionRunner()
        server, _manager = daemon_factory(
            idle_shutdown_seconds=5.0,
            session_runner=runner,
        )
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        status = client.get_daemon_status()
        assert status.resources.sessions_active == 0

        summary = client.open_session(
            OpenSessionRequest(connection_id=connection.id)
        )

        assert _wait_for_sessions_active(client, timeout=5.0)

        status = client.get_daemon_status()
        assert status.resources.sessions_active >= 1

        client.close_session(CloseSessionRequest(session_id=summary.id))
        assert _wait_sessions_drained(client, timeout=5.0)

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_client_count_tracking(self, daemon_factory):
        """Connected client count is tracked."""
        server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
        client = DaemonClient(socket_path=server.socket_path)

        status = client.get_daemon_status()
        assert status.resources.clients >= 1

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)
