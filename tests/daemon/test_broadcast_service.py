"""Focused daemon-owned broadcast lifecycle and composition tests."""

from __future__ import annotations

import threading
import time

from sshpilot.api.models.broadcast import (
    BroadcastCommandRequest,
    BroadcastExecutionPolicy,
    HostCommandState,
)
from sshpilot.api.models.interactions import ExecutionInteractionMode
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import (
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
)
from sshpilot.api.models.common import utc_now
from sshpilot.daemon.broadcast_service import BroadcastCommandService
from sshpilot.daemon.operation_runtime import OperationRuntime


class LaunchProvider:
    def __init__(self):
        self.calls = []

    def prepare_remote_command_launch(self, connection_id, command, *, interaction_policy):
        self.calls.append((connection_id, command, interaction_policy))
        return (("ssh", str(connection_id), command), {"BASE": "1"})


class Broker:
    def __init__(self):
        self.calls = []
        self.interaction_modes = []
        self.cancelled = []

    def prepare_operation_launch(
        self,
        argv,
        environment,
        *,
        scope_id,
        connection_id,
        hostname="",
        interaction_mode=ExecutionInteractionMode.INTERACTIVE,
    ):
        assert hostname == str(connection_id)
        self.interaction_modes.append(interaction_mode)
        self.calls.append((scope_id, connection_id, tuple(argv), dict(environment)))
        return tuple(argv), {**environment, "BROKER": str(connection_id)}

    def cancel_session(self, scope_id):
        self.cancelled.append(scope_id)


class Runner:
    def __init__(self, delays=None, exits=None):
        self.delays = delays or {}
        self.exits = exits or {}
        self.inputs = []
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def run(
        self,
        argv,
        environment,
        policy,
        *,
        cancel_event,
        on_process,
        on_output,
        input_data=None,
    ):
        connection_id = argv[1]
        if input_data is not None:
            self.inputs.append(bytes(input_data))
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            deadline = time.monotonic() + self.delays.get(connection_id, 0)
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    from sshpilot.daemon.operation_runtime import OperationCancelled
                    raise OperationCancelled()
                time.sleep(0.002)
            on_output("stdout", f"out:{connection_id}")
            code = self.exits.get(connection_id, 0)
            return code, f"out:{connection_id}", "err" if code else "", False, False
        finally:
            with self.lock:
                self.active -= 1


def wait_terminal(service, started, owner):
    for _ in range(500):
        summary = service.get(started.operation.operation_id, client_id=owner)
        if summary.operation.state in {
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }:
            return summary
        time.sleep(0.005)
    raise AssertionError("broadcast did not finish")


def test_broker_wraps_every_target_and_results_remain_in_request_order():
    runtime = OperationRuntime()
    launch = LaunchProvider()
    broker = Broker()
    runner = Runner(delays={"slow": 0.04, "fast": 0.001})
    service = BroadcastCommandService(runtime, launch, interaction_broker=broker, runner=runner)
    owner = ClientId("client-owner")
    started = service.start(
        BroadcastCommandRequest((ConnectionId("slow"), ConnectionId("fast")), "hostname"),
        owner_client_id=owner,
    )
    result = wait_terminal(service, started, owner)
    assert result.operation.state is OperationState.SUCCEEDED
    assert tuple(item.connection_id for item in result.targets) == ("slow", "fast")
    assert all(item.state is HostCommandState.SUCCEEDED for item in result.targets)
    assert {call[1] for call in broker.calls} == {"slow", "fast"}
    assert all(call[0] == started.operation.operation_id for call in broker.calls)
    assert broker.interaction_modes == [
        ExecutionInteractionMode.INTERACTIVE,
        ExecutionInteractionMode.INTERACTIVE,
    ]
    assert broker.cancelled == [started.operation.operation_id]
    runtime.shutdown()


def test_autofill_only_broadcast_policy_reaches_interaction_broker():
    runtime = OperationRuntime()
    broker = Broker()
    service = BroadcastCommandService(
        runtime, LaunchProvider(), interaction_broker=broker, runner=Runner()
    )
    owner = ClientId("client-owner")
    started = service.start(
        BroadcastCommandRequest(
            (ConnectionId("demo"),),
            "true",
            BroadcastExecutionPolicy(
                interaction_mode=ExecutionInteractionMode.AUTOFILL_ONLY
            ),
        ),
        owner_client_id=owner,
    )
    wait_terminal(service, started, owner)
    assert broker.interaction_modes == [ExecutionInteractionMode.AUTOFILL_ONLY]
    runtime.shutdown()


def test_concurrency_is_bounded_and_nonzero_exit_fails_parent():
    runtime = OperationRuntime()
    runner = Runner(delays={str(i): 0.01 for i in range(6)}, exits={"3": 7})
    service = BroadcastCommandService(
        runtime, LaunchProvider(), interaction_broker=Broker(), runner=runner
    )
    owner = ClientId("client-owner")
    request = BroadcastCommandRequest(
        tuple(ConnectionId(str(i)) for i in range(6)),
        "false",
        BroadcastExecutionPolicy(concurrency_limit=2),
    )
    result = wait_terminal(service, service.start(request, owner_client_id=owner), owner)
    assert runner.maximum <= 2
    assert result.operation.state is OperationState.FAILED
    assert result.targets[3].exit_code == 7
    runtime.shutdown()


