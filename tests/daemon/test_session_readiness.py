"""SessionRuntime readiness: OpenSSH diagnostics primary, PTY evidence fallback."""

from __future__ import annotations

import threading

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    OpenSessionRequest,
    SessionExitInfo,
    SessionState,
)
from sshpilot.api.models.terminal import TerminalDimensions
from sshpilot.core.ssh_diagnostics import SshDiagnosticResult, SshDiagnosticState
from sshpilot.daemon.session_runtime import SessionRuntime
from tests.helpers.fake_connection_repository import make_test_repository


class _Handle:
    def __init__(self, on_exit, on_output, on_eof):
        self._on_exit = on_exit
        self._on_output = on_output
        self._on_eof = on_eof
        self.writes: list[bytes] = []
        self._exit_info = None
        self.terminated = False
        self.killed = False

    def write(self, data: bytes) -> bool:
        self.writes.append(data)
        return True

    def resize(self, dimensions: TerminalDimensions) -> None:
        return None

    def terminate(self):
        self.terminated = True
        self.exit(SessionExitInfo(exit_code=0, reason="terminated"))

    def kill(self):
        self.killed = True
        self.exit(SessionExitInfo(signal=9, reason="killed"))

    def wait(self, timeout):
        return self._exit_info

    def poll(self):
        return None if self._exit_info is None else 0

    def exit(self, info):
        if self._exit_info is not None:
            return
        self._exit_info = info
        self._on_exit(info)


class _EvidenceTerminalRunner:
    terminal_capable = True

    def __init__(self):
        self.started = threading.Event()
        self.handle: _Handle | None = None

    def start(self, spec, on_exit, on_output=None, on_eof=None):
        del spec
        self.handle = _Handle(on_exit, on_output, on_eof)
        self.started.set()
        return self.handle

    def emit(self, data: bytes):
        assert self.handle is not None
        self.handle._on_output(data)

    def close(self):
        return None


class _FakeReadiness:
    """Stub of the SshReadinessManager contract for runtime tests."""

    def __init__(self, engaged=True, immediate_result=None):
        self._engaged = engaged
        self._immediate_result = immediate_result
        self.subscribed: list[str] = []
        self.finished: list[str] = []
        self.result_cb = None
        self.grace_cb = None

    def subscribe(self, session_id, on_result, on_grace_expired=None):
        self.subscribed.append(session_id)
        self.result_cb = on_result
        self.grace_cb = on_grace_expired
        if self._immediate_result is not None:
            on_result(session_id, self._immediate_result)

    def is_engaged(self, session_id):
        return self._engaged

    def finish(self, session_id):
        if session_id not in self.finished:
            self.finished.append(session_id)

    def deliver(self, session_id, result):
        assert self.result_cb is not None
        self.result_cb(session_id, result)

    def fire_grace(self, session_id):
        assert self.grace_cb is not None
        self.grace_cb(session_id)


def _prepared(core, runtime, client_id="client:a"):
    return runtime.prepare_open_session(
        OpenSessionRequest(
            connection_id=core.list_connections()[0].id,
            dimensions=TerminalDimensions(rows=24, columns=80),
        ),
        client_id=ClientId(client_id),
    )


def _attach(runtime, session_id, client_id="client:a"):
    return runtime.attach_session(
        AttachSessionRequest(
            session_id=session_id,
            request_input=True,
            want_terminal_output=True,
        ),
        client_id=ClientId(client_id),
    )


def test_diagnostic_gate_defers_running_until_authenticated_marker():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-gate")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    runtime.enable_connection_evidence_gate()
    authenticated: list[str] = []
    runtime.set_authenticated_callback(lambda sid: authenticated.append(sid))
    outputs: list[bytes] = []
    runtime.subscribe_terminal(lambda item: outputs.append(item.data))

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    assert readiness.subscribed == [prepared.id]

    # Connected-looking PTY evidence must NOT promote a diagnostics session.
    runner.emit(b"\r\n\x1b[32malice@host\x1b[0m:\x1b[34m~\x1b[0m$ ")
    assert runtime.get_session(prepared.id).state is SessionState.STARTING

    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.AUTHENTICATED,
            'debug1: Authenticated to example.test ([127.0.0.1]:22) using "publickey".',
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert authenticated == [prepared.id]
    assert readiness.finished == [prepared.id]

    runtime.shutdown()
    core.close()


