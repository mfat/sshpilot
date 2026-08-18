"""Regression coverage for terminate-all / quit lifecycle bugs.

1. Terminate everything must clear orphaned SFTP (via force stop, not
   per-client close_sftp ownership).
2. File-manager directory-count callbacks must not raise after teardown.
3. Terminate everything must not call cleanup_and_quit when termination failed.
"""

from __future__ import annotations

import types
from unittest.mock import Mock

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId, SftpServiceId
from sshpilot.api.models.daemon import (
    DaemonLifecycleState,
    DaemonResourceCounts,
    DaemonStopResult,
    StopDaemonRequest,
)
from sshpilot.api.models.operations import (
    CloseSftpRequest,
    OpenSftpRequest,
    SftpServiceState,
)
from sshpilot.daemon.sftp_runtime import SftpServiceRuntime
from sshpilot.daemon_sftp_backend import DaemonSftpManager
from sshpilot.file_manager.common import FileEntry
from sshpilot.sftp_service_controller import (
    DaemonSftpServiceController,
    SftpControllerState,
    required_daemon_sftp_capabilities,
)


# ---------------------------------------------------------------------------
# Shared fake SFTP runner (keeps a service READY without real SSH)
# ---------------------------------------------------------------------------


class _Connection:
    def __init__(self):
        self.id = ConnectionId("demo")
        self.protocol = "ssh"
        self.hostname = "example.test"
        self.username = "alice"
        self.port = 22


class _CoreClient:
    def __init__(self):
        self.connection = _Connection()

    def get_connection(self, _connection_id):
        return self.connection


class _FakeSftpClient:
    def close(self):
        return None


class _FakeSftpHandle:
    def __init__(self):
        self.client = _FakeSftpClient()
        self.terminated = 0

    def terminate(self):
        self.terminated += 1
        self.client.close()

    def wait(self, timeout):
        del timeout
        return True


class _FakeSftpRunner:
    def start(self, _spec):
        return _FakeSftpHandle()

    def close(self):
        return None


def _ready_orphaned_service():
    """READY SFTP owned by app-a, then orphaned via detach_client."""
    runtime = SftpServiceRuntime(_CoreClient(), runner=_FakeSftpRunner())
    owner_a = ClientId("app-a")
    summary = runtime.prepare_open_service(
        OpenSftpRequest(connection_id=ConnectionId("demo")),
        client_id=owner_a,
    )
    runtime.start_service(summary.id)
    assert runtime.get_service(summary.id).state is SftpServiceState.READY
    runtime.detach_client(owner_a)
    assert runtime.get_service(summary.id).owner_client_id is None
    return runtime, summary.id


class _ClientOverRuntime:
    """Minimal DaemonClient surface for ``terminate_all_daemon_work``.

    ``stop_daemon(force=True)`` mirrors real daemon shutdown: tear down SFTP
    without per-client ownership checks.
    """

    def __init__(self, runtime: SftpServiceRuntime, client_id: ClientId):
        self._runtime = runtime
        self._client_id = client_id

    def list_sessions(self):
        return []

    def list_transfers(self):
        return []

    def list_forwards(self):
        return []

    def list_sftp_services(self):
        return list(self._runtime.list_services())

    def close_sftp(self, request: CloseSftpRequest) -> None:
        if not self._runtime.prepare_close_service(
            request, client_id=self._client_id
        ):
            return
        self._runtime.finish_close_service(request.service_id)

    def stop_daemon(self, request: StopDaemonRequest | None = None) -> DaemonStopResult:
        force = bool(getattr(request, "force", False)) if request else False
        if not force:
            return DaemonStopResult(
                accepted=False,
                state=DaemonLifecycleState.RUNNING,
                resources=DaemonResourceCounts(sftp_active=1),
                will_lose=("sftp",),
                confirmation="token",
                message="retry with force=true",
            )
        self._runtime.shutdown()
        return DaemonStopResult(
            accepted=True,
            state=DaemonLifecycleState.DRAINING,
            resources=DaemonResourceCounts(),
            will_lose=("sftp",),
            message="forced stop",
        )


