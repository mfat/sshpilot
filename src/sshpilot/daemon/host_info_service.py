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
    HostInfoFailure,
    HostInfoFailureCode,
    HostInfoProbe,
    HostInfoRequest,
    HostInfoSnapshot,
    HostInfoSummary,
    InterfaceCounters,
)
from sshpilot.api.models.interactions import ExecutionInteractionMode
from sshpilot.api.models.operations import OperationId
from sshpilot.core.host_info import (
    FULL_PROBE_COMMAND,
    NETWORK_COUNTERS_COMMAND,
    parse_counters_probe,
    parse_host_info,
    parse_network_counters,
)
from sshpilot.core.host_info.parser import split_sections

logger = logging.getLogger(__name__)

#: A full gather runs two dozen small readers; a slow or loaded host still
#: answers well inside this, and the operation is cancellable throughout.
FULL_PROBE_TIMEOUT_SECONDS = 60.0

#: Bandwidth sampling is a single ``cat`` and must never outlive its sampling
#: interval, or repeated samples would queue behind each other.
COUNTERS_PROBE_TIMEOUT_SECONDS = 15.0

_PROBE_COMMANDS = {
    HostInfoProbe.FULL: FULL_PROBE_COMMAND,
    HostInfoProbe.NETWORK_COUNTERS: NETWORK_COUNTERS_COMMAND,
}

_PROBE_TIMEOUTS = {
    HostInfoProbe.FULL: FULL_PROBE_TIMEOUT_SECONDS,
    HostInfoProbe.NETWORK_COUNTERS: COUNTERS_PROBE_TIMEOUT_SECONDS,
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
                    # answer; repeated bandwidth samples must never raise a
                    # prompt of their own on top of an established session.
                    interaction_mode=(
                        ExecutionInteractionMode.INTERACTIVE
                        if probe is HostInfoProbe.FULL
                        else ExecutionInteractionMode.AUTOFILL_ONLY
                    ),
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

        try:
            snapshot, counters = self._parse(probe, target.stdout or "")
        except (TypeError, ValueError):
            logger.debug("Host information parsing failed", exc_info=True)
            return HostInfoSummary(
                summary.operation,
                probe,
                None,
                (),
                HostInfoFailure(
                    HostInfoFailureCode.UNREADABLE_SYSTEM_INFORMATION,
                    ErrorCode.REMOTE_COMMAND_FAILED,
                ),
            )
        return HostInfoSummary(summary.operation, probe, snapshot, counters, None)

    @staticmethod
    def _parse(
        probe: HostInfoProbe, stdout: str
    ) -> Tuple[Optional[HostInfoSnapshot], Tuple[InterfaceCounters, ...]]:
        if probe is HostInfoProbe.NETWORK_COUNTERS:
            return None, parse_counters_probe(stdout)
        snapshot = parse_host_info(stdout)
        counters = parse_network_counters(split_sections(stdout).get("NET_DEV", ""))
        return snapshot, counters

    @staticmethod
    def _failure_for(target) -> Optional[HostInfoFailure]:
        if target is None:
            return None
        if target.failure is None and target.state is not HostCommandState.FAILED:
            return None

        diagnostic = (target.stderr or "").strip()
        failure = target.failure
        if failure is None:
            return HostInfoFailure(
                HostInfoFailureCode.PROBE_FAILED,
                ErrorCode.REMOTE_COMMAND_FAILED,
                diagnostic=diagnostic,
            )

        if failure.code == "broadcast_timeout":
            return HostInfoFailure(
                HostInfoFailureCode.PROBE_TIMED_OUT,
                ErrorCode.OPERATION_TIMED_OUT,
                diagnostic=diagnostic,
            )
        if failure.code == "broadcast_nonzero_exit":
            return HostInfoFailure(
                HostInfoFailureCode.PROBE_FAILED,
                ErrorCode.REMOTE_COMMAND_FAILED,
                diagnostic=diagnostic,
            )
        if failure.code == "broadcast_launch_failed":
            return HostInfoFailure(
                HostInfoFailureCode.PROBE_START_FAILED,
                ErrorCode.SESSION_STARTUP_FAILED,
                diagnostic=diagnostic,
            )

        try:
            error_code = ErrorCode(failure.code)
        except ValueError:
            return HostInfoFailure(
                HostInfoFailureCode.PROBE_FAILED,
                ErrorCode.REMOTE_COMMAND_FAILED,
                diagnostic=diagnostic or failure.message,
            )

        code = {
            ErrorCode.CONNECTION_NOT_FOUND: HostInfoFailureCode.CONNECTION_NOT_FOUND,
            ErrorCode.UNSUPPORTED_SESSION_PROTOCOL: (
                HostInfoFailureCode.SSH_CONNECTION_REQUIRED
            ),
            ErrorCode.OPERATION_TIMED_OUT: HostInfoFailureCode.PROBE_TIMED_OUT,
            ErrorCode.TRANSPORT_TIMEOUT: HostInfoFailureCode.PROBE_TIMED_OUT,
            ErrorCode.SESSION_STARTUP_FAILED: HostInfoFailureCode.PROBE_START_FAILED,
        }.get(error_code)
        if code is not None:
            return HostInfoFailure(code, error_code, diagnostic=diagnostic)
        return HostInfoFailure(
            HostInfoFailureCode.PROBE_FAILED,
            error_code,
            diagnostic=diagnostic or failure.message,
        )
