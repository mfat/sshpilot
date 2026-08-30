"""Minimal shared daemon operation lifecycle.

Public-key deployment and remote authorized-key mutations are user-visible
commands that may take time. This runtime owns their small explicit state
machine — queued/running/succeeded/failed/cancelled — plus safe messages,
timestamps, optional safe progress and cooperative cancellation. It is
deliberately *not* a workflow engine: no dependency graphs, no resumable
persistence, no plugin actions.

Operations run on daemon-owned worker threads. Event publication goes through
the shared ``EventPublisher`` so frontends observe ``operation.created`` /
``operation.state_changed`` like any other lifecycle family.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Deque, Dict, Optional, Tuple, Union

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.logging_support import log_context
from sshpilot.api.events import CoreEventCallback, EventPublisher, EventType, Subscription
from sshpilot.api.models.common import ClientId, ConnectionId, utc_now
from sshpilot.api.models.operations import (
    IdentityFailure,
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
    ServiceFailure,
    SftpFailure,
    is_terminal_operation_state,
    is_valid_operation_transition,
)
from sshpilot.runtime_identity import new_operation_id

logger = logging.getLogger(__name__)

_DEFAULT_TERMINAL_RETENTION = 128
_DEFAULT_SHUTDOWN_TIMEOUT = 3.0


class OperationCancelled(Exception):
    """Raised inside an operation body when cancellation was requested."""


class OperationHandle:
    """The body-facing control surface of one running operation."""

    def __init__(self, runtime: "OperationRuntime", operation_id: OperationId) -> None:
        self._runtime = runtime
        self.operation_id = operation_id

    def report(self, message: str, progress: Optional[float] = None) -> None:
        """Publish a safe status message and optional bounded progress."""
        self._runtime.report_progress(self.operation_id, message, progress)

    def set_process(self, process: object) -> None:
        """Register the supervised child process for kill-on-cancel."""
        self._runtime.set_operation_process(self.operation_id, process)

    def clear_process(self) -> None:
        self._runtime.set_operation_process(self.operation_id, None)

    def set_cancel_hook(self, hook: Optional[Callable[[], None]]) -> None:
        """Register one cooperative cancellation hook for the operation."""
        self._runtime.set_cancel_hook(self.operation_id, hook)

    def raise_if_cancelled(self) -> None:
        if self._runtime.cancel_requested(self.operation_id):
            raise OperationCancelled()

    def set_result(self, result: Dict) -> None:
        """Attach a wire-safe result payload to the operation summary."""
        self._runtime.set_operation_result(self.operation_id, result)


OperationBody = Callable[[OperationHandle], str]
OperationFailure = Union[ServiceFailure, SftpFailure, IdentityFailure]
OperationFailureMapper = Callable[[BaseException], OperationFailure]


class OperationRuntime:
    """Thread-safe registry and executor for daemon operations."""

    def __init__(
        self,
        *,
        terminal_retention: int = _DEFAULT_TERMINAL_RETENTION,
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        if type(terminal_retention) is not int or terminal_retention < 0:
            raise ValueError("terminal_retention must be a non-negative integer")
        if shutdown_timeout < 0:
            raise ValueError("shutdown_timeout must be non-negative")
        self._condition = threading.Condition(threading.RLock())
        self._records: Dict[OperationId, OperationSummary] = {}
        self._terminal_records: "OrderedDict[OperationId, None]" = OrderedDict()
        self._bodies: Dict[OperationId, OperationBody] = {}
        self._failure_mappers: Dict[OperationId, OperationFailureMapper] = {}
        self._threads: Dict[OperationId, threading.Thread] = {}
        self._thread_started: set[OperationId] = set()
        self._cancel_requested: Dict[OperationId, bool] = {}
        self._cancel_hooks: Dict[OperationId, Callable[[], None]] = {}
        self._cancel_hook_called: set[OperationId] = set()
        self._processes: Dict[OperationId, object] = {}
        self._publisher = EventPublisher()
        self._event_condition = threading.Condition(threading.Lock())
        self._event_queue: Deque[Tuple[EventType, OperationSummary]] = deque()
        self._event_draining = False
        self._publisher_close_requested = False
        self._closed = False
        self._terminal_retention = terminal_retention
        self._shutdown_timeout = float(shutdown_timeout)

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        return self._publisher.subscribe(callback)

    def start_operation(
        self,
        kind: OperationKind,
        body: OperationBody,
        *,
        connection_id: Optional[ConnectionId] = None,
        owner_client_id: Optional[ClientId] = None,
        message: str = "",
        failure_mapper: Optional[OperationFailureMapper] = None,
    ) -> OperationSummary:
        """Register and queue one operation; return its initial summary."""
        if not isinstance(kind, OperationKind):
            raise TypeError("operation kind must be an OperationKind")
        if not callable(body):
            raise TypeError("operation body must be callable")
        if failure_mapper is not None and not callable(failure_mapper):
            raise TypeError("operation failure mapper must be callable or None")
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
                message=_sanitize_message(message),
                created_at=utc_now(),
                connection_id=connection_id,
                owner_client_id=owner_client_id,
            )
            self._records[operation_id] = summary
            self._bodies[operation_id] = body
            if failure_mapper is not None:
                self._failure_mappers[operation_id] = failure_mapper
            self._cancel_requested[operation_id] = False
            thread = threading.Thread(
                target=self._run_operation,
                args=(operation_id,),
                name=f"sshpilot-operation-{operation_id[:12]}",
                daemon=True,
            )
            self._threads[operation_id] = thread
            should_drain = self._queue_event_locked(
                EventType.OPERATION_CREATED, summary
            )
        if should_drain:
            self._drain_events()
        with self._condition:
            current = self._records.get(operation_id)
            if current is None or current.state is not OperationState.QUEUED or self._closed:
                self._cleanup_worker_locked(operation_id)
                return current or summary
            self._thread_started.add(operation_id)
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

    def client_can_interact(
        self,
        operation_id: OperationId,
        client_id: ClientId,
    ) -> bool:
        """Whether *client_id* may claim interactions scoped to an operation.

        Mirrors the session/SFTP/forward runtimes: only the operation's owner
        client may see and answer the prompts its worker raises (password,
        passphrase, host-key, FIDO presence). An operation recorded without an
        owner is not claimable by any client, and unknown operation ids are
        always denied — a ``operation-`` prefixed scope that never belonged to
        a registered operation stays invisible.
        """
        with self._condition:
            summary = self._records.get(operation_id)
            if summary is None or summary.owner_client_id is None:
                return False
            return summary.owner_client_id == client_id

    def cancel_operation(self, operation_id: OperationId) -> OperationSummary:
        """Request cancellation and interrupt registered work when possible."""
        with self._condition:
            summary = self._records.get(operation_id)
            if summary is None:
                raise SshPilotError(
                    ErrorCode.OPERATION_NOT_FOUND,
                    "The requested operation does not exist",
                )
            if is_terminal_operation_state(summary.state):
                return summary
            self._cancel_requested[operation_id] = True
            hook = self._take_cancel_hook_locked(operation_id)
            process = self._processes.get(operation_id)
            if summary.state is OperationState.QUEUED:
                updated = self._build_transition_locked(
                    summary,
                    OperationState.CANCELLED,
                    "The operation was cancelled",
                )
                self._records[operation_id] = updated
                self._remember_terminal_locked(operation_id)
            else:
                updated = summary
        if hook is not None:
            self._invoke_cancel_hook(hook)
        if process is not None:
            self._terminate_process(process)
        if summary.state is OperationState.QUEUED:
            with self._condition:
                should_drain = self._queue_event_locked(
                    EventType.OPERATION_STATE_CHANGED, updated
                )
            if should_drain:
                self._drain_events()
        return updated

    def set_operation_result(self, operation_id: OperationId, result: Dict) -> None:
        if type(result) is not dict:
            raise TypeError("operation result must be a mapping")
        with self._condition:
            summary = self._records.get(operation_id)
            if summary is None or is_terminal_operation_state(summary.state):
                return
            updated = OperationSummary(
                operation_id=summary.operation_id,
                kind=summary.kind,
                state=summary.state,
                message=summary.message,
                created_at=summary.created_at,
                connection_id=summary.connection_id,
                started_at=summary.started_at,
                finished_at=summary.finished_at,
                progress=summary.progress,
                owner_client_id=summary.owner_client_id,
                failure=summary.failure,
                result=result,
            )
            self._records[operation_id] = updated

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
            normalized_progress = _normalize_progress(progress, summary.progress)
            updated = OperationSummary(
                operation_id=summary.operation_id,
                kind=summary.kind,
                state=summary.state,
                message=_sanitize_message(message),
                created_at=summary.created_at,
                connection_id=summary.connection_id,
                started_at=summary.started_at,
                finished_at=summary.finished_at,
                progress=normalized_progress,
                owner_client_id=summary.owner_client_id,
                failure=summary.failure,
                result=summary.result,
            )
            self._records[operation_id] = updated
            should_drain = self._queue_event_locked(
                EventType.OPERATION_STATE_CHANGED, updated
            )
        if should_drain:
            self._drain_events()

    def set_operation_process(self, operation_id: OperationId, process: object) -> None:
        with self._condition:
            if process is None:
                self._processes.pop(operation_id, None)
            else:
                self._processes[operation_id] = process
            cancel = self._cancel_requested.get(operation_id, False)
        if cancel and process is not None:
            self._terminate_process(process)

    def set_cancel_hook(
        self, operation_id: OperationId, hook: Optional[Callable[[], None]]
    ) -> None:
        if hook is not None and not callable(hook):
            raise TypeError("cancel hook must be callable or None")
        with self._condition:
            summary = self._records.get(operation_id)
            if summary is None or operation_id in self._cancel_hook_called:
                return
            if hook is None:
                self._cancel_hooks.pop(operation_id, None)
                return
            self._cancel_hooks[operation_id] = hook
            cancel = self._cancel_requested.get(operation_id, False)
            if cancel:
                hook = self._take_cancel_hook_locked(operation_id)
            else:
                hook = None
        if hook is not None:
            self._invoke_cancel_hook(hook)

    def _queue_event_locked(
        self, event_type: EventType, summary: OperationSummary
    ) -> bool:
        with self._event_condition:
            self._event_queue.append((event_type, summary))
            if self._event_draining:
                return False
            self._event_draining = True
            return True

    def _drain_events(self) -> None:
        while True:
            with self._event_condition:
                if self._event_queue:
                    event_type, summary = self._event_queue.popleft()
                else:
                    self._event_draining = False
                    close = self._publisher_close_requested
                    self._publisher_close_requested = False
                    break
            try:
                self._publisher.publish(
                    event_type,
                    summary,
                    connection_id=summary.connection_id,
                )
            except RuntimeError:
                logger.debug("Operation event publisher is closed", exc_info=True)
        if close:
            self._publisher.close()

    def _request_publisher_close(self) -> None:
        with self._event_condition:
            self._publisher_close_requested = True
            should_drain = not self._event_draining and bool(self._event_queue)
            close_now = not self._event_draining and not self._event_queue
            if should_drain:
                self._event_draining = True
        if should_drain:
            self._drain_events()
        elif close_now:
            self._publisher.close()

    def _queue_terminal_event_locked(self, summary: OperationSummary) -> None:
        self._queue_event_locked(EventType.OPERATION_STATE_CHANGED, summary)

    def cancel_requested(self, operation_id: OperationId) -> bool:
        with self._condition:
            return self._cancel_requested.get(operation_id, False)

    def _run_operation(self, operation_id: OperationId) -> None:
        with self._condition:
            body = self._bodies.get(operation_id)
            failure_mapper = self._failure_mappers.get(operation_id)
            summary = self._records.get(operation_id)
            if body is None or summary is None or is_terminal_operation_state(summary.state):
                self._cleanup_worker_locked(operation_id)
                return
            if self._cancel_requested.get(operation_id, False):
                updated = self._build_transition_locked(
                    summary,
                    OperationState.CANCELLED,
                    "The operation was cancelled",
                )
                self._records[operation_id] = updated
                self._remember_terminal_locked(operation_id)
                publish = updated
            else:
                updated = self._build_transition_locked(
                    summary, OperationState.RUNNING, summary.message
                )
                self._records[operation_id] = updated
                publish = updated
            should_drain = self._queue_event_locked(
                EventType.OPERATION_STATE_CHANGED, publish
            )
        if should_drain:
            self._drain_events()
        if publish.state is OperationState.CANCELLED:
            with self._condition:
                self._cleanup_worker_locked(operation_id)
            return
        handle = OperationHandle(self, operation_id)
        try:
            with log_context(
                operation=operation_id,
                client=summary.owner_client_id,
                connection=summary.connection_id,
            ):
                logger.debug("operation started")
                final_message = body(handle)
            with self._condition:
                summary = self._records.get(operation_id)
                if summary is None or is_terminal_operation_state(summary.state):
                    return
                target = (
                    OperationState.CANCELLED
                    if self._cancel_requested.get(operation_id, False)
                    else OperationState.SUCCEEDED
                )
                updated = self._build_transition_locked(
                    summary,
                    target,
                    "The operation was cancelled" if target is OperationState.CANCELLED else final_message,
                )
                self._records[operation_id] = updated
                if is_terminal_operation_state(target):
                    self._remember_terminal_locked(operation_id)
                should_drain = self._queue_event_locked(
                    EventType.OPERATION_STATE_CHANGED, updated
                )
            if should_drain:
                self._drain_events()
        except OperationCancelled:
            self._finish_from_worker(
                operation_id,
                OperationState.CANCELLED,
                "The operation was cancelled",
            )
        except SshPilotError as error:
            with log_context(
                operation=operation_id,
                client=summary.owner_client_id,
                connection=summary.connection_id,
            ):
                logger.info("operation failed code=%s", error.code.value)
            if self.cancel_requested(operation_id):
                self._finish_from_worker(
                    operation_id,
                    OperationState.CANCELLED,
                    "The operation was cancelled",
                )
            else:
                if failure_mapper is not None:
                    failure = failure_mapper(error)
                    message = ""
                else:
                    failure = ServiceFailure(
                        code=error.code.value,
                        message=_sanitize_message(error.message),
                    )
                    message = error.message
                self._finish_from_worker(
                    operation_id,
                    OperationState.FAILED,
                    message,
                    failure=failure,
                )
        except Exception as error:
            with log_context(
                operation=operation_id,
                client=summary.owner_client_id,
                connection=summary.connection_id,
            ):
                logger.exception("operation crashed")
            if self.cancel_requested(operation_id):
                self._finish_from_worker(
                    operation_id,
                    OperationState.CANCELLED,
                    "The operation was cancelled",
                )
            else:
                if failure_mapper is not None:
                    failure = failure_mapper(error)
                    message = ""
                else:
                    failure = ServiceFailure(
                        code=ErrorCode.INTERNAL_ERROR.value,
                        message="The operation failed unexpectedly",
                    )
                    message = "The operation failed unexpectedly"
                self._finish_from_worker(
                    operation_id,
                    OperationState.FAILED,
                    message,
                    failure=failure,
                )
        finally:
            with self._condition:
                self._cleanup_worker_locked(operation_id)

    def _finish_from_worker(
        self,
        operation_id: OperationId,
        target: OperationState,
        message: str,
        *,
        failure: Optional[OperationFailure] = None,
    ) -> None:
        with self._condition:
            summary = self._records.get(operation_id)
            if summary is None or is_terminal_operation_state(summary.state):
                return
            if target is OperationState.SUCCEEDED and self._cancel_requested.get(
                operation_id, False
            ):
                target = OperationState.CANCELLED
                message = "The operation was cancelled"
                failure = None
            updated = self._build_transition_locked(
                summary, target, message, failure=failure
            )
            self._records[operation_id] = updated
            self._remember_terminal_locked(operation_id)
            should_drain = self._queue_event_locked(
                EventType.OPERATION_STATE_CHANGED, updated
            )
        if should_drain:
            self._drain_events()

    def _build_transition_locked(
        self,
        summary: OperationSummary,
        target: OperationState,
        message: str,
        *,
        failure: Optional[OperationFailure] = None,
    ) -> OperationSummary:
        if not is_valid_operation_transition(summary.state, target):
            return summary
        now = utc_now()
        terminal = is_terminal_operation_state(target)
        return OperationSummary(
            operation_id=summary.operation_id,
            kind=summary.kind,
            state=target,
            message=_sanitize_message(message),
            created_at=summary.created_at,
            connection_id=summary.connection_id,
            started_at=(summary.started_at if target is OperationState.QUEUED else summary.started_at or now),
            finished_at=now if terminal else summary.finished_at,
            progress=summary.progress,
            owner_client_id=summary.owner_client_id,
            failure=failure,
            result=summary.result,
        )

    def _remember_terminal_locked(self, operation_id: OperationId) -> None:
        self._terminal_records.pop(operation_id, None)
        self._terminal_records[operation_id] = None
        while len(self._terminal_records) > self._terminal_retention:
            expired, _ = self._terminal_records.popitem(last=False)
            self._records.pop(expired, None)

    def _take_cancel_hook_locked(
        self, operation_id: OperationId
    ) -> Optional[Callable[[], None]]:
        hook = self._cancel_hooks.pop(operation_id, None)
        if hook is not None:
            self._cancel_hook_called.add(operation_id)
        return hook

    @staticmethod
    def _invoke_cancel_hook(hook: Callable[[], None]) -> None:
        try:
            hook()
        except Exception:
            logger.debug("operation cancellation hook failed", exc_info=True)

    def _cleanup_worker_locked(self, operation_id: OperationId) -> None:
        self._bodies.pop(operation_id, None)
        self._failure_mappers.pop(operation_id, None)
        self._threads.pop(operation_id, None)
        self._thread_started.discard(operation_id)
        self._cancel_requested.pop(operation_id, None)
        self._cancel_hooks.pop(operation_id, None)
        self._cancel_hook_called.discard(operation_id)
        self._processes.pop(operation_id, None)

    def shutdown(self) -> None:
        """Reject new work, request bounded cancellation, and close publishing."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            live = tuple(
                operation_id
                for operation_id, summary in self._records.items()
                if not is_terminal_operation_state(summary.state)
            )
            hooks = []
            processes = []
            terminal_events = []
            for operation_id in live:
                self._cancel_requested[operation_id] = True
                hook = self._take_cancel_hook_locked(operation_id)
                if hook is not None:
                    hooks.append(hook)
                process = self._processes.get(operation_id)
                if process is not None:
                    processes.append(process)
                summary = self._records.get(operation_id)
                if summary is not None and summary.state is OperationState.QUEUED:
                    updated = self._build_transition_locked(
                        summary,
                        OperationState.CANCELLED,
                        "The operation was cancelled",
                    )
                    self._records[operation_id] = updated
                    self._remember_terminal_locked(operation_id)
                    self._queue_terminal_event_locked(updated)
        for hook in hooks:
            self._invoke_cancel_hook(hook)
        for process in processes:
            self._terminate_process(process)
        deadline = time.monotonic() + self._shutdown_timeout
        for operation_id in live:
            with self._condition:
                thread = self._threads.get(operation_id)
            with self._condition:
                started = operation_id in self._thread_started
            if thread is None or not started or thread is threading.current_thread():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
        with self._condition:
            for operation_id in live:
                summary = self._records.get(operation_id)
                if summary is None or is_terminal_operation_state(summary.state):
                    continue
                updated = self._build_transition_locked(
                    summary,
                    OperationState.CANCELLED,
                    "The operation was cancelled",
                )
                self._records[operation_id] = updated
                self._remember_terminal_locked(operation_id)
                terminal_events.append(updated)
                self._queue_event_locked(EventType.OPERATION_STATE_CHANGED, updated)
        self._request_publisher_close()

    @staticmethod
    def _terminate_process(process: object) -> None:
        if getattr(process, "_sshpilot_process_group", False):
            try:
                pid = int(getattr(process, "pid"))
                os.killpg(pid, signal.SIGTERM)
            except Exception:
                logger.debug("operation process-group terminate failed", exc_info=True)
        try:
            process.terminate()  # type: ignore[attr-defined]
        except Exception:
            logger.debug("operation process terminate failed", exc_info=True)
        try:
            process.wait(timeout=2.0)  # type: ignore[attr-defined]
        except Exception:
            if getattr(process, "_sshpilot_process_group", False):
                try:
                    pid = int(getattr(process, "pid"))
                    os.killpg(pid, signal.SIGKILL)
                except Exception:
                    logger.debug("operation process-group kill failed", exc_info=True)
            try:
                process.kill()  # type: ignore[attr-defined]
            except Exception:
                logger.debug("operation process kill failed", exc_info=True)


_MAX_MESSAGE_LENGTH = 240


def _sanitize_message(message: str) -> str:
    """Bound and redact user-visible operation messages."""
    text = "".join(
        char for char in str(message or "") if char == "\t" or ord(char) >= 0x20
    ).strip()
    redacted = []
    for token in text.split():
        if "=" in token and token.split("=", 1)[0].lower() in {
            "password",
            "passphrase",
            "secret",
            "token",
            "session",
            "bw_session",
            "api_key",
            "client_secret",
        }:
            redacted.append(token.split("=", 1)[0] + "=<redacted>")
        else:
            redacted.append(token)
    text = " ".join(redacted)
    if len(text) > _MAX_MESSAGE_LENGTH:
        text = text[: _MAX_MESSAGE_LENGTH - 1].rstrip() + "…"
    return text


def _normalize_progress(
    progress: Optional[float], previous: Optional[float]
) -> Optional[float]:
    if progress is None:
        return previous
    try:
        value = float(progress)
    except (TypeError, ValueError) as exc:
        raise ValueError("operation progress must be numeric") from exc
    if not 0.0 <= value <= 1.0:
        raise ValueError("operation progress must be between 0.0 and 1.0")
    return value