# ---------------------------------------------------------------------------
# Issue 1 — terminate-all must clear orphaned SFTP services
# ---------------------------------------------------------------------------


def test_issue1_second_client_cannot_close_orphaned_sftp():
    """Premise: after app-a disconnects, app-b's close_sftp is ownership-blocked."""
    runtime, service_id = _ready_orphaned_service()
    owner_b = ClientId("app-b")

    with pytest.raises(SshPilotError) as raised:
        runtime.prepare_close_service(
            CloseSftpRequest(service_id=service_id),
            client_id=owner_b,
        )
    assert raised.value.code is ErrorCode.SERVICE_OWNER_REQUIRED
    assert "originating client" in raised.value.message
    assert runtime.get_service(service_id).state is SftpServiceState.READY


def test_issue1_terminate_all_clears_orphaned_sftp_via_force_stop():
    """Terminate everything uses force stop so orphans are torn down."""
    from sshpilot.daemon_quit_policy import terminate_all_daemon_work

    runtime, _service_id = _ready_orphaned_service()
    client_b = _ClientOverRuntime(runtime, ClientId("app-b"))

    errors = terminate_all_daemon_work(client_b)
    assert errors == []

    # Force stop shuts down the SFTP runtime (same as daemon.stop force).
    with pytest.raises(SshPilotError) as raised:
        runtime.list_services()
    assert raised.value.code is ErrorCode.DAEMON_SHUTTING_DOWN


def test_issue1_per_resource_fallback_still_blocked_without_stop_daemon():
    """Without stop_daemon, bare close_sftp cannot clear an orphan (old path)."""
    from sshpilot.daemon_quit_policy import _terminate_all_per_resource

    runtime, service_id = _ready_orphaned_service()
    owned = _ClientOverRuntime(runtime, ClientId("app-b"))

    class _NoStopClient:
        def list_sessions(self):
            return owned.list_sessions()

        def list_transfers(self):
            return owned.list_transfers()

        def list_forwards(self):
            return owned.list_forwards()

        def list_sftp_services(self):
            return owned.list_sftp_services()

        def close_sftp(self, request):
            return owned.close_sftp(request)

    errors = _terminate_all_per_resource(_NoStopClient())
    assert any("close_sftp" in message for message in errors)
    assert runtime.get_service(service_id).state is SftpServiceState.READY


def test_issue1_terminate_all_force_stops_live_daemon_with_orphan(daemon_factory):
    """End-to-end: App B Terminate everything force-stops a daemon holding App A's SFTP."""
    import time

    from sshpilot.api import DaemonClient
    from sshpilot.daemon_quit_policy import terminate_all_daemon_work

    server, _manager = daemon_factory(idle_shutdown_seconds=5.0)
    client_a = DaemonClient(socket_path=server.socket_path, client_id="app-a")
    connection = client_a.list_connections()[0]
    client_a.open_sftp(OpenSftpRequest(connection_id=connection.id))
    client_a.close()
    time.sleep(0.3)

    client_b = DaemonClient(socket_path=server.socket_path, client_id="app-b")
    assert client_b.list_sftp_services(), "expected orphaned SFTP from app-a"
    errors = terminate_all_daemon_work(client_b)
    assert errors == []
    try:
        client_b.close()
    except Exception:
        pass
    assert server.wait_stopped(timeout=5.0)


# ---------------------------------------------------------------------------
# Issue 2 — file-manager callbacks continue during shutdown
# ---------------------------------------------------------------------------


