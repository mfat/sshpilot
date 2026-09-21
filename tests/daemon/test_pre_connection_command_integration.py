"""The pre-connection command, through a real daemon and a real client.

The unit tests pin the runner and the launcher hook in isolation. This one
proves the parts that only exist in composition: the daemon builds a runner at
startup, the connection service can read ``pre_command`` off a real record, the
command actually runs, and the resulting notice survives the event fan-out, the
codec and the client decode.

``connections.prepare_external_terminal_launch`` is the vehicle because it is
the one launch route that needs no OpenSSH child -- and because it is the route
that does *not* go through ``SshLauncher``, so it is the easiest one to leave
behind by accident.
"""

from __future__ import annotations

import threading
import time

from sshpilot.api import DaemonClient, EventType
from sshpilot.api.models.pre_command import (
    PreCommandLaunchKind,
    PreCommandPhase,
    PreCommandReason,
)
from sshpilot.daemon import DaemonServer
from tests.helpers.fake_connection_repository import FakeConnectionRepository, _record


class _LaunchProvider:
    """Enough of a launch provider to produce an argv without touching SSH."""

    def prepare_terminal_launch(self, connection_id, **kwargs):
        return ("ssh", "example.com"), {"SSH_AUTH_SOCK": "/tmp/agent"}


def _daemon(tmp_path, pre_command: str):
    repo = FakeConnectionRepository(
        [_record(data={"pre_command": pre_command})]
    )
    socket_path = tmp_path / "daemon" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _core_factory():
        from sshpilot.core.connection_application_service import (
            ConnectionApplicationService,
        )

        return ConnectionApplicationService(
            repo,
            launch_provider=_LaunchProvider(),
            client_name="sshpilotd",
        )

    server = DaemonServer(_core_factory, socket_path=socket_path)
    server.start_in_thread()
    return server


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.01)
    return bool(predicate())


def test_an_external_terminal_launch_runs_the_command_and_narrates_it(tmp_path):
    marker = tmp_path / "knocked"
    server = _daemon(tmp_path, f"touch {marker}")
    client = DaemonClient(socket_path=server.socket_path)
    try:
        notices = []
        subscription = client.subscribe_events(
            lambda event: (
                notices.append(event.payload)
                if event.type is EventType.PRE_CONNECTION_COMMAND
                else None
            )
        )

        connection_id = client.list_connections()[0].id
        client.prepare_external_terminal_launch(connection_id)

        assert _wait(lambda: len(notices) >= 2), notices
        assert marker.exists(), "the pre-connection command did not run"

        running, finished = notices[0], notices[1]
        assert running.phase is PreCommandPhase.RUNNING
        assert running.kind is PreCommandLaunchKind.TERMINAL
        assert finished.phase is PreCommandPhase.FINISHED
        assert finished.reason is PreCommandReason.OK
        assert finished.exit_code == 0
        assert finished.connection_id == connection_id
        subscription.close()
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_a_failing_command_is_narrated_and_the_launch_still_succeeds(tmp_path):
    """The contract the whole feature is built around: notify, never abort."""

    server = _daemon(tmp_path, "exit 5")
    client = DaemonClient(socket_path=server.socket_path)
    try:
        notices = []
        client.subscribe_events(
            lambda event: (
                notices.append(event.payload)
                if event.type is EventType.PRE_CONNECTION_COMMAND
                else None
            )
        )

        connection_id = client.list_connections()[0].id
        spec = client.prepare_external_terminal_launch(connection_id)

        assert spec.argv[0] == "ssh"
        assert _wait(lambda: any(n.phase is PreCommandPhase.FINISHED for n in notices))
        finished = next(
            n for n in notices if n.phase is PreCommandPhase.FINISHED
        )
        assert finished.reason is PreCommandReason.NONZERO_EXIT
        assert finished.exit_code == 5
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_a_connection_without_a_command_publishes_nothing(tmp_path):
    server = _daemon(tmp_path, "")
    client = DaemonClient(socket_path=server.socket_path)
    try:
        notices = []
        client.subscribe_events(
            lambda event: (
                notices.append(event.payload)
                if event.type is EventType.PRE_CONNECTION_COMMAND
                else None
            )
        )

        connection_id = client.list_connections()[0].id
        client.prepare_external_terminal_launch(connection_id)

        # Nothing to wait for; give the daemon a moment to prove it stayed quiet.
        assert not _wait(lambda: bool(notices), timeout=0.5)
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()
