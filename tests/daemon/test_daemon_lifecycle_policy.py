"""Phase 11 daemon lifecycle policy, idle shutdown, and management APIs."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from sshpilot.api import Capability, DaemonClient, ErrorCode, SshPilotError
from sshpilot.api.models.daemon import (
    DaemonLifecycleState,
    RestartDaemonRequest,
    StopDaemonRequest,
)
from sshpilot.api.models.sessions import OpenSessionRequest, SessionExitInfo
from sshpilot.daemon.runtime_cleanup import sweep_stale_askpass_sockets


class _BlockingSessionRunner:
    """Keep a session STARTING/RUNNING until terminate/kill."""

    def __init__(self) -> None:
        self.started = threading.Event()

    def start(self, spec, on_exit, on_output=None):
        self.started.set()
        return _BlockingHandle(on_exit)

    def close(self):
        pass


class _BlockingHandle:
    def __init__(self, on_exit) -> None:
        self._on_exit = on_exit
        self.terminated = 0
        self.killed = 0

    def terminate(self):
        self.terminated += 1
        self._on_exit(SessionExitInfo(exit_code=0, reason="terminated"))

    def kill(self):
        self.killed += 1
        self._on_exit(SessionExitInfo(exit_code=-1, signal="KILL", reason="killed"))

    def resize(self, *_args, **_kwargs):
        return None

    def write(self, _data):
        return None

    def close_input(self):
        return None


def test_daemon_status_reports_ready_lifecycle(daemon_factory):
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    client = DaemonClient(socket_path=server.socket_path)
    status = client.get_daemon_status()
    assert status.state is DaemonLifecycleState.READY
    assert status.server_instance_id
    assert status.idle.idle_shutdown_enabled is False
    assert status.resources.clients >= 1
    diagnostics = client.get_daemon_diagnostics()
    assert diagnostics.status.server_instance_id == status.server_instance_id
    assert diagnostics.uptime_seconds >= 0
    assert diagnostics.socket_bound is True
    client.close()
    server.shutdown()
    assert server.wait_stopped()


def test_stop_without_resources_is_accepted(daemon_factory):
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    client = DaemonClient(socket_path=server.socket_path)
    result = client.stop_daemon(StopDaemonRequest())
    assert result.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)
    assert not server.socket_path.exists()


def test_stop_with_live_session_requires_confirmation(daemon_factory):
    runner = _BlockingSessionRunner()
    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.0,
        drain_timeout_seconds=0.2,
        session_runner=runner,
        session_shutdown_timeout=1.0,
    )
    client = DaemonClient(socket_path=server.socket_path)
    connection = client.list_connections()[0]
    opened = client.open_session(OpenSessionRequest(connection_id=connection.id))
    assert opened.id
    assert runner.started.wait(timeout=2.0)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        status = client.get_daemon_status()
        if status.resources.sessions_active:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("session never became an idle blocker")
    refused = client.stop_daemon(StopDaemonRequest())
    assert refused.accepted is False
    assert refused.confirmation
    assert "sessions" in refused.will_lose
    accepted = client.stop_daemon(
        StopDaemonRequest(confirmation=refused.confirmation)
    )
    assert accepted.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)


def test_force_stop_bypasses_confirmation(daemon_factory):
    runner = _BlockingSessionRunner()
    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.0,
        drain_timeout_seconds=0.2,
        session_runner=runner,
        session_shutdown_timeout=1.0,
    )
    client = DaemonClient(socket_path=server.socket_path)
    connection = client.list_connections()[0]
    client.open_session(OpenSessionRequest(connection_id=connection.id))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if client.get_daemon_status().resources.sessions_active:
            break
        time.sleep(0.02)
    result = client.stop_daemon(StopDaemonRequest(force=True))
    assert result.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)


def test_restart_sets_restart_requested(daemon_factory):
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    client = DaemonClient(socket_path=server.socket_path)
    result = client.restart_daemon(RestartDaemonRequest())
    assert result.accepted is True
    assert result.restart_requested is True
    client.close()
    assert server.wait_stopped(timeout=5.0)


def test_idle_shutdown_after_clients_leave(daemon_factory):
    server, _manager = daemon_factory(idle_shutdown_seconds=0.25)
    client = DaemonClient(socket_path=server.socket_path)
    status = client.get_daemon_status()
    assert status.idle.idle_shutdown_enabled is True
    assert status.idle.idle_shutdown_seconds == 0.25
    client.close()
    assert server.wait_stopped(timeout=5.0)
    assert not server.socket_path.exists()


def test_idle_countdown_cancelled_by_new_client(daemon_factory):
    server, _manager = daemon_factory(idle_shutdown_seconds=0.4)
    first = DaemonClient(socket_path=server.socket_path)
    first.close()
    time.sleep(0.15)
    second = DaemonClient(socket_path=server.socket_path)
    status = second.get_daemon_status()
    assert status.state in {DaemonLifecycleState.READY, DaemonLifecycleState.IDLE}
    time.sleep(0.35)
    assert not server.stopped
    second.close()
    assert server.wait_stopped(timeout=5.0)


def test_draining_rejects_new_session_open(daemon_factory):
    runner = _BlockingSessionRunner()
    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.0,
        drain_timeout_seconds=2.0,
        session_runner=runner,
    )
    client = DaemonClient(socket_path=server.socket_path)
    connection = client.list_connections()[0]
    client.open_session(OpenSessionRequest(connection_id=connection.id))
    assert runner.started.wait(timeout=2.0)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if client.get_daemon_status().resources.sessions_active:
            break
        time.sleep(0.02)
    refused = client.stop_daemon(StopDaemonRequest())
    assert refused.accepted is False
    accepted = client.stop_daemon(
        StopDaemonRequest(confirmation=refused.confirmation)
    )
    assert accepted.accepted is True
    status = client.get_daemon_status()
    assert status.state is DaemonLifecycleState.DRAINING
    with pytest.raises(SshPilotError) as excinfo:
        client.open_session(OpenSessionRequest(connection_id=connection.id))
    assert excinfo.value.code is ErrorCode.DAEMON_SHUTTING_DOWN
    # Status remains available while draining.
    assert client.get_daemon_status().state is DaemonLifecycleState.DRAINING
    client.close()
    assert server.wait_stopped(timeout=5.0)


def test_capabilities_include_daemon_management(daemon_factory):
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    client = DaemonClient(socket_path=server.socket_path)
    supported = client.get_capabilities().supported
    assert Capability.DAEMON_STATUS in supported
    assert Capability.DAEMON_CONTROL in supported
    assert Capability.DAEMON_EVENTS in supported
    client.close()
    server.shutdown()
    assert server.wait_stopped()


def test_sweep_removes_stale_askpass_sockets(tmp_path):
    runtime = tmp_path / "sshpilot"
    runtime.mkdir(mode=0o700)
    for index in range(120):
        path = runtime / f"askpass-{index:04d}.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        sock.close()
        path.chmod(0o600)
    live = runtime / "askpass-live.sock"
    live_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live_sock.bind(str(live))
    live_sock.listen(1)
    live.chmod(0o600)
    removed = sweep_stale_askpass_sockets(runtime, limit=512)
    assert removed >= 100
    assert live.exists()
    live_sock.close()
    sweep_stale_askpass_sockets(runtime)
    assert not Path(live).exists()
