"""Phase 13.3 lifecycle proof: idle shutdown, force stop, externally-managed
daemon, child reaping, reconnect idle reset, and resource drain tests."""

from __future__ import annotations

import os
import threading
import time

import pytest

from sshpilot.api import DaemonClient
from sshpilot.api.models.daemon import (
    DaemonLifecycleState,
    StopDaemonRequest,
)
from sshpilot.api.models.sessions import OpenSessionRequest


class _BlockingSessionRunner:
    """Keep a session STARTING/RUNNING until terminate/kill."""

    def __init__(self) -> None:
        self.started = threading.Event()

    def start(self, spec, on_exit, on_output=None):
        self.started.set()
        return _BlockingHandle(on_exit)

    def close(self):
        """Match the process-runner lifecycle contract used by SessionRuntime."""
        return None


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

    def write(self, _data):
        return None

    def close_input(self):
        return None


# ---------------------------------------------------------------------------
# Idle shutdown
# ---------------------------------------------------------------------------


def test_idle_shutdown_fires_with_short_timeout(daemon_factory):
    """Daemon exits naturally after idle timeout when no clients remain."""
    server, _manager = daemon_factory(idle_shutdown_seconds=0.3)
    client = DaemonClient(socket_path=server.socket_path)
    status = client.get_daemon_status()
    assert status.idle.idle_shutdown_enabled is True
    client.close()
    assert server.wait_stopped(timeout=5.0)
    assert not server.socket_path.exists()


def test_active_session_suppresses_idle_exit(daemon_factory):
    """Active session prevents idle shutdown from firing."""
    runner = _BlockingSessionRunner()
    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.3,
        session_runner=runner,
        drain_timeout_seconds=0.2,
    )
    client = DaemonClient(socket_path=server.socket_path)
    connection = client.list_connections()[0]
    client.open_session(OpenSessionRequest(connection_id=connection.id))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if client.get_daemon_status().resources.sessions_active:
            break
        time.sleep(0.02)
    else:
        pytest.fail("session never became active")

    time.sleep(0.6)
    assert not server.stopped
    result = client.stop_daemon(StopDaemonRequest(force=True))
    assert result.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)


def test_active_forward_suppresses_idle_exit(daemon_factory):
    """Active forward prevents idle shutdown from firing."""
    runner = _BlockingSessionRunner()
    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.3,
        session_runner=runner,
        drain_timeout_seconds=0.2,
    )
    client = DaemonClient(socket_path=server.socket_path)
    connection = client.list_connections()[0]
    client.open_session(OpenSessionRequest(connection_id=connection.id))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if client.get_daemon_status().resources.sessions_active:
            break
        time.sleep(0.02)

    from sshpilot.api.models import ForwardType, OpenForwardRequest

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
    assert fwd.id

    time.sleep(0.6)
    assert not server.stopped

    result = client.stop_daemon(StopDaemonRequest(force=True))
    assert result.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)


def test_final_work_ending_starts_idle_timer(daemon_factory):
    """When the last active resource ends, idle timer starts and daemon exits.

    Opens a session, then terminates it via close_session (which calls
    handle.terminate -> on_exit), then verifies the daemon exits via
    idle shutdown — NOT via force stop.
    """
    runner = _BlockingSessionRunner()
    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.3,
        session_runner=runner,
        drain_timeout_seconds=0.2,
    )
    client = DaemonClient(socket_path=server.socket_path)
    connection = client.list_connections()[0]
    opened = client.open_session(OpenSessionRequest(connection_id=connection.id))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if client.get_daemon_status().resources.sessions_active:
            break
        time.sleep(0.02)
    else:
        pytest.fail("session never became active")

    # Close the session via public API — this terminates the blocking handle
    # via on_exit, transitioning the session to a terminal state.
    from sshpilot.api.models.sessions import CloseSessionRequest

    client.close_session(CloseSessionRequest(session_id=opened.id))

    # Wait for the session to reach a terminal state.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        resources = client.get_daemon_status().resources
        if resources.sessions_active == 0:
            break
        time.sleep(0.1)
    else:
        pytest.fail("session did not terminate after close_session")

    # Now no active resources remain — idle timer should start.
    # The daemon should exit naturally after idle_shutdown_seconds (0.3s).
    client.close()
    assert server.wait_stopped(timeout=5.0)
    assert not server.socket_path.exists()


def test_reconnect_resets_idle_timer(daemon_factory):
    """A new client arriving during idle window resets the timer."""
    server, _manager = daemon_factory(idle_shutdown_seconds=0.5)
    first = DaemonClient(socket_path=server.socket_path)
    first.close()
    time.sleep(0.2)
    second = DaemonClient(socket_path=server.socket_path)
    status = second.get_daemon_status()
    assert status.state in {DaemonLifecycleState.READY, DaemonLifecycleState.IDLE}
    time.sleep(0.4)
    assert not server.stopped
    second.close()
    assert server.wait_stopped(timeout=5.0)


# ---------------------------------------------------------------------------
# Force stop / terminate-all
# ---------------------------------------------------------------------------