def test_diagnostic_marker_before_handle_is_stored_still_promotes():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-early")
    runner = _EvidenceTerminalRunner()
    marker = SshDiagnosticResult(
        SshDiagnosticState.AUTHENTICATED,
        'debug1: Authenticated to example.test ([127.0.0.1]:22) using "password".',
    )
    readiness = _FakeReadiness(engaged=True, immediate_result=marker)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING

    runtime.shutdown()
    core.close()


def test_diagnostic_mux_marker_promotes():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-mux")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    assert runtime.get_session(prepared.id).state is SessionState.STARTING

    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.MUX_SESSION_OPENED,
            "debug1: mux_client_request_session: master session id: 0",
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING

    runtime.shutdown()
    core.close()


def test_diagnostic_failure_fails_session_with_trusted_detail():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-fail")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    states: list[str] = []
    runtime.subscribe_events(
        lambda event: states.append(getattr(event.payload, "state", None))
    )

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.FAILED,
            "alice@example.test: Permission denied (publickey).",
        ),
    )
    failed = runtime.get_session(prepared.id)
    assert failed.state is SessionState.FAILED
    assert failed.failure is not None
    assert failed.failure.message == "alice@example.test: Permission denied (publickey)."
    assert SessionState.RUNNING not in states

    runtime.shutdown()
    core.close()


def test_diagnostic_failure_while_running_does_not_tear_session_down():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-latefail")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.AUTHENTICATED,
            'debug1: Authenticated to example.test using "publickey".',
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.FAILED,
            "alice@example.test: Connection closed by 127.0.0.1.",
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING

    runtime.shutdown()
    core.close()


def test_grace_expiry_falls_back_to_pty_evidence():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-grace")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    runtime.enable_connection_evidence_gate()

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    assert runtime.get_session(prepared.id).state is SessionState.STARTING

    readiness.fire_grace(prepared.id)
    assert readiness.finished == [prepared.id]
    assert runtime.get_session(prepared.id).state is SessionState.STARTING

    runner.emit(b"\r\n\x1b[32malice@host\x1b[0m:\x1b[34m~\x1b[0m$ ")
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING

    runtime.shutdown()
    core.close()


def test_grace_expiry_promotes_on_buffered_connected_evidence():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-grace2")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    runtime.enable_connection_evidence_gate()

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    runner.emit(b"\x1b[32muser@host\x1b[0m:~$ ")

    readiness.fire_grace(prepared.id)
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING

    runtime.shutdown()
    core.close()


def test_grace_expiry_does_not_promote_without_evidence():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-grace3")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    runtime.enable_connection_evidence_gate()

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    runner.emit(b"alice@example.test's password: ")
    readiness.fire_grace(prepared.id)
    assert runtime.get_session(prepared.id).state is SessionState.STARTING
    assert runner.handle is not None
    assert runtime.get_session(prepared.id).failure is None

    runtime.shutdown()
    core.close()


def test_not_engaged_session_promotes_immediately():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-unengaged")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=False)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert readiness.finished == []

    runtime.shutdown()
    core.close()


def test_diagnostic_failure_code_preserves_interaction_cancellation():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-cancel")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)

    def _classify(_session_id):
        return ErrorCode.OPERATION_CANCELLED

    runtime._auth_failure_classifier = _classify

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.FAILED,
            "alice@example.test: Permission denied (publickey).",
        ),
    )
    failed = runtime.get_session(prepared.id)
    assert failed.failure is not None
    assert failed.failure.code == ErrorCode.OPERATION_CANCELLED.value

    runtime.shutdown()
    core.close()


def test_readiness_finished_on_clean_exit_and_terminal_close():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-exit")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.AUTHENTICATED,
            'debug1: Authenticated to example.test using "publickey".',
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert readiness.finished == [prepared.id]

    assert runner.handle is not None
    runner.handle.exit(SessionExitInfo(exit_code=0, reason="process_exit"))
    runner.handle._on_eof()
    assert runtime.get_session(prepared.id).state is SessionState.CLOSED
    # finish is idempotent; no further finished entries.
    assert readiness.finished == [prepared.id]

    runtime.shutdown()
    core.close()


def test_exit_before_authentication_does_not_claim_running():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-earlyexit")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    states: list[str] = []
    runtime.subscribe_events(
        lambda event: states.append(getattr(event.payload, "state", None))
    )

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    assert runner.handle is not None
    runner.handle.exit(SessionExitInfo(exit_code=255, reason="process_exit"))
    runner.handle._on_eof()
    assert SessionState.RUNNING not in states
    assert readiness.finished == [prepared.id]
    assert runtime.get_session(prepared.id).state is SessionState.CLOSED

    runtime.shutdown()
    core.close()