def test_issue2_count_pass_callback_must_not_raise_after_teardown():
    """Directory-count success must not raise when chaining after detach/close."""
    client = Mock()
    caps = Mock()
    caps.supported = required_daemon_sftp_capabilities()
    client.get_capabilities.return_value = caps
    bridge = Mock()

    controller = DaemonSftpServiceController(
        client=client,
        bridge=bridge,
        connection_id=ConnectionId("conn-1"),
    )
    with controller._lock:
        controller._state = SftpControllerState.READY
        controller._service_id = SftpServiceId("svc-1")

    fake = types.SimpleNamespace(
        _closed=False,
        _sftp_controller=controller,
        emit=Mock(),
    )
    folders = [
        FileEntry(name="a", is_dir=True, size=0, modified=0),
        FileEntry(name="b", is_dir=True, size=0, modified=0),
    ]

    def _list_then_teardown(path, *, on_success, on_error, cursor=None, limit=None):
        class _Result:
            entries = (object(), object())

        fake._closed = True
        controller.detach()
        on_success(_Result())

    controller.list_directory = _list_then_teardown  # type: ignore[method-assign]

    DaemonSftpManager._start_count_pass(fake, "/home/user", folders)

    assert fake._closed is True
    assert controller.state is SftpControllerState.DETACHED


def test_issue2_controller_list_directory_does_not_raise_when_not_ready():
    """Callback APIs must deliver not-ready via on_error, never raise."""
    client = Mock()
    caps = Mock()
    caps.supported = required_daemon_sftp_capabilities()
    client.get_capabilities.return_value = caps
    bridge = Mock()
    controller = DaemonSftpServiceController(
        client=client,
        bridge=bridge,
        connection_id=ConnectionId("conn-1"),
    )
    errors: list[BaseException] = []
    controller.list_directory(
        "/tmp",
        on_success=lambda _r: pytest.fail("should not succeed"),
        on_error=errors.append,
    )
    assert len(errors) == 1
    assert isinstance(errors[0], SshPilotError)
    assert errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY
    bridge.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Issue 3 — shutdown must not continue despite terminate-all failure
# ---------------------------------------------------------------------------


def _sync_terminate_dispatch(monkeypatch, quit_policy):
    """Run terminate-all worker + UI finish synchronously in unit tests."""
    monkeypatch.setattr(quit_policy, "_run_in_background", lambda fn: fn())
    monkeypatch.setattr(quit_policy, "_invoke_on_main", lambda fn: fn())
    monkeypatch.setattr(
        quit_policy,
        "wait_for_daemon_termination",
        lambda **_kwargs: [],
    )


def test_issue3_apply_terminate_all_escalates_when_stop_is_refused(monkeypatch):
    """A daemon that refuses the stop RPC is killed, then verified gone.

    Escalation is unconditional — quit is not a request the background service
    may decline — but the *exit* is not: it happens only because verification
    afterwards finds nothing owned still running.
    """
    import sshpilot.daemon_quit_policy as quit_policy
    import sshpilot.shutdown as shutdown_mod

    _sync_terminate_dispatch(monkeypatch, quit_policy)
    monkeypatch.setattr(
        quit_policy,
        "terminate_all_daemon_work",
        lambda _client: [
            "stop_daemon force: Only the originating client may mutate this SFTP service"
        ],
    )
    forced: list[object] = []
    monkeypatch.setattr(
        quit_policy,
        "force_daemon_exit",
        lambda socket_path: forced.append(socket_path) or [],
    )
    monkeypatch.setattr(quit_policy, "verify_quit_teardown", lambda *_a, **_k: [])

    quit_calls: list[object] = []
    monkeypatch.setattr(
        shutdown_mod,
        "cleanup_and_quit",
        lambda window: quit_calls.append(window),
    )

    class _Window:
        client = object()
        _daemon_quit_decision = None

        def get_application(self):
            return None

    window = _Window()
    quit_policy.apply_terminate_all(window)

    assert forced, "a refused stop must escalate to forcing the daemon out"
    assert quit_calls == [window]


def test_issue3_apply_terminate_all_quits_when_termination_succeeds(monkeypatch):
    """Successful Terminate everything proceeds to cleanup_and_quit."""
    import sshpilot.daemon_quit_policy as quit_policy
    import sshpilot.shutdown as shutdown_mod

    _sync_terminate_dispatch(monkeypatch, quit_policy)
    monkeypatch.setattr(
        quit_policy,
        "terminate_all_daemon_work",
        lambda _client: [],
    )
    monkeypatch.setattr(quit_policy, "verify_quit_teardown", lambda *_a, **_k: [])
    quit_calls: list[object] = []
    monkeypatch.setattr(
        shutdown_mod,
        "cleanup_and_quit",
        lambda window: quit_calls.append(window),
    )

    class _Window:
        client = object()

        def get_application(self):
            return None

    window = _Window()
    quit_policy.apply_terminate_all(window)
    assert quit_calls == [window]


