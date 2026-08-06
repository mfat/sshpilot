"""Minimal shared daemon operation lifecycle.

Public-key deployment and remote authorized-key mutations are user-visible
commands that may take time.  This runtime owns their small explicit state
machine — queued/running/succeeded/failed/cancelled — plus safe messages,
timestamps, optional safe progress and cooperative cancellation.  It is
deliberately *not* a workflow engine: no dependency graphs, no resumable
persistence, no plugin actions.

Operations run on daemon-owned worker threads (mirroring the transfer
runtime).  Event publication goes through the shared ``EventPublisher`` so
frontends observe ``operation.created`` / ``operation.state_changed`` like any
other lifecycle family.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, Optional

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.events import CoreEventCallback, EventPublisher, EventType, Subscription
from sshpilot.api.models.common import ClientId, ConnectionId, utc_now
from sshpilot.api.models.operations import (
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
    ServiceFailure,
    is_valid_operation_transition,
)
from sshpilot.runtime_identity import new_operation_id

logger = logging.getLogger(__name__)


class OperationCancelled(Exception):
    """Raised inside an operation body when cancellation was requested."""


class OperationHandle:
    """The body-facing control surface of one running operation.

    The body reports safe progress/messages, registers the child process it
    is supervising (so cancellation can terminate it), and periodically calls
    :meth:`raise_if_cancelled`.
    """

    def __init__(self, runtime: "OperationRuntime", operation_id: OperationId) -> None:
        self._runtime = runtime
        self.operation_id = operation_id

    def report(self, message: str, progress: Optional[float] = None) -> None:
        """Publish a safe status message (and optional 0..1 progress)."""
        self._runtime.report_progress(self.operation_id, message, progress)

    def set_process(self, process: object) -> None:
        """Register the supervised child process for kill-on-cancel."""
        self._runtime.set_operation_process(self.operation_id, process)

    def clear_process(self) -> None:
        self._runtime.set_operation_process(self.operation_id, None)

    def raise_if_cancelled(self) -> None:
        if self._runtime.cancel_requested(self.operation_id):
            raise OperationCancelled()


# The operation body receives its handle and returns a safe terminal message.
OperationBody = Callable[[OperationHandle], str]


class OperationRuntime:
    """Thread-safe registry and executor for daemon operations."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._records: Dict[OperationId, OperationSummary] = {}
        self._bodies: Dict[OperationId, OperationBody] = {}
        self._threads: Dict[OperationId, threading.Thread] = {}
        self._cancel_requested: Dict[OperationId, bool] = {}
        self._processes: Dict[OperationId, object] = {}
        self._publisher = EventPublisher()
        self._closed = False

    # -- subscriptions -----------------------------------------------------
    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        return self._publisher.subscribe(callback)

    # -- lifecycle ---------------------------------------------------------
    def start_operation(
        self,
        kind: OperationKind,
        body: OperationBody,
        *,
        connection_id: Optional[ConnectionId] = None,
        owner_client_id: Optional[ClientId] = None,
        message: str = "",
    ) -> OperationSummary:
        """Register and queue one operation; returns its initial summary."""
        if not isinstance(kind, OperationKind):
            raise TypeError("operation kind must be an OperationKind")
        if not callable(body):
            raise TypeError("operation body must be callable")
        operation_id = OperationId(new_operation_id())
        with self._condition:
            if self._closed:
                raise SshPilotError(
                    ErrorCode.DAEMON_SHUTTING_DOWN,
                    "The daemon is shutting down",
                    retryable=True,
                )
            summary = OperationSummary(
                operation_id=operation_id,
                kind=kind,
                state=OperationState.QUEUED,
                message=message,
                created_at=utc_now(),
                connection_id=connection_id,
                owner_client_id=owner_client_id,
            )
            self._records[operation_id] = summary
            self._bodies[operation_id] = body
            self._cancel_requested[operation_id] = False
            self._publisher.publish(
                EventType.OPERATION_CREATED,
                summary,
                connection_id=connection_id,
            )
            thread = threading.Thread(
                target=self._run_operation,
                args=(operation_id,),
                name=f"sshpilot-operation-{operation_id[:12]}",
                daemon=True,
            )
            self._threads[operation_id] = thread
            thread.start()
            return summary

    def get_operation(self, operation_id: OperationId) -> OperationSummary:
        with self._condition:
            summary = self._records.get(operation_id)
            if summary is None:
                raise SshPilotError(
                    ErrorCode.OPERATION_NOT_FOUND,
                    "The requested operation does not exist",
                )
            return summary

    def cancel_operation(self, operation_id: OperationId) -> OperationSummary:
        """Request cancellation; kills the supervised child if one runs."""
        with self._condition:
            summary = self._records.get(operation_id)
            if summary is None:
                raise SshPilotError(
                    ErrorCode.OPERATION_NOT_FOUND,
                    "The requested operation does not exist",
                )
            if summary.state in (
                OperationState.SUCCEEDED,
                OperationState.FAILED,
                OperationState.CANCELLED,
            ):
                return summary
            self._cancel_requested[operation_id] = True
            process = self._processes.get(operation_id)
        if process is not None:
            self._terminate_process(process)
        with self._condition:
            return self._records[operation_id]

    # -- body-facing hooks ---------------------------------------------------
    def report_progress(
        self,
        operation_id: OperationId,
        message: str,
        progress: Optional[float] = None,
    ) -> None:
        with self._condition:
            summary = self._records.get(operation_id)
            if summary is None or summary.state is not OperationState.RUNNING:
                return
            updated = OperationSummary(
                operation_id=summary.operation_id,
                kind=summary.kind,
                state=summary.state,
                message=_sanitize_message(message),
                created_at=summary.created_at,
                connection_id=summary.connection_id,
                started_at=summary.started_at,
                finished_at=summary.finished_at,
                progress=(
                    float(progress)
                    if progress is not None
                    else summary.progress
                ),
                owner_client_id=summary.owner_client_id,
                failure=summary.failure,
            )
            self._records[operation_id] = updated
        self._publisher.publish(
            EventType.OPERATION_STATE_CHANGED,
            updated,
            connection_id=updated.connection_id,
        )

    def set_operation_process(
        self, operation_id: OperationId, process: object
    ) -> None:
        with self._condition:
            if process is None:
                self._processes.pop(operation_id, None)
            else:
                self._processes[operation_id] = process
            cancel = self._cancel_requested.get(operation_id, False)
        if cancel and process is not None:
            self._terminate_process(process)

    def cancel_requested(self, operation_id: OperationId) -> bool:
        with self._condition:
            return self._cancel_requested.get(operation_id, False)

    # -- worker --------------------------------------------------------------
    def _run_operation(self, operation_id: OperationId) -> None:
        with self._condition:
            body = self._bodies.get(operation_id)
            summary = self._records.get(operation_id)
            if body is None or summary is None:
                return
            if self._cancel_requested.get(operation_id, False):
                self._transition(
                    operation_id, OperationState.CANCELLED, "The operation was cancelled"
                )
                return
            self._transition(operation_id, OperationState.RUNNING, summary.message)
        handle = OperationHandle(self, operation_id)
        try:
            final_message = body(handle)
        except OperationCancelled:
            with self._condition:
                self._transition(
                    operation_id, OperationState.CANCELLED, "The operation was cancelled"
                )
        except SshPilotError as error:
            logger.info(
                "Operation %s failed: %s", operation_id, error.code.value
            )
            with self._condition:
                self._transition(
                    operation_id,
                    OperationState.FAILED,
                    error.message,
                    failure=ServiceFailure(
                        code=error.code.value, message=error.message
                    ),
                )
        except Exception:
            logger.exception("Operation %s crashed", operation_id)
            with self._condition:
                self._transition(
                    operation_id,
                    OperationState.FAILED,
                    "The operation failed unexpectedly",
                    failure=ServiceFailure(
                        code=ErrorCode.INTERNAL_ERROR.value,
                        message="The operation failed unexpectedly",
                    ),
                )
        else:
            with self._condition:
                self._transition(operation_id, OperationState.SUCCEEDED, final_message)
        finally:
            with self._condition:
                self._bodies.pop(operation_id, None)
                self._threads.pop(operation_id, None)
                self._cancel_requested.pop(operation_id, None)
                self._processes.pop(operation_id, None)

    def _transition(
        self,
        operation_id: OperationId,
        target: OperationState,
        message: str,
        *,
        failure: Optional[ServiceFailure] = None,
    ) -> None:
        summary = self._records.get(operation_id)
        if summary is None:
            return
        if not is_valid_operation_transition(summary.state, target):
            return
        now = utc_now()
        updated = OperationSummary(
            operation_id=summary.operation_id,
            kind=summary.kind,
            state=target,
            message=_sanitize_message(message),
            created_at=summary.created_at,
            connection_id=summary.connection_id,
            started_at=summary.started_at if target is OperationState.QUEUED else (
                summary.started_at or now
            ),
            finished_at=(
                now
                if target
                in (
                    OperationState.SUCCEEDED,
                    OperationState.FAILED,
                    OperationState.CANCELLED,
                )
                else summary.finished_at
            ),
            progress=summary.progress,
            owner_client_id=summary.owner_client_id,
            failure=failure,
        )
        self._records[operation_id] = updated
        self._publisher.publish(
            EventType.OPERATION_STATE_CHANGED,
            updated,
            connection_id=updated.connection_id,
        )

    # -- shutdown --------------------------------------------------------------
    def shutdown(self) -> None:
        """Cancel all live operations and stop accepting new ones."""
        with self._condition:
            self._closed = True
            live = [
                operation_id
                for operation_id, summary in self._records.items()
                if summary.state in (OperationState.QUEUED, OperationState.RUNNING)
            ]
            processes = [
                self._processes[operation_id]
                for operation_id in live
                if operation_id in self._processes
            ]
            for operation_id in live:
                self._cancel_requested[operation_id] = True
        for process in processes:
            self._terminate_process(process)
        with self._condition:
            for operation_id in live:
                summary = self._records.get(operation_id)
                if summary is not None and summary.state is OperationState.QUEUED:
                    self._transition(
                        operation_id,
                        OperationState.CANCELLED,
                        "The operation was cancelled",
                    )

    @staticmethod
    def _terminate_process(process: object) -> None:
        try:
            process.terminate()  # type: ignore[attr-defined]
        except Exception:
            logger.debug("operation process terminate failed", exc_info=True)
        try:
            process.wait(timeout=2.0)  # type: ignore[attr-defined]
        except Exception:
            try:
                process.kill()  # type: ignore[attr-defined]
            except Exception:
                logger.debug("operation process kill failed", exc_info=True)


_MAX_MESSAGE_LENGTH = 240


def _sanitize_message(message: str) -> str:
    """Bound and strip control characters from a user-visible message."""
    text = str(message or "")
    text = "".join(
        char for char in text if char == "\t" or ord(char) >= 0x20
    ).strip()
    if len(text) > _MAX_MESSAGE_LENGTH:
        text = text[: _MAX_MESSAGE_LENGTH - 1].rstrip() + "…"
    return text
