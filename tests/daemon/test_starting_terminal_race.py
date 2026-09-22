"""SessionRuntime: defer live output / drop input until RUNNING."""

from __future__ import annotations

import threading


from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    OpenSessionRequest,
    SessionExitInfo,
    SessionState,
)
from sshpilot.api.models.terminal import TerminalDimensions, TerminalInput
from sshpilot.daemon.session_runtime import SessionRuntime
from tests.helpers.fake_connection_repository import make_test_repository


class _Handle:
    def __init__(self, on_exit, on_output, on_eof):
        self._on_exit = on_exit
        self._on_output = on_output
        self._on_eof = on_eof
        self.writes: list[bytes] = []
        self.last_dimensions = None
        self._exit_info = None

    def write(self, data: bytes) -> bool:
        self.writes.append(data)
        return True

    def resize(self, dimensions: TerminalDimensions) -> None:
        self.last_dimensions = dimensions

    def terminate(self):
        self.exit(SessionExitInfo(exit_code=0, reason="terminated"))

    def kill(self):
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


class _GatedTerminalRunner:
    terminal_capable = True

    def __init__(self):
        self.start_entered = threading.Event()
        self.release_auth = threading.Event()
        self.handle: _Handle | None = None
        self.closed = False

    def start(self, spec, on_exit, on_output=None, on_eof=None):
        handle = _Handle(on_exit, on_output, on_eof)
        self.handle = handle
        self.start_entered.set()
        # Emit banner while auth gate would still be waiting.
        if on_output:
            on_output(b"BANNER\n")
        self.release_auth.wait(5)
        return handle

    def close(self):
        self.closed = True
        self.release_auth.set()


class _EvidenceTerminalRunner:
    """Return the PTY immediately; tests drive its raw output afterward."""

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


def test_starting_input_is_dropped_and_output_deferred_until_running():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="starting-race")
    runner = _GatedTerminalRunner()
    runtime = SessionRuntime(core, runner=runner)
    outputs: list[bytes] = []
    runtime.subscribe_terminal(lambda item: outputs.append(item.data))

    gate_passed = threading.Event()

    def _gate(session_id, **_kwargs):
        gate_passed.wait(5)
        return True

    runtime.set_auth_gate(_gate, timeout_seconds=5.0)

    prepared = runtime.prepare_open_session(
        OpenSessionRequest(
            connection_id=core.list_connections()[0].id,
            dimensions=TerminalDimensions(rows=24, columns=80),
        ),
        client_id=ClientId("client:a"),
    )
    assert prepared.state is SessionState.STARTING

    starter = threading.Thread(
        target=runtime.start_session, args=(prepared.id,), daemon=True
    )
    starter.start()
    assert runner.start_entered.wait(2)

    attached = runtime.attach_session(
        AttachSessionRequest(
            session_id=prepared.id,
            request_input=True,
            want_terminal_output=True,
        ),
        client_id=ClientId("client:a"),
    )
    assert attached.attachment.input_owner is True

    # Early input during STARTING must not raise / must not reach the PTY.
    runtime.send_terminal_input(
        TerminalInput(
            session_id=prepared.id,
            attachment_id=attached.attachment.id,
            data=b"early\n",
        ),
        client_id=ClientId("client:a"),
    )
    assert runner.handle is not None
    assert runner.handle.writes == []
    # Banner was produced before RUNNING — held back from live subscribers.
    assert outputs == []

    gate_passed.set()
    runner.release_auth.set()
    starter.join(timeout=3)
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert b"BANNER\n" in outputs

    runtime.send_terminal_input(
        TerminalInput(
            session_id=prepared.id,
            attachment_id=attached.attachment.id,
            data=b"live\n",
        ),
        client_id=ClientId("client:a"),
    )
    assert b"live\n" in runner.handle.writes

    runtime.shutdown()
    core.close()


