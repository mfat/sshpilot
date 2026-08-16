"""SessionRuntime readiness: OpenSSH diagnostics primary, PTY evidence fallback."""

from __future__ import annotations

import threading

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    CloseSessionRequest,
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
    """Real-contract stub of the SshReadinessManager for runtime tests.

    ``finish()`` releases the session's engagement exactly like the real
    manager (which pops its lease record), so an early decisive result that
    parks on the runtime must not have finished the lease yet — the handoff
    race is surfaced instead of masked.
    """

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
        # Mirror the real manager: finishing a lease releases engagement.
        self._engaged = False

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
    # Grace expiry keeps the diagnostics lease armed so a late verdict can
    # still be applied; only promotion/failure/exit/close finish it.
    assert readiness.finished == []
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


def test_exit_before_authentication_with_zero_output_and_clean_code_still_fails():
    """A session that exits from STARTING with exit_code=0 but *no PTY output
    at all* must not be silently treated as an expected/clean close — it has
    never proven it did anything useful. This is the exact shape of GH #1166
    (frozen macOS build: ssh never actually launched, so the session exited
    "cleanly" a moment later with zero output and no diagnostic verdict)."""
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-silent-exit")
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
    # No runner.emit(...) call: the session never produces any terminal
    # output before exiting, unlike a genuinely clean "user typed exit".
    runner.handle.exit(SessionExitInfo(exit_code=0, reason="process_exit"))
    runner.handle._on_eof()

    assert SessionState.RUNNING not in states
    closed = runtime.get_session(prepared.id)
    assert closed.state is SessionState.CLOSED
    assert closed.failure is not None
    assert closed.failure.message == "The session ended before it produced any output"

    runtime.shutdown()
    core.close()


def test_gated_session_without_result_stays_alive_until_marker():
    """A diagnostics-gated session must not have its process reaped while the
    decisive verdict is still outstanding (regression for the over-eager
    terminate-after-start cleanup)."""
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-keepalive")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    assert runner.handle is not None
    assert not runner.handle.terminated
    assert runtime.get_session(prepared.id).state is SessionState.STARTING

    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.AUTHENTICATED,
            'debug1: Authenticated to example.test using "publickey".',
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert not runner.handle.terminated

    runtime.shutdown()
    core.close()


def test_parked_authenticated_marker_applies_atomically_when_handle_stored():
    """A marker that arrives before the process handle is stored is parked on
    the record with the lease still engaged, then applied by start_session:
    RUNNING exactly once, lease finished exactly once after."""
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-park-ok")
    runner = _EvidenceTerminalRunner()
    marker = SshDiagnosticResult(
        SshDiagnosticState.AUTHENTICATED,
        'debug1: Authenticated to example.test using "publickey".',
    )
    readiness = _FakeReadiness(engaged=True, immediate_result=marker)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert readiness.finished == [prepared.id]
    assert not readiness.is_engaged(prepared.id)

    runtime.shutdown()
    core.close()


def test_parked_failure_applies_atomically_and_terminates_handle():
    """A FAILED marker that arrives before the process handle is stored is
    parked, then applied by start_session: the session fails (never RUNNING),
    the lease finishes, and the freshly spawned process is reaped."""
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-park-fail")
    runner = _EvidenceTerminalRunner()
    failed = SshDiagnosticResult(
        SshDiagnosticState.FAILED,
        "alice@example.test: Permission denied (publickey).",
    )
    readiness = _FakeReadiness(engaged=True, immediate_result=failed)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    states: list = []
    runtime.subscribe_events(
        lambda event: states.append(getattr(event.payload, "state", None))
    )

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    assert runner.handle is not None
    assert runtime.get_session(prepared.id).state is SessionState.FAILED
    assert SessionState.RUNNING not in states
    assert readiness.finished == [prepared.id]
    assert not readiness.is_engaged(prepared.id)
    assert runner.handle.terminated

    runtime.shutdown()
    core.close()


def test_grace_after_promotion_is_a_noop():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-grace-late")
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
    readiness.fire_grace(prepared.id)
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING

    runtime.shutdown()
    core.close()