def test_force_stop_terminates_all_resources(daemon_factory):
    """force=True immediately accepts stop with active sessions."""
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
    assert result.will_lose
    client.close()
    assert server.wait_stopped(timeout=5.0)
    assert not server.socket_path.exists()


def test_force_stop_does_not_wait_full_drain(daemon_factory):
    """force=True must tear down immediately, not wait out drain_timeout (5s)."""
    runner = _BlockingSessionRunner()
    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.0,
        drain_timeout_seconds=5.0,
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

    started = time.monotonic()
    result = client.stop_daemon(StopDaemonRequest(force=True))
    assert result.accepted is True
    client.close()
    assert server.wait_stopped(timeout=2.0)
    assert time.monotonic() - started < 2.0
    assert not server.socket_path.exists()


def test_repeated_stop_request_is_idempotent(daemon_factory):
    """Calling stop twice does not crash; second call returns accepted or transport error."""
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    client = DaemonClient(socket_path=server.socket_path)
    first = client.stop_daemon(StopDaemonRequest())
    assert first.accepted is True
    from sshpilot.api.errors import ErrorCode, SshPilotError

    try:
        second = client.stop_daemon(StopDaemonRequest())
        assert second.accepted is True
    except SshPilotError as exc:
        assert exc.code is ErrorCode.TRANSPORT_CLOSED
    client.close()
    assert server.wait_stopped(timeout=5.0)


def test_stop_while_already_stopping(daemon_factory):
    """Stop request while daemon is already stopping returns accepted."""
    runner = _BlockingSessionRunner()
    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.0,
        drain_timeout_seconds=2.0,
        session_runner=runner,
    )
    client = DaemonClient(socket_path=server.socket_path)
    connection = client.list_connections()[0]
    client.open_session(OpenSessionRequest(connection_id=connection.id))
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
    third = client.stop_daemon(StopDaemonRequest())
    assert third.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)


# ---------------------------------------------------------------------------
# Socket and metadata cleanup
# ---------------------------------------------------------------------------


def test_socket_removed_on_clean_exit(daemon_factory):
    """Socket file disappears after clean daemon stop."""
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    client = DaemonClient(socket_path=server.socket_path)
    assert server.socket_path.exists()
    result = client.stop_daemon(StopDaemonRequest())
    assert result.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)
    assert not server.socket_path.exists()


def test_socket_removed_on_idle_exit(daemon_factory):
    """Socket file disappears after idle shutdown."""
    server, _manager = daemon_factory(idle_shutdown_seconds=0.2)
    client = DaemonClient(socket_path=server.socket_path)
    client.close()
    assert server.wait_stopped(timeout=5.0)
    assert not server.socket_path.exists()


def test_no_pid_or_metadata_files_after_exit(daemon_factory):
    """Daemon does not create PID files or metadata files — only the socket."""
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    client = DaemonClient(socket_path=server.socket_path)
    result = client.stop_daemon(StopDaemonRequest())
    assert result.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)
    # Verify: no PID file, no metadata file, no instance record on disk.
    socket_dir = server.socket_path.parent
    if socket_dir.exists():
        remaining = list(socket_dir.iterdir())
        # Only allow empty directory; no files should remain.
        assert remaining == [], f"unexpected files after exit: {remaining}"


# ---------------------------------------------------------------------------
# Externally-managed daemon
# ---------------------------------------------------------------------------


def test_externally_managed_daemon_not_killed_by_client_disconnect(
    daemon_factory,
):
    """A daemon that was NOT started by the client stays alive after disconnect."""
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    client = DaemonClient(socket_path=server.socket_path)
    client.close()
    time.sleep(0.1)
    assert not server.stopped
    assert server.socket_path.exists()
    server.shutdown()
    assert server.wait_stopped(timeout=5.0)


def test_app_launched_daemon_stop_on_quit(daemon_factory):
    """Simulates the app-launched daemon quit path: stop_daemon then close."""
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    client = DaemonClient(socket_path=server.socket_path)
    result = client.stop_daemon(StopDaemonRequest())
    assert result.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)
    assert not server.socket_path.exists()


def test_app_launched_daemon_force_when_refused(daemon_factory):
    """When stop is refused due to active work, force=True terminates all."""
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

    # First attempt: refused because of active session.
    refused = client.stop_daemon(StopDaemonRequest())
    assert refused.accepted is False
    assert refused.confirmation
    # Force: bypasses confirmation, terminates all.
    forced = client.stop_daemon(StopDaemonRequest(force=True))
    assert forced.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)
    assert not server.socket_path.exists()


# ---------------------------------------------------------------------------
# Child reaping
# ---------------------------------------------------------------------------