def test_issue3_apply_terminate_all_escalates_when_daemon_outlives_its_stop(monkeypatch):
    """An accepted stop that never completes still ends in a dead daemon.

    Quit is gated on confirmed exit, not on the stop RPC being accepted — but
    a daemon that accepts and then lingers is signalled rather than allowed to
    keep the application open.
    """
    import sshpilot.daemon_quit_policy as quit_policy
    import sshpilot.shutdown as shutdown_mod

    monkeypatch.setattr(quit_policy, "_run_in_background", lambda fn: fn())
    monkeypatch.setattr(quit_policy, "_invoke_on_main", lambda fn: fn())
    monkeypatch.setattr(quit_policy, "terminate_all_daemon_work", lambda _c: [])
    monkeypatch.setattr(
        quit_policy,
        "wait_for_daemon_termination",
        lambda **_kwargs: ["daemon did not finish shutting down: socket still present"],
    )

    quit_calls: list[object] = []
    forced: list[object] = []
    monkeypatch.setattr(
        shutdown_mod,
        "cleanup_and_quit",
        lambda window: quit_calls.append(window),
    )
    monkeypatch.setattr(
        quit_policy,
        "force_daemon_exit",
        lambda socket_path: forced.append(socket_path) or [],
    )
    monkeypatch.setattr(quit_policy, "verify_quit_teardown", lambda *_a, **_k: [])

    class _Window:
        client = object()
        _daemon_quit_decision = "terminate_all"

        def get_application(self):
            return None

    window = _Window()
    quit_policy.apply_terminate_all(window)
    assert forced, "a daemon that outlives its own stop must be signalled"
    assert quit_calls == [window]


def test_wait_for_daemon_termination_requires_process_and_socket(tmp_path):
    """Confirmed termination needs process exit and socket removal when both known."""
    from sshpilot.daemon_quit_policy import wait_for_daemon_termination

    sock = tmp_path / "sshpilotd.sock"
    sock.write_text("")

    class _Proc:
        def __init__(self):
            self._code = None

        def poll(self):
            return self._code

    proc = _Proc()
    handle = types.SimpleNamespace(process=proc)

    # Still running + socket present → timeout error.
    errors = wait_for_daemon_termination(
        daemon_process=handle,
        socket_path=sock,
        timeout=0.15,
        poll_interval=0.05,
    )
    assert errors and "still" in errors[0]

    # Process exits but socket remains → still an error.
    proc._code = 0
    errors = wait_for_daemon_termination(
        daemon_process=handle,
        socket_path=sock,
        timeout=0.15,
        poll_interval=0.05,
    )
    assert errors and "socket" in errors[0]

    # Both gone → success.
    sock.unlink()
    assert (
        wait_for_daemon_termination(
            daemon_process=handle,
            socket_path=sock,
            timeout=0.5,
            poll_interval=0.05,
        )
        == []
    )


def test_terminate_refuses_daemon_control_without_stop_daemon():
    """Production clients advertising DAEMON_CONTROL must expose stop_daemon."""
    from sshpilot.api.capabilities import Capability
    from sshpilot.daemon_quit_policy import terminate_all_daemon_work

    caps = types.SimpleNamespace(supported=frozenset({Capability.DAEMON_CONTROL}))
    client = types.SimpleNamespace(
        get_capabilities=lambda: caps,
        # no stop_daemon
        list_sessions=list,
        list_sftp_services=list,
        list_transfers=list,
        list_forwards=list,
    )
    errors = terminate_all_daemon_work(client)
    assert errors and "DAEMON_CONTROL" in errors[0]


# ---------------------------------------------------------------------------
# Exit is gated on verification, not on teardown having been attempted.
# ---------------------------------------------------------------------------


