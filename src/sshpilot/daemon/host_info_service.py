"""Daemon-owned remote host-information probes.

The daemon decides what runs on the remote host and hands frontends a parsed
snapshot, so no frontend ever carries probe text or parsing rules.  Execution
is delegated to :class:`BroadcastCommandService` rather than reimplemented:
that service already owns SSH construction, authentication, interaction
brokering, output limits, cancellation and the operation lifecycle, and this
one only adds "which command" and "how to read the answer".

Because execution is delegated, a host-info operation *is* a one-shot remote
command operation and is reported with ``OperationKind.BROADCAST_COMMAND``.
Frontends observe completion through the ordinary operation state events.

This module is deliberately GTK-free.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Optional, Tuple

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.broadcast import (
    BroadcastCommandRequest,
    BroadcastCommandSummary,
    BroadcastExecutionPolicy,
    HostCommandState,
)
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.host_info import (
    HostInfoProbe,
    HostInfoRequest,
    HostInfoSnapshot,
    HostInfoSummary,
    InterfaceCounters,
    LiveSample,
)
from sshpilot.api.models.interactions import ExecutionInteractionMode
from sshpilot.api.models.operations import OperationId, ServiceFailure
from sshpilot.core.host_info import (
    FULL_PROBE_COMMAND,
    LIVE_PROBE_COMMAND,
    NETWORK_COUNTERS_COMMAND,
    parse_counters_probe,
    parse_host_info,
    parse_live_probe,
    parse_network_counters,
)
from sshpilot.core.host_info.parser import split_sections

logger = logging.getLogger(__name__)

#: A full gather runs two dozen small readers; a slow or loaded host still
#: answers well inside this, and the operation is cancellable throughout.
FULL_PROBE_TIMEOUT_SECONDS = 60.0

#: A live sample is a handful of ``cat``s and must never outlive its sampling
#: interval, or repeated samples would queue behind each other.
SAMPLE_PROBE_TIMEOUT_SECONDS = 15.0

#: The name this bound had while bandwidth was the only thing sampled.
COUNTERS_PROBE_TIMEOUT_SECONDS = SAMPLE_PROBE_TIMEOUT_SECONDS

_PROBE_COMMANDS = {
    HostInfoProbe.FULL: FULL_PROBE_COMMAND,
    HostInfoProbe.NETWORK_COUNTERS: NETWORK_COUNTERS_COMMAND,
    HostInfoProbe.LIVE: LIVE_PROBE_COMMAND,
}

_PROBE_TIMEOUTS = {
    HostInfoProbe.FULL: FULL_PROBE_TIMEOUT_SECONDS,
    HostInfoProbe.NETWORK_COUNTERS: SAMPLE_PROBE_TIMEOUT_SECONDS,
    HostInfoProbe.LIVE: SAMPLE_PROBE_TIMEOUT_SECONDS,
}

#: How many probe operations stay readable after they finish.  A client that
#: learns of completion from an event still re-reads the result, so a finished
#: probe must not become "not found"; the bound keeps bandwidth sampling from
#: growing this map without limit.
DEFAULT_PROBE_RETENTION = 128


class HostInfoService:
    """Run read-only host probes and return parsed, frontend-neutral results."""

    def __init__(self, broadcast_service, *, retention: int = DEFAULT_PROBE_RETENTION) -> None:
        if broadcast_service is None:
            raise ValueError("host information requires a broadcast command service")
        if type(retention) is not int or retention < 1:
            raise ValueError("probe retention must be a positive integer")
        self._broadcast = broadcast_service
        self._retention = retention
        self._lock = threading.Lock()
        self._probes: "OrderedDict[OperationId, HostInfoProbe]" = OrderedDict()

    # -- lifecycle ------------------------------------------------------

    def start(
        self, request: HostInfoRequest, *, owner_client_id: ClientId
    ) -> HostInfoSummary:
        if type(request) is not HostInfoRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST, "A host information request is required"
            )
        probe = request.probe
        summary = self._broadcast.start(
            BroadcastCommandRequest(
                (request.connection_id,),
                _PROBE_COMMANDS[probe],
                BroadcastExecutionPolicy(
                    concurrency_limit=1,
                    timeout_seconds=_PROBE_TIMEOUTS[probe],
                    # A first gather may need a passphrase, password or MFA
                    # answer; repeated live samples must never raise a prompt
                    # of their own. The "established session" they ride is the
                    # OpenSSH multiplex master created by require_master below
                    # -- a ControlMaster on the shared ControlPath, not a PTY
                    # and not a long-lived Host Info channel.
                    interaction_mode=(
                        ExecutionInteractionMode.INTERACTIVE
                        if probe is HostInfoProbe.FULL
                        else ExecutionInteractionMode.AUTOFILL_ONLY
                    ),
                    # Every probe holds the multiplex master, preference or
                    # not: the FULL gather authenticates once and becomes the
                    # master, and the autofill-only samples ride it instead
                    # of re-authenticating. Without this a connection whose
                    # password was typed but not stored gathers fine and
                    # then never produces a live sample (and so never a CPU
                    # utilization, which exists only between two readings).
                    require_master=True,
                ),
            ),
            owner_client_id=owner_client_id,
        )
        self._remember(summary.operation.operation_id, probe)
        return self._project(summary, probe)

    def get(self, operation_id: OperationId, *, client_id: ClientId) -> HostInfoSummary:
        summary = self._broadcast.get(operation_id, client_id=client_id)
        return self._project(summary, self._probe_for(operation_id))

    def cancel(self, operation_id: OperationId, *, client_id: ClientId) -> HostInfoSummary:
        summary = self._broadcast.cancel(operation_id, client_id=client_id)
        return self._project(summary, self._probe_for(operation_id))

    # -- projection -----------------------------------------------------

    def _remember(self, operation_id: OperationId, probe: HostInfoProbe) -> None:
        with self._lock:
            self._probes[operation_id] = probe
            self._probes.move_to_end(operation_id)
            while len(self._probes) > self._retention:
                self._probes.popitem(last=False)

    def _probe_for(self, operation_id: OperationId) -> HostInfoProbe:
        with self._lock:
            probe = self._probes.get(operation_id)
        if probe is None:
            raise SshPilotError(
                ErrorCode.OPERATION_NOT_FOUND,
                "The requested host information probe does not exist",
            )
        return probe

    def _project(
        self, summary: BroadcastCommandSummary, probe: HostInfoProbe
    ) -> HostInfoSummary:
        """Parse a finished probe; a running one reports no result yet."""

        target = summary.targets[0] if summary.targets else None
        if target is None or target.state is not HostCommandState.SUCCEEDED:
            return HostInfoSummary(
                summary.operation, probe, None, (), self._failure_for(target)
            )

        snapshot, counters, live = self._parse(probe, target.stdout or "")
        return HostInfoSummary(summary.operation, probe, snapshot, counters, None, live)

    @staticmethod
    def _parse(
        probe: HostInfoProbe, stdout: str
    ) -> Tuple[
        Optional[HostInfoSnapshot],
        Tuple[InterfaceCounters, ...],
        Optional[LiveSample],
    ]:
        """Read one probe's output.

        ``counters`` is filled by every probe so a caller that only wants
        bandwidth need not know which probe produced the answer.
        """

        try:
            if probe is HostInfoProbe.NETWORK_COUNTERS:
                return None, parse_counters_probe(stdout), None
            if probe is HostInfoProbe.LIVE:
                live = parse_live_probe(stdout)
                return None, live.counters, live
            snapshot = parse_host_info(stdout)
            counters = parse_network_counters(split_sections(stdout).get("NET_DEV", ""))
            return snapshot, counters, None
        except (TypeError, ValueError) as error:
            # A host that answers with something unparseable is a failed
            # probe, not a daemon fault; report it as such rather than
            # letting a parse error escape as an internal error.
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "The remote host returned unreadable system information",
            ) from error

    @staticmethod
    def _failure_for(target) -> Optional[ServiceFailure]:
        if target is None:
            return None
        if target.failure is not None:
            return target.failure
        if target.state is HostCommandState.FAILED:
            detail = (target.stderr or "").strip()
            return ServiceFailure(
                code="host_info_probe_failed",
                message=detail or "The host information probe failed",
            )
        return None
