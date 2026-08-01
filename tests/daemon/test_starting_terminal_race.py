"""SessionRuntime: defer live output / drop input until RUNNING."""

from __future__ import annotations

import threading


from sshpilot.api import InProcessClient
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    OpenSessionRequest,
    SessionExitInfo,
    SessionState,
)
from sshpilot.api.models.terminal import TerminalDimensions, TerminalInput
from sshpilot.daemon.session_runtime import SessionRuntime


class _Connection:
    def __init__(self):
        self.nickname = "demo"
        self.id = "demo"
        self.uuid = "demo"
        self.host = "demo"
        self.hostname = "example.test"
        self.username = "alice"
        self.port = 22
        self.protocol = "ssh"
        self.aliases = []
        self.auth_method = 0
        self.keyfile = ""
        self.identity_files = []
        self.certificate = ""
        self.certificate_files = []
        self.x11_forwarding = False
        self.forwarding_rules = []
        self.proxy_jump = []
        self.data = {}


class _Manager:
    def __init__(self):
        self.connection = _Connection()

    def get_connections(self):
        return [self.connection]

    def connect(self, _name, _callback):
        return 1

    def disconnect(self, _handler_id):
        return None


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


def test_starting_input_is_dropped_and_output_deferred_until_running():
    manager = _Manager()
    core = InProcessClient(manager, client_name="starting-race")
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