def _gated_window():
    class _Window:
        client = object()
        _daemon_quit_decision = None
        _daemon_quit_close_policy = None
        _daemon_shutdown_intent = None

        def get_application(self):
            return None

    return _Window()


def test_surviving_daemon_prevents_the_gui_from_exiting(monkeypatch):
    """Required: eviction failure must not end in a 'successful' quit.

    Exiting here would strand a running daemon with no owner left to reap it,
    which is exactly the state the next launch has to fight.
    """
    import sshpilot.daemon_quit_policy as quit_policy
    import sshpilot.shutdown as shutdown_mod

    _sync_terminate_dispatch(monkeypatch, quit_policy)
    monkeypatch.setattr(quit_policy, "terminate_all_daemon_work", lambda _c: [])
    monkeypatch.setattr(quit_policy, "force_daemon_exit", lambda _p: ["stuck"])
    monkeypatch.setattr(
        quit_policy,
        "verify_quit_teardown",
        lambda *_a, **_k: ["the background service still owns its socket (pid=99)"],
    )

    quit_calls = []
    presented = []
    monkeypatch.setattr(
        shutdown_mod, "cleanup_and_quit", lambda window: quit_calls.append(window)
    )
    monkeypatch.setattr(
        quit_policy,
        "_present_incomplete_teardown",
        lambda window, survivors: presented.append(list(survivors)),
    )

    window = _gated_window()
    quit_policy.apply_terminate_all(window)

    assert quit_calls == [], "quit must not complete while something owned survives"
    assert presented and "still owns its socket" in presented[0][0]
    # The window is usable again rather than stuck mid-quit.
    assert window._daemon_quit_decision is None
    assert window._daemon_shutdown_intent is None


def test_surviving_tracked_child_prevents_the_gui_from_exiting(monkeypatch):
    """Required: a live owned SSH child blocks exit even with the daemon gone."""
    import sshpilot.daemon_quit_policy as quit_policy
    import sshpilot.shutdown as shutdown_mod

    _sync_terminate_dispatch(monkeypatch, quit_policy)
    monkeypatch.setattr(quit_policy, "terminate_all_daemon_work", lambda _c: [])
    monkeypatch.setattr(
        quit_policy,
        "verify_quit_teardown",
        lambda *_a, **_k: ["a remote session process is still running (pid=4242)"],
    )
    quit_calls = []
    monkeypatch.setattr(
        shutdown_mod, "cleanup_and_quit", lambda window: quit_calls.append(window)
    )
    monkeypatch.setattr(
        quit_policy, "_present_incomplete_teardown", lambda *_a: None
    )

    quit_policy.apply_terminate_all(_gated_window())
    assert quit_calls == []


def test_verified_teardown_allows_the_gui_to_exit(monkeypatch):
    """Required: everything owned confirmed dead permits the quit."""
    import sshpilot.daemon_quit_policy as quit_policy
    import sshpilot.shutdown as shutdown_mod

    _sync_terminate_dispatch(monkeypatch, quit_policy)
    monkeypatch.setattr(quit_policy, "terminate_all_daemon_work", lambda _c: [])
    monkeypatch.setattr(quit_policy, "verify_quit_teardown", lambda *_a, **_k: [])
    quit_calls = []
    monkeypatch.setattr(
        shutdown_mod, "cleanup_and_quit", lambda window: quit_calls.append(window)
    )

    window = _gated_window()
    quit_policy.apply_terminate_all(window)
    assert quit_calls == [window]


def test_a_verification_that_cannot_run_blocks_the_quit(monkeypatch):
    """An unprovable shutdown is not a proven one."""
    import sshpilot.daemon_quit_policy as quit_policy
    import sshpilot.daemon.runtime_verification as verification

    def _explode(**_kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(
        verification, "verify_sshpilot_runtime_terminated", _explode
    )
    survivors = quit_policy.verify_quit_teardown("/nonexistent/sshpilotd.sock")
    assert survivors, "a verification failure must read as 'not verified'"