def test_daemon_broadcast_passes_protected_command_input_to_runner():
    runtime = OperationRuntime()
    runner = Runner()
    service = BroadcastCommandService(
        runtime, LaunchProvider(), interaction_broker=Broker(), runner=runner
    )
    owner = ClientId("client-owner")
    request = BroadcastCommandRequest((ConnectionId("demo"),), "sudo true")
    started = service.start(
        request,
        owner_client_id=owner,
        input_data=bytearray(b"password\n"),
    )
    result = wait_terminal(service, started, owner)
    assert result.operation.state is OperationState.SUCCEEDED
    assert runner.inputs == [b"password\n"]
    runtime.shutdown()


def test_daemon_broadcast_publishes_incremental_output():
    runtime = OperationRuntime()
    output = []
    service = BroadcastCommandService(
        runtime,
        LaunchProvider(),
        interaction_broker=Broker(),
        runner=Runner(),
        output_publisher=lambda *event: output.append(event),
    )
    owner = ClientId("client-owner")
    started = service.start(
        BroadcastCommandRequest((ConnectionId("demo"),), "docker logs -f"),
        owner_client_id=owner,
    )
    wait_terminal(service, started, owner)
    assert output == [(started.operation.operation_id, ConnectionId("demo"), "stdout", "out:demo")]
    runtime.shutdown()


def test_terminal_result_retention_is_bounded():
    runtime = OperationRuntime(terminal_retention=1)
    service = BroadcastCommandService(
        runtime,
        LaunchProvider(),
        interaction_broker=Broker(),
        runner=Runner(),
        terminal_retention=1,
    )
    owner = ClientId("client-owner")
    first = service.start(
        BroadcastCommandRequest((ConnectionId("one"),), "true"), owner_client_id=owner
    )
    wait_terminal(service, first, owner)
    second = service.start(
        BroadcastCommandRequest((ConnectionId("two"),), "true"), owner_client_id=owner
    )
    wait_terminal(service, second, owner)
    assert first.operation.operation_id not in service._targets
    assert first.operation.operation_id not in service._cancels
    runtime.shutdown()


def test_real_server_capabilities_survive_refresh_and_client_can_start(tmp_path):
    """Handshake and system.get_capabilities must agree for production clients."""
    from sshpilot.api import DaemonClient
    from sshpilot.api.capabilities import Capability
    from sshpilot.daemon import DaemonServer
    from tests.helpers.fake_connection_repository import make_test_connection_service

    socket_path = tmp_path / "daemon.sock"
    launch_provider = LaunchProvider()
    server = DaemonServer(
        lambda: make_test_connection_service(launch_provider=launch_provider),
        socket_path=socket_path,
    )
    server.start_in_thread()
    client = DaemonClient(socket_path=socket_path)
    try:
        capabilities = client.get_capabilities()
        assert Capability.BROADCAST_READ in capabilities.supported
        assert Capability.BROADCAST_WRITE in capabilities.supported
        assert Capability.BROADCAST_EVENTS in capabilities.supported
        started = client.start_broadcast_command(
            BroadcastCommandRequest((ConnectionId("demo"),), "true")
        )
        assert started.operation.kind.value == "broadcast_command"
    finally:
        client.close()
        server.shutdown()
        assert server.wait_stopped()


def test_cancel_rpc_only_signals_and_workers_finish_cancellation():
    runtime = OperationRuntime()
    runner = Runner(delays={"0": 2, "1": 2, "2": 2})
    service = BroadcastCommandService(
        runtime, LaunchProvider(), interaction_broker=Broker(), runner=runner
    )
    owner = ClientId("client-owner")
    started = service.start(
        BroadcastCommandRequest(
            (ConnectionId("0"), ConnectionId("1"), ConnectionId("2")),
            "sleep",
            BroadcastExecutionPolicy(concurrency_limit=2),
        ),
        owner_client_id=owner,
    )
    for _ in range(100):
        if runner.maximum == 2:
            break
        time.sleep(0.005)
    before = time.monotonic()
    service.cancel(started.operation.operation_id, client_id=owner)
    assert time.monotonic() - before < 0.1
    result = wait_terminal(service, started, owner)
    assert result.operation.state is OperationState.CANCELLED
    assert all(item.state is HostCommandState.CANCELLED for item in result.targets)
    runtime.shutdown()


class QueuedRuntime:
    """Operation runtime double whose worker has not entered its body."""

    def __init__(self):
        self.summary = None

    def start_operation(self, kind, body, *, owner_client_id, message):
        self.summary = OperationSummary(
            operation_id=OperationId("operation-queued"),
            kind=kind,
            state=OperationState.QUEUED,
            message=message,
            created_at=utc_now(),
            owner_client_id=owner_client_id,
        )
        return self.summary

    def get_operation(self, operation_id):
        assert operation_id == self.summary.operation_id
        return self.summary

    def cancel_operation(self, operation_id):
        assert operation_id == self.summary.operation_id
        self.summary = OperationSummary(
            operation_id=self.summary.operation_id,
            kind=OperationKind.BROADCAST_COMMAND,
            state=OperationState.CANCELLED,
            message="cancelled",
            created_at=self.summary.created_at,
            finished_at=utc_now(),
            owner_client_id=self.summary.owner_client_id,
        )
        return self.summary


def test_cancel_before_operation_body_finalizes_pending_targets_and_retention():
    runtime = QueuedRuntime()
    service = BroadcastCommandService(
        runtime, LaunchProvider(), interaction_broker=Broker(), runner=Runner()
    )
    owner = ClientId("client-owner")
    started = service.start(
        BroadcastCommandRequest((ConnectionId("one"), ConnectionId("two")), "true"),
        owner_client_id=owner,
    )
    cancelled = service.cancel(started.operation.operation_id, client_id=owner)
    assert cancelled.operation.state is OperationState.CANCELLED
    assert all(target.state is HostCommandState.CANCELLED for target in cancelled.targets)
    assert started.operation.operation_id in service._terminal_records
