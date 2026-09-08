"""The daemon owns the host-info probe text, its policy, and its parsing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.broadcast import (
    BroadcastCommandRequest,
    BroadcastCommandSummary,
    HostCommandResult,
    HostCommandState,
)
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.host_info import HostInfoProbe, HostInfoRequest
from sshpilot.api.models.interactions import ExecutionInteractionMode
from sshpilot.api.models.operations import (
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
)
from sshpilot.core.host_info import (
    FULL_PROBE_COMMAND,
    LIVE_PROBE_COMMAND,
    NETWORK_COUNTERS_COMMAND,
)
from sshpilot.daemon.host_info_service import HostInfoService

CLIENT = ClientId("client-1")
CONNECTION = ConnectionId("conn-1")


def _operation(state: OperationState) -> OperationSummary:
    return OperationSummary(
        OperationId("op-1"),
        OperationKind.BROADCAST_COMMAND,
        state,
        "probe",
        datetime.now(timezone.utc),
    )


class FakeBroadcastService:
    """Records the request it was given and replays a scripted result."""

    def __init__(self, target: HostCommandResult, state=OperationState.SUCCEEDED) -> None:
        self.requests = []
        self._target = target
        self._state = state
        self.cancelled = []

    def start(self, request, *, owner_client_id, input_data=None):
        self.requests.append(request)
        return self._summary()

    def get(self, operation_id, *, client_id):
        return self._summary()

    def cancel(self, operation_id, *, client_id):
        self.cancelled.append(operation_id)
        return BroadcastCommandSummary(
            _operation(OperationState.CANCELLED),
            (HostCommandResult(CONNECTION, HostCommandState.CANCELLED),),
        )

    def _summary(self):
        return BroadcastCommandSummary(_operation(self._state), (self._target,))


def _succeeded(stdout: str) -> FakeBroadcastService:
    return FakeBroadcastService(
        HostCommandResult(CONNECTION, HostCommandState.SUCCEEDED, 0, stdout)
    )


FULL_OUTPUT = (
    "===HOSTNAME===\nrouter\n"
    "===MEMINFO===\nMemTotal: 1024 kB\nMemAvailable: 512 kB\n"
    "===NET_DEV===\n  eth0: 100 1 0 0 0 0 0 0 200 1\n"
    "===END===\n"
)


def test_full_probe_sends_the_core_probe_text_interactively():
    broadcast = _succeeded(FULL_OUTPUT)
    service = HostInfoService(broadcast)

    summary = service.start(HostInfoRequest(CONNECTION), owner_client_id=CLIENT)

    request = broadcast.requests[0]
    assert type(request) is BroadcastCommandRequest
    assert request.command == FULL_PROBE_COMMAND
    assert request.connection_ids == (CONNECTION,)
    assert request.policy.concurrency_limit == 1
    assert request.policy.interaction_mode is ExecutionInteractionMode.INTERACTIVE
    # The gather authenticates once and becomes the multiplex master the
    # autofill-only samples below ride; without it an unstored password
    # gathers fine and then never produces a live sample.
    assert request.policy.require_master is True
    assert summary.probe is HostInfoProbe.FULL
    assert summary.snapshot is not None
    assert summary.snapshot.hostname == "router"
    assert summary.snapshot.memory.used_bytes == 512 * 1024
    assert summary.counters[0].rx_bytes == 100


def test_counter_probe_is_cheap_and_never_raises_its_own_prompt():
    broadcast = _succeeded("===NET_DEV===\n  eth0: 5 1 0 0 0 0 0 0 7 1\n===END===\n")
    service = HostInfoService(broadcast)

    summary = service.start(
        HostInfoRequest(CONNECTION, HostInfoProbe.NETWORK_COUNTERS),
        owner_client_id=CLIENT,
    )

    request = broadcast.requests[0]
    assert request.command == NETWORK_COUNTERS_COMMAND
    assert request.policy.interaction_mode is ExecutionInteractionMode.AUTOFILL_ONLY
    assert request.policy.timeout_seconds == 15.0
    assert request.policy.require_master is True
    assert summary.snapshot is None
    assert summary.counters == (summary.counters[0],)
    assert (summary.counters[0].rx_bytes, summary.counters[0].tx_bytes) == (5, 7)


def test_a_running_probe_reports_no_result_yet():
    broadcast = FakeBroadcastService(
        HostCommandResult(CONNECTION, HostCommandState.RUNNING),
        state=OperationState.RUNNING,
    )
    service = HostInfoService(broadcast)

    summary = service.start(HostInfoRequest(CONNECTION), owner_client_id=CLIENT)

    assert summary.snapshot is None
    assert summary.counters == ()
    assert summary.failure is None


def test_a_failed_probe_surfaces_the_remote_error():
    broadcast = FakeBroadcastService(
        HostCommandResult(
            CONNECTION, HostCommandState.FAILED, 127, "", "sh: ip: not found"
        ),
        state=OperationState.FAILED,
    )
    service = HostInfoService(broadcast)

    summary = service.start(HostInfoRequest(CONNECTION), owner_client_id=CLIENT)

    assert summary.snapshot is None
    assert summary.failure is not None
    assert summary.failure.message == "sh: ip: not found"


def test_get_and_cancel_delegate_to_the_broadcast_operation():
    broadcast = _succeeded(FULL_OUTPUT)
    service = HostInfoService(broadcast)
    started = service.start(HostInfoRequest(CONNECTION), owner_client_id=CLIENT)
    operation_id = started.operation.operation_id

    # The probe kind is remembered until the operation reaches a terminal
    # state, so a re-read of a finished probe re-parses from the same result.
    reread = service.get(operation_id, client_id=CLIENT)
    assert reread.probe is HostInfoProbe.FULL

    cancelled = service.cancel(operation_id, client_id=CLIENT)
    assert broadcast.cancelled == [operation_id]
    assert cancelled.snapshot is None


def test_an_unknown_operation_is_rejected():
    service = HostInfoService(_succeeded(FULL_OUTPUT))
    with pytest.raises(SshPilotError) as excinfo:
        service.get(OperationId("nope"), client_id=CLIENT)
    assert excinfo.value.code is ErrorCode.OPERATION_NOT_FOUND


def test_the_service_requires_a_broadcast_service():
    with pytest.raises(ValueError):
        HostInfoService(None)


def test_probe_retention_is_bounded_so_sampling_cannot_grow_it_forever():
    broadcast = _succeeded(FULL_OUTPUT)
    service = HostInfoService(broadcast, retention=2)
    for index in range(3):
        service._remember(OperationId(f"op-{index}"), HostInfoProbe.NETWORK_COUNTERS)
    assert list(service._probes) == [OperationId("op-1"), OperationId("op-2")]


def test_the_live_probe_is_cheap_and_never_raises_its_own_prompt():
    """A prompt every two seconds on an established session would be unusable,
    so only the first gather is interactive."""

    broadcast = _succeeded(
        "===NET_DEV===\n  eth0: 5 1 0 0 0 0 0 0 7 1\n"
        "===STAT===\ncpu  100 2 30 400 5 0 0 0 0 0\ncpu0 100 2 30 400 5 0 0 0 0 0\n"
        "===MEMINFO===\nMemTotal: 1024 kB\nMemAvailable: 256 kB\n"
        "===LOADAVG===\n0.50 0.40 0.30 1/200 12345\n"
        "===END===\n"
    )
    service = HostInfoService(broadcast)

    summary = service.start(
        HostInfoRequest(CONNECTION, HostInfoProbe.LIVE), owner_client_id=CLIENT
    )

    request = broadcast.requests[0]
    assert request.command == LIVE_PROBE_COMMAND
    assert request.policy.interaction_mode is ExecutionInteractionMode.AUTOFILL_ONLY
    assert request.policy.timeout_seconds == 15.0
    # Autofill-only can only re-authenticate from the store, so the sample
    # rides the FULL gather's master instead of prompting every two seconds.
    assert request.policy.require_master is True

    assert summary.probe is HostInfoProbe.LIVE
    # A live sample is not a gather: it carries no snapshot.
    assert summary.snapshot is None
    assert summary.live is not None
    assert [item.name for item in summary.live.cpu_times] == ["cpu", "cpu0"]
    assert summary.live.memory.used_bytes == (1024 - 256) * 1024
    assert summary.live.load_average.one == 0.50
    # counters is filled by every probe, so a caller that only wants bandwidth
    # need not know which probe answered.
    assert summary.counters == summary.live.counters
    assert (summary.counters[0].rx_bytes, summary.counters[0].tx_bytes) == (5, 7)


def test_the_full_gather_reads_the_counters_a_live_sample_differences_against():
    """Without this baseline the first live sample would have nothing to
    subtract from and would report a rate of zero."""

    broadcast = _succeeded(
        "===HOSTNAME===\nrouter\n"
        "===STAT===\ncpu  100 2 30 400 5 0 0 0 0 0\n"
        "===NET_DEV===\n  eth0: 100 1 0 0 0 0 0 0 200 2\n"
        "===END===\n"
    )
    service = HostInfoService(broadcast)

    summary = service.start(
        HostInfoRequest(CONNECTION, HostInfoProbe.FULL), owner_client_id=CLIENT
    )

    assert broadcast.requests[0].command == FULL_PROBE_COMMAND
    assert [item.name for item in summary.snapshot.cpu_times] == ["cpu"]
    assert summary.counters[0].rx_bytes == 100
    # The gather is not itself a live sample.
    assert summary.live is None
