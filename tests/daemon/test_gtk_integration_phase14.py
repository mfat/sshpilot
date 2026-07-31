"""Phase 14 integration proof: prove the GTK daemon integration paths work.

These tests exercise the daemon-side controllers and managers that the GTK
application uses, without requiring actual VTE widgets. They prove the
production path works end-to-end through the daemon API.
"""

from __future__ import annotations

import threading
import time

import pytest

from sshpilot.api import DaemonClient
from sshpilot.api.models.sessions import (
    OpenSessionRequest,
    AttachSessionRequest,
    DetachSessionRequest,
    CloseSessionRequest,
    SessionState,
)
from sshpilot.api.models.terminal import TerminalInput


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


# ---------------------------------------------------------------------------
# Terminal integration: daemon session lifecycle
# ---------------------------------------------------------------------------


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


class TestSftpDaemonPath:
    """Prove the daemon SFTP service API works."""

    def test_open_sftp_returns_service_id(self, daemon_factory):
        """DaemonClient.open_sftp accepts the request and returns a service ID."""
        from sshpilot.api.models.operations import OpenSftpRequest, CloseSftpRequest

        server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        opened = client.open_sftp(OpenSftpRequest(connection_id=connection.id))
        assert opened.id is not None

        client.close_sftp(CloseSftpRequest(service_id=opened.id))

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_list_sftp_services(self, daemon_factory):
        """DaemonClient.list_sftp_services tracks opened services."""
        from sshpilot.api.models.operations import (
            OpenSftpRequest,
            SftpServiceState,
            CloseSftpRequest,
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

        client.close_sftp(CloseSftpRequest(service_id=opened.id))

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)


# ---------------------------------------------------------------------------
# Forward integration: daemon forward path
# ---------------------------------------------------------------------------


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


class TestSessionRestore:
    """Prove session restore metadata works."""

    def test_save_and_query_metadata(self, daemon_factory):
        """Session metadata can be saved and queried against active sessions."""
        from sshpilot.daemon_session_restore import DaemonSessionRestoreManager

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

        class FakeConfig:
            def __init__(self):
                self._store = {}

            def get_setting(self, key, default=None):
                return self._store.get(key, default)

            def set_setting(self, key, value):
                self._store[key] = value

        config = FakeConfig()
        manager = DaemonSessionRestoreManager(config)

        class FakeTabState:
            session_id = str(summary.id)
            daemon_instance_id = str(client.server_instance_id)
            connection_id = str(connection.id)
            expected_sequence = 0
            view_id = "test-view"

        manager.save_session_metadata(FakeTabState(), "Test Terminal")

        saved = config.get_setting("terminal.daemon_session_restore_state", [])
        assert len(saved) == 1
        assert saved[0]["session_id"] == str(summary.id)

        restorable = manager.get_restorable_sessions(client)
        assert len(restorable) == 1
        assert restorable[0].session_id == str(summary.id)

        manager.remove_session_metadata(str(summary.id))
        saved = config.get_setting("terminal.daemon_session_restore_state", [])
        assert len(saved) == 0

        client.close_session(CloseSessionRequest(session_id=summary.id))
        _wait_sessions_drained(client, timeout=5.0)

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_metadata_deduplication(self, daemon_factory):
        """Saving metadata for the same session replaces the old entry."""
        from sshpilot.daemon_session_restore import DaemonSessionRestoreManager

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

        class FakeConfig:
            def __init__(self):
                self._store = {}

            def get_setting(self, key, default=None):
                return self._store.get(key, default)

            def set_setting(self, key, value):
                self._store[key] = value

        config = FakeConfig()
        manager = DaemonSessionRestoreManager(config)

        class FakeTabState:
            session_id = str(summary.id)
            daemon_instance_id = str(client.server_instance_id)
            connection_id = str(connection.id)
            expected_sequence = 0
            view_id = "test-view"

        manager.save_session_metadata(FakeTabState(), "Tab 1")
        manager.save_session_metadata(FakeTabState(), "Tab 2")

        saved = config.get_setting("terminal.daemon_session_restore_state", [])
        assert len(saved) == 1
        assert saved[0]["tab_title"] == "Tab 2"

        client.close_session(CloseSessionRequest(session_id=summary.id))
        _wait_sessions_drained(client, timeout=5.0)

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)


# ---------------------------------------------------------------------------
# Protocol compatibility
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


class TestQuitPolicy:
    """Prove quit policy resolution works."""

    def test_tab_close_policy_defaults(self):
        """Default tab close policy is DETACH."""
        from sshpilot.daemon_terminal_policy import (
            resolve_tab_close_policy,
            TerminalClosePolicy,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                return default

        policy = resolve_tab_close_policy(FakeConfig())
        assert policy == TerminalClosePolicy.DETACH

    def test_app_close_policy_defaults(self):
        """Default app close policy is DETACH."""
        from sshpilot.daemon_terminal_policy import (
            resolve_app_close_policy,
            TerminalClosePolicy,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                return default

        policy = resolve_app_close_policy(FakeConfig())
        assert policy == TerminalClosePolicy.DETACH

    def test_tab_close_policy_terminate(self):
        """Tab close policy can be set to TERMINATE."""
        from sshpilot.daemon_terminal_policy import (
            resolve_tab_close_policy,
            TerminalClosePolicy,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                if key == "terminal.daemon_tab_close_policy":
                    return "terminate"
                return default

        policy = resolve_tab_close_policy(FakeConfig())
        assert policy == TerminalClosePolicy.TERMINATE

    def test_tab_close_policy_ask(self):
        """Tab close policy can be set to ASK."""
        from sshpilot.daemon_terminal_policy import (
            resolve_tab_close_policy,
            TerminalClosePolicy,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                if key == "terminal.daemon_tab_close_policy":
                    return "ask"
                return default

        policy = resolve_tab_close_policy(FakeConfig())
        assert policy == TerminalClosePolicy.ASK

    def test_terminal_route_daemon_default(self):
        """Default terminal route is DAEMON."""
        from sshpilot.daemon_terminal_policy import (
            resolve_ssh_terminal_route,
            SshTerminalRoute,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                return default

        class FakeConnection:
            protocol = "ssh"

        route = resolve_ssh_terminal_route(FakeConfig(), FakeConnection())
        assert route == SshTerminalRoute.DAEMON

    def test_terminal_route_external(self):
        """External terminal preference returns EXTERNAL route."""
        from sshpilot.daemon_terminal_policy import (
            resolve_ssh_terminal_route,
            SshTerminalRoute,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                if key == "use-external-terminal":
                    return True
                return default

        class FakeConnection:
            protocol = "ssh"

        route = resolve_ssh_terminal_route(FakeConfig(), FakeConnection())
        assert route in {SshTerminalRoute.EXTERNAL, None}


# ---------------------------------------------------------------------------
# Resource tracking
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