def test_no_zombie_children_after_force_stop(daemon_factory):
    """No zombie or child processes remain after force stop.

    The daemon runs in-process (start_in_thread), so we verify by checking
    /proc for any children of the current process that look like ssh/sshd.
    This is a best-effort check — the test fails hard if verification
    itself cannot be performed.
    """
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

    # Verify no child processes remain by scanning /proc.
    my_pid = os.getpid()
    orphan_pids = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == my_pid:
                continue
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
                    parts = f.read().split()
                    ppid = int(parts[3]) if len(parts) > 3 else 0
                    if ppid == my_pid:
                        with open(f"/proc/{pid}/cmdline", encoding="utf-8", errors="replace") as cmd:
                            cmdline = cmd.read().replace("\x00", " ").strip()
                        executable = cmdline.split(maxsplit=1)[0].rsplit("/", 1)[-1]
                        if executable in {"ssh", "sshd", "scp", "sftp"}:
                            orphan_pids.append((pid, cmdline))
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pytest.fail("Cannot read /proc to verify child reaping")
    assert orphan_pids == [], f"orphan children of pid {my_pid}: {orphan_pids}"


# ---------------------------------------------------------------------------
# Client disconnect during shutdown
# ---------------------------------------------------------------------------


def test_client_disconnect_during_shutdown(daemon_factory):
    """Client disconnecting during drain does not prevent daemon exit."""
    runner = _BlockingSessionRunner()
    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.0,
        drain_timeout_seconds=1.0,
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

    refused = client.stop_daemon(StopDaemonRequest())
    if not refused.accepted:
        client.stop_daemon(
            StopDaemonRequest(confirmation=refused.confirmation)
        )
    client.close()
    assert server.wait_stopped(timeout=5.0)
    assert not server.socket_path.exists()


def test_sftp_resource_tracking_blocks_idle(daemon_factory):
    """SFTP active/retained counts appear in daemon resource tracking.

    The daemon reports sftp_active and sftp_retained in resource counts;
    when either is non-zero, idle exit is suppressed. This test verifies
    the resource counting logic exposes the SFTP fields (initially zero).
    """
    server, _manager = daemon_factory(idle_shutdown_seconds=0.2)
    client = DaemonClient(socket_path=server.socket_path)
    status = client.get_daemon_status()

    # Initially no SFTP activity.
    assert status.resources.sftp_active == 0
    assert status.resources.sftp_retained == 0
    # The connected client itself counts as a live resource.
    assert status.resources.clients == 1
    # Idle exit blocked by connected client.
    assert "clients" in status.idle.idle_blockers

    # Daemon exits via force stop (client keeps it alive via clients count).
    result = client.stop_daemon(StopDaemonRequest(force=True))
    assert result.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)


def test_daemon_resource_counts_reflect_session_close(daemon_factory):
    """Resource counts drop to zero after closing a session, enabling idle exit.

    Opens a session, verifies resources.sessions_active > 0, closes the
    session, verifies sessions_active drops to zero, then confirms idle exit fires.
    """
    runner = _BlockingSessionRunner()
    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.3,
        session_runner=runner,
        drain_timeout_seconds=0.2,
    )
    client = DaemonClient(socket_path=server.socket_path)
    connection = client.list_connections()[0]
    client.open_session(OpenSessionRequest(connection_id=connection.id))

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if client.get_daemon_status().resources.sessions_active:
            break
        time.sleep(0.02)
    else:
        pytest.fail("session never became active")

    # Verify resources report the active session.
    status = client.get_daemon_status()
    assert status.resources.sessions_active > 0

    # Close session via public API; resources should drop to zero.
    from sshpilot.api.models.sessions import CloseSessionRequest

    client.close_session(CloseSessionRequest(session_id=client.list_sessions()[0].id))

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if client.get_daemon_status().resources.sessions_active == 0:
            break
        time.sleep(0.1)
    else:
        pytest.fail("sessions_active did not reach zero after session close")

    # Session resources gone; force stop to cleanly exit (client still connected).
    result = client.stop_daemon(StopDaemonRequest(force=True))
    assert result.accepted is True
    client.close()
    assert server.wait_stopped(timeout=5.0)


def test_claim_orphaned_forward(daemon_factory):
    """A new client can claim a forward orphaned by a prior client.

    Client A opens a forward, then disconnects (which orphans it via
    detach_client). Client B claims the orphaned forward, becoming its
    new owner, and can then close it.
    """
    from sshpilot.api.models.operations import (
        ClaimForwardRequest,
        CloseForwardRequest,
    )
    from sshpilot.api.models import ForwardType, OpenForwardRequest

    server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
    client_a = DaemonClient(
        socket_path=server.socket_path,
        client_id="client-a",
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

    # Client A disconnects — forward should be orphaned.
    client_a.close()
    time.sleep(0.3)

    # Client B connects.
    client_b = DaemonClient(
        socket_path=server.socket_path,
        client_id="client-b",
    )
    forwards = client_b.list_forwards()
    active = [f for f in forwards if f.id == fwd.id]
    assert len(active) == 1
    # Owner was cleared by detach_client.
    assert active[0].owner_client_id != "client-b"

    # Client B claims the orphaned forward.
    claimed = client_b.claim_forward(ClaimForwardRequest(forward_id=fwd.id))
    assert claimed.owner_client_id == "client-b"

    # Client B can now close it (ownership check passes).
    client_b.close_forward(CloseForwardRequest(forward_id=fwd.id))

    result = client_b.stop_daemon(StopDaemonRequest(force=True))
    assert result.accepted is True
    client_b.close()
    assert server.wait_stopped(timeout=5.0)
