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
    PreCommandStage,
)
from sshpilot.daemon import DaemonServer
from tests.helpers.fake_connection_repository import FakeConnectionRepository, _record


class _LaunchProvider:
    """Enough of a launch provider to produce an argv without touching SSH."""

    def prepare_terminal_launch(self, connection_id, **kwargs):
        return ("ssh", "example.com"), {"SSH_AUTH_SOCK": "/tmp/agent"}


def _daemon(tmp_path, pre_command: str, *, metadata=None, hostname="example.com"):
    record = _record(hostname=hostname, data={"pre_command": pre_command})
    repo = FakeConnectionRepository([record])
    if metadata:
        # The knock host comes from the connection, so these tests point it at
        # the loopback listeners they just stood up.
        repo.update_connection_metadata(record.id, dict(metadata))
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


def test_a_knock_sequence_is_sent_in_order_before_the_launch(tmp_path):
    """The native knocker, through the real daemon, against a real listener.

    A knock is only worth anything if the packets arrive, in order, before
    OpenSSH dials -- and none of that is observable from a mocked socket. This
    stands up three listeners, points a connection's sequence at them, and
    checks what actually turned up.
    """

    import socket

    listeners = []
    ports = []
    arrived = []
    for _ in range(3):
        handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        handle.bind(("127.0.0.1", 0))
        handle.listen(8)
        listeners.append(handle)
        ports.append(handle.getsockname()[1])

    def watch(handle, port):
        while True:
            try:
                client, _ = handle.accept()
            except OSError:
                return
            arrived.append(port)
            client.close()

    for handle, port in zip(listeners, ports):
        threading.Thread(target=watch, args=(handle, port), daemon=True).start()

    sequence = ",".join(str(port) for port in ports)
    server = _daemon(
        tmp_path,
        "",
        metadata={"pre_command_knock": sequence},
        hostname="127.0.0.1",
    )
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

        assert _wait(lambda: len(arrived) >= 3), arrived
        assert arrived == ports, "the sequence must arrive in the order written"
        assert spec.argv[0] == "ssh"

        assert _wait(lambda: any(n.phase is PreCommandPhase.FINISHED for n in notices))
        finished = next(n for n in notices if n.phase is PreCommandPhase.FINISHED)
        assert finished.reason is PreCommandReason.OK
        assert finished.stage is PreCommandStage.KNOCK
        # A knock runs no process, so it has no exit status to report.
        assert finished.exit_code is None
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()
        for handle in listeners:
            handle.close()


def test_a_knock_and_a_command_both_run_in_that_order(tmp_path):
    """They compose: the sequence opens the way, the command uses it."""

    import socket

    handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    handle.bind(("127.0.0.1", 0))
    handle.listen(4)
    port = handle.getsockname()[1]
    knocked = threading.Event()

    def watch():
        try:
            client, _ = handle.accept()
        except OSError:
            return
        knocked.set()
        client.close()

    threading.Thread(target=watch, daemon=True).start()

    marker = tmp_path / "after-knock"
    server = _daemon(
        tmp_path,
        f"touch {marker}",
        metadata={
            "pre_command": f"touch {marker}",
            "pre_command_knock": str(port),
        },
        hostname="127.0.0.1",
    )
    client = DaemonClient(socket_path=server.socket_path)
    try:
        connection_id = client.list_connections()[0].id
        client.prepare_external_terminal_launch(connection_id)
        assert _wait(knocked.is_set), "the knock never arrived"
        assert _wait(marker.exists), "the command did not run after the knock"
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()
        handle.close()


def test_the_editor_s_test_button_sends_a_real_knock(tmp_path):
    """Test must exercise the same sockets a launch would, through the daemon.

    A Test that took a different path would not be testing the thing that runs.
    """

    import socket

    handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    handle.bind(("127.0.0.1", 0))
    handle.listen(4)
    port = handle.getsockname()[1]
    knocked = threading.Event()

    def watch():
        try:
            client_socket, _ = handle.accept()
        except OSError:
            return
        knocked.set()
        client_socket.close()

    threading.Thread(target=watch, daemon=True).start()

    server = _daemon(tmp_path, "")
    client = DaemonClient(socket_path=server.socket_path)
    try:
        result = client.test_pre_command(
            "", knock=str(port), hostname="127.0.0.1"
        )
        assert knocked.wait(2.0), "the knock never reached the listener"
        assert result.succeeded
        assert result.stage is PreCommandStage.KNOCK
        # No daemon-composed prose: the sentence the user reads is the
        # frontend's, so it can be translated.
        assert result.output == ""
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()
        handle.close()


def test_a_malformed_sequence_is_explained_by_test(tmp_path):
    """The editor is the one place the typo can still be fixed."""

    server = _daemon(tmp_path, "")
    client = DaemonClient(socket_path=server.socket_path)
    try:
        result = client.test_pre_command(
            "", knock="7000,80OO", hostname="127.0.0.1"
        )
        assert not result.succeeded
        assert result.stage is PreCommandStage.KNOCK
        assert "not a port number" in result.output
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()