def test_connection_evidence_gate_does_not_equate_spawn_with_connected():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="evidence-gate")
    runner = _EvidenceTerminalRunner()
    runtime = SessionRuntime(core, runner=runner)
    runtime.enable_connection_evidence_gate()
    outputs: list[bytes] = []
    runtime.subscribe_terminal(lambda item: outputs.append(item.data))

    prepared = runtime.prepare_open_session(
        OpenSessionRequest(
            connection_id=core.list_connections()[0].id,
            dimensions=TerminalDimensions(rows=24, columns=80),
        ),
        client_id=ClientId("client:a"),
    )
    attached = runtime.attach_session(
        AttachSessionRequest(
            session_id=prepared.id,
            request_input=True,
            want_terminal_output=True,
        ),
        client_id=ClientId("client:a"),
    )
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)

    # A live SSH process and its local diagnostics are not a live connection.
    assert runtime.get_session(prepared.id).state is SessionState.STARTING
    runner.emit(b"debug1: Connecting to example.test port 22.\r\n")
    assert runtime.get_session(prepared.id).state is SessionState.STARTING
    runner.emit(b"alice@example.test's password: ")
    assert runtime.get_session(prepared.id).state is SessionState.STARTING
    assert outputs[-1] == b"alice@example.test's password: "

    # STARTING output and input remain live for native PTY auth while the
    # status waits for genuine post-login evidence.
    runtime.send_terminal_input(
        TerminalInput(
            session_id=prepared.id,
            attachment_id=attached.attachment.id,
            data=b"typed-secret\n",
        ),
        client_id=ClientId("client:a"),
    )
    assert runner.handle is not None
    assert runner.handle.writes == [b"typed-secret\n"]

    # Raw, coloured remote prompt output is real login evidence.
    runner.emit(b"\r\n\x1b[32malice@host\x1b[0m:\x1b[34m~\x1b[0m$ ")
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING

    runtime.shutdown()
    core.close()


def test_connection_evidence_gate_turns_unconfirmed_failure_into_failed_state():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="evidence-failure")
    runner = _EvidenceTerminalRunner()
    runtime = SessionRuntime(core, runner=runner)
    runtime.enable_connection_evidence_gate()
    states = []
    runtime.subscribe_events(
        lambda event: states.append(getattr(event.payload, "state", None))
    )

    prepared = runtime.prepare_open_session(
        OpenSessionRequest(
            connection_id=core.list_connections()[0].id,
            dimensions=TerminalDimensions(rows=24, columns=80),
        ),
        client_id=ClientId("client:a"),
    )
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)
    runner.emit(b"Permission denied (publickey,password).\r\n")
    assert runner.handle is not None
    runner.handle.exit(SessionExitInfo(exit_code=255, reason="process_exit"))
    runner.handle._on_eof()
    failed = runtime.get_session(prepared.id)
    # Cleanup closes the failed process immediately, but the authoritative
    # lifecycle must publish FAILED and must never claim it was RUNNING.
    assert SessionState.FAILED in states
    assert SessionState.RUNNING not in states
    assert failed.state is SessionState.CLOSED
    assert failed.failure is not None
    assert failed.failure.diagnostic == "permission denied (publickey,password)."

    runtime.shutdown()
    core.close()


def test_verbose_ssh_chatter_past_the_evidence_window_is_not_connected():
    """A truncated debug line must not read as remote output.

    ``ssh -v`` emits more than the evidence window (4000 bytes) of ``debug1:``
    lines before the password prompt. The window is a byte slice, so it opens
    mid-line, and that fragment matches none of the local-chatter prefixes the
    classifier recognises — it used to fall through to "connected" and promote
    the session before the user had even been asked for a password, spending
    the one commit point that stores a remembered credential.
    """

    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="evidence-window")
    runner = _EvidenceTerminalRunner()
    runtime = SessionRuntime(core, runner=runner)
    runtime.enable_connection_evidence_gate()
    authenticated: list[str] = []
    runtime.set_authenticated_callback(lambda session_id: authenticated.append(session_id))

    prepared = runtime.prepare_open_session(
        OpenSessionRequest(
            connection_id=core.list_connections()[0].id,
            dimensions=TerminalDimensions(rows=24, columns=80),
        ),
        client_id=ClientId("client:a"),
    )
    runtime.attach_session(
        AttachSessionRequest(
            session_id=prepared.id,
            request_input=True,
            want_terminal_output=True,
        ),
        client_id=ClientId("client:a"),
    )
    runtime.start_session(prepared.id)
    assert runner.started.wait(1)

    # Real OpenSSH pre-authentication chatter, past the 4000-byte window.
    chatter = b"".join(
        b"debug1: identity file /home/alice/.ssh/id_ed25519_%04d type -1\r\n" % index
        for index in range(80)
    )
    assert len(chatter) > 4000
    runner.emit(chatter)
    assert runtime.get_session(prepared.id).state is SessionState.STARTING
    assert authenticated == []

    # The prompt itself is still not evidence, however much precedes it.
    runner.emit(b"alice@example.test's password: ")
    assert runtime.get_session(prepared.id).state is SessionState.STARTING
    assert authenticated == []

    # Genuine post-login output still promotes, and only then.
    runner.emit(b"\r\nalice@host:~$ ")
    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert authenticated == [prepared.id]

    runtime.shutdown()
    core.close()