def test_late_success_after_grace_promotes_still_starting_session():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-late-ok")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    readiness.fire_grace(prepared.id)
    assert runtime.get_session(prepared.id).state is SessionState.STARTING

    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.AUTHENTICATED,
            'debug1: Authenticated to example.test using "publickey".',
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING

    runtime.shutdown()
    core.close()


def test_late_failure_after_grace_fails_still_starting_session():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-late-fail")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    states: list = []
    runtime.subscribe_events(
        lambda event: states.append(getattr(event.payload, "state", None))
    )

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    readiness.fire_grace(prepared.id)
    assert runtime.get_session(prepared.id).state is SessionState.STARTING

    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.FAILED,
            "alice@example.test: Connection closed by 127.0.0.1.",
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.FAILED
    assert SessionState.RUNNING not in states
    assert readiness.finished == [prepared.id]

    runtime.shutdown()
    core.close()


def test_late_diagnostics_after_pty_fallback_promotion_are_idempotent():
    """Once the PTY fallback promotes a session, a late decisive marker must
    neither promote again nor regress RUNNING to FAILED, and the authenticated
    callback fires exactly once."""
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-late-pty")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    runtime.enable_connection_evidence_gate()
    authenticated: list = []
    runtime.set_authenticated_callback(lambda sid: authenticated.append(sid))

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    runner.emit(b"\r\n\x1b[32malice@host\x1b[0m:\x1b[34m~\x1b[0m$ ")
    readiness.fire_grace(prepared.id)
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert authenticated == [prepared.id]

    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.AUTHENTICATED,
            'debug1: Authenticated to host ([::1]:22) using "publickey".',
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert authenticated == [prepared.id]
    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.FAILED,
            "alice@host: Connection closed by 127.0.0.1.",
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert authenticated == [prepared.id]

    runtime.shutdown()
    core.close()


def test_close_while_diagnostics_pending_finishes_lease():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-close-pending")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    assert runtime.get_session(prepared.id).state is SessionState.STARTING

    runtime.close_session(CloseSessionRequest(session_id=prepared.id))
    assert readiness.finished == [prepared.id]
    # A late verdict after close must not resurrect the session.
    readiness.deliver(
        prepared.id,
        SshDiagnosticResult(
            SshDiagnosticState.AUTHENTICATED,
            'debug1: Authenticated to example.test using "publickey".',
        ),
    )
    assert runtime.get_session(prepared.id).state is SessionState.CLOSING

    runtime.shutdown()
    core.close()


def test_exit_before_authentication_with_failure_evidence_fails_and_finishes_lease():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-exit-fail")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    states: list = []
    runtime.subscribe_events(
        lambda event: states.append(getattr(event.payload, "state", None))
    )

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    runner.emit(b"alice@example.test: Permission denied (publickey).\r\n")
    assert runner.handle is not None
    runner.handle.exit(SessionExitInfo(exit_code=255, reason="process_exit"))
    runner.handle._on_eof()
    assert SessionState.RUNNING not in states
    assert SessionState.FAILED in states
    assert readiness.finished == [prepared.id]

    runtime.shutdown()
    core.close()


def test_success_delivered_twice_promotes_and_notifies_exactly_once():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="diag-twice")
    runner = _EvidenceTerminalRunner()
    readiness = _FakeReadiness(engaged=True)
    runtime = SessionRuntime(core, runner=runner, readiness_manager=readiness)
    authenticated: list = []
    runtime.set_authenticated_callback(lambda sid: authenticated.append(sid))
    running_events: list = []
    runtime.subscribe_events(
        lambda event: running_events.append(
            getattr(event.payload, "state", None)
        )
    )

    prepared = _prepared(core, runtime)
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    marker = SshDiagnosticResult(
        SshDiagnosticState.AUTHENTICATED,
        'debug1: Authenticated to example.test using "publickey".',
    )
    readiness.deliver(prepared.id, marker)
    readiness.deliver(prepared.id, marker)
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert authenticated == [prepared.id]
    assert running_events.count(SessionState.RUNNING) == 1

    runtime.shutdown()
    core.close()
