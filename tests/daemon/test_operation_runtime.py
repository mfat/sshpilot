from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.events import EventType
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.operations import (
    OperationKind,
    OperationState,
    OperationSummary,
    ServiceFailure,
    is_terminal_operation_state,
    is_valid_operation_transition,
)
from sshpilot.daemon.operation_runtime import (
    OperationCancelled,
    OperationRuntime,
)


def _wait_for_state(runtime, operation_id, state, timeout=2.0):
    event = threading.Event()

    def _callback(public_event):
        if (
            public_event.type is EventType.OPERATION_STATE_CHANGED
            and public_event.payload.operation_id == operation_id
            and public_event.payload.state is state
        ):
            event.set()

    subscription = runtime.subscribe_events(_callback)
    try:
        if runtime.get_operation(operation_id).state is state:
            return runtime.get_operation(operation_id)
        assert event.wait(timeout), f"operation did not reach {state.value}"
        return runtime.get_operation(operation_id)
    finally:
        subscription.close()


def test_operation_state_machine_and_summary_validation():
    assert is_valid_operation_transition(OperationState.QUEUED, OperationState.RUNNING)
    assert is_valid_operation_transition(OperationState.RUNNING, OperationState.SUCCEEDED)
    assert not is_valid_operation_transition(OperationState.SUCCEEDED, OperationState.RUNNING)
    assert is_terminal_operation_state(OperationState.CANCELLED)
    assert not is_terminal_operation_state(OperationState.RUNNING)

    with pytest.raises(ValueError):
        OperationSummary(
            operation_id="operation-invalid",
            kind=OperationKind.KEY_DEPLOYMENT,
            state=OperationState.RUNNING,
            message="safe",
            created_at=datetime.now(timezone.utc),
            progress=1.1,
        )


def test_success_failure_and_safe_progress_events():
    runtime = OperationRuntime()
    events = []
    subscription = runtime.subscribe_events(events.append)
    try:
        success = runtime.start_operation(
            OperationKind.KEY_DEPLOYMENT,
            lambda handle: (handle.report("password=secret phase\n", 0.5), "done")[1],
            connection_id=ConnectionId("connection-safe"),
        )
        finished = _wait_for_state(runtime, success.operation_id, OperationState.SUCCEEDED)
        assert finished.message == "done"
        assert finished.progress == 0.5
        assert finished.created_at.tzinfo is not None
        assert finished.started_at is not None
        assert finished.finished_at is not None

        failed = runtime.start_operation(
            OperationKind.KEY_DEPLOYMENT,
            lambda _handle: (_ for _ in ()).throw(
                SshPilotError(ErrorCode.REMOTE_COMMAND_FAILED, "safe failure")
            ),
        )
        failed_summary = _wait_for_state(runtime, failed.operation_id, OperationState.FAILED)
        assert failed_summary.failure == ServiceFailure(
            code=ErrorCode.REMOTE_COMMAND_FAILED.value,
            message="safe failure",
        )
        assert all("password=secret" not in repr(event) for event in events)
        terminal = [
            event
            for event in events
            if event.type is EventType.OPERATION_STATE_CHANGED
            and event.payload.operation_id == success.operation_id
            and is_terminal_operation_state(event.payload.state)
        ]
        assert len(terminal) == 1
    finally:
        subscription.close()
        runtime.shutdown()


def test_reentrant_created_callback_shutdown_cancels_without_starting_worker():
    runtime = OperationRuntime()
    events = []
    body_entered = threading.Event()
    shutdown_done = threading.Event()
    callback_seen = threading.Event()

    def _callback(event):
        events.append(event)
        if event.type is EventType.OPERATION_CREATED:
            callback_seen.set()
            runtime.shutdown()
            shutdown_done.set()

    subscription = runtime.subscribe_events(_callback)
    try:
        operation = runtime.start_operation(
            OperationKind.KEY_DEPLOYMENT,
            lambda _handle: (body_entered.set(), "must-not-run")[1],
        )
        assert callback_seen.is_set()
        assert shutdown_done.is_set()
        assert not body_entered.is_set()
        assert runtime.get_operation(operation.operation_id).state is OperationState.CANCELLED
        states = [
            event.payload.state
            for event in events
            if event.type is EventType.OPERATION_STATE_CHANGED
            and event.payload.operation_id == operation.operation_id
        ]
        assert states == [OperationState.CANCELLED]
        with pytest.raises(RuntimeError):
            runtime.subscribe_events(lambda _event: None)
        with pytest.raises(SshPilotError) as error:
            runtime.start_operation(OperationKind.KEY_DEPLOYMENT, lambda _handle: "no")
        assert error.value.code is ErrorCode.DAEMON_SHUTTING_DOWN
        runtime.shutdown()
        assert not runtime._bodies
        assert not runtime._threads
        assert not runtime._processes
        assert not runtime._cancel_hooks
    finally:
        subscription.close()
        runtime.shutdown()


def test_cancel_before_start_is_terminal_and_idempotent():
    runtime = OperationRuntime()
    entered = threading.Event()
    release = threading.Event()
    gate = threading.Event()
    original_run = runtime._run_operation
    runtime._run_operation = lambda operation_id: (gate.wait(2), original_run(operation_id))[1]
    try:
        operation = runtime.start_operation(
            OperationKind.KEY_DEPLOYMENT,
            lambda _handle: (entered.set(), release.wait(2), "done")[2],
        )
        cancelled = runtime.cancel_operation(operation.operation_id)
        assert cancelled.state is OperationState.CANCELLED
        assert runtime.cancel_operation(operation.operation_id) == cancelled
        gate.set()
        assert not entered.is_set()
    finally:
        gate.set()
        release.set()
        runtime.shutdown()


def test_cancel_running_calls_hook_once_and_cannot_finish_successfully():
    runtime = OperationRuntime()
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    hook_count = 0
    hook_lock = threading.Lock()

    def _hook():
        nonlocal hook_count
        with hook_lock:
            hook_count += 1
        cancelled.set()
        release.set()

    def _body(handle):
        handle.set_cancel_hook(_hook)
        entered.set()
        release.wait(2)
        handle.raise_if_cancelled()
        return "must not succeed"

    try:
        operation = runtime.start_operation(OperationKind.KEY_DEPLOYMENT, _body)
        assert entered.wait(2)
        running = _wait_for_state(runtime, operation.operation_id, OperationState.RUNNING)
        assert running.state is OperationState.RUNNING
        first = runtime.cancel_operation(operation.operation_id)
        second = runtime.cancel_operation(operation.operation_id)
        assert first.state is OperationState.RUNNING
        assert second.state is OperationState.RUNNING
        assert cancelled.wait(2)
        final = _wait_for_state(runtime, operation.operation_id, OperationState.CANCELLED)
        assert final.state is OperationState.CANCELLED
        assert hook_count == 1
    finally:
        release.set()
        runtime.shutdown()


def test_hook_registered_after_cancellation_runs_once_and_reregistration_is_ignored():
    runtime = OperationRuntime()
    entered = threading.Event()
    release = threading.Event()
    hook_calls = []
    hook_called = threading.Event()
    handles = []

    def _body(handle):
        handles.append(handle)
        entered.set()
        release.wait(2)
        handle.raise_if_cancelled()
        return "no"

    def _hook():
        hook_calls.append("called")
        hook_called.set()
        runtime.get_operation(operation.operation_id)
        runtime.cancel_requested(operation.operation_id)

    try:
        operation = runtime.start_operation(OperationKind.KEY_DEPLOYMENT, _body)
        assert entered.wait(2)
        runtime.cancel_operation(operation.operation_id)
        handles[0].set_cancel_hook(_hook)
        handles[0].set_cancel_hook(_hook)
        assert hook_called.wait(2)
        assert hook_calls == ["called"]
        release.set()
        assert _wait_for_state(runtime, operation.operation_id, OperationState.CANCELLED)
    finally:
        release.set()
        runtime.shutdown()


def test_failing_hook_does_not_prevent_process_termination_or_completion():
    runtime = OperationRuntime()
    entered = threading.Event()
    release = threading.Event()
    process = type(
        "Process",
        (),
        {
            "terminate": lambda self: setattr(self, "terminated", True),
            "wait": lambda self, timeout=None: None,
            "kill": lambda self: setattr(self, "killed", True),
        },
    )()

    def _body(handle):
        handle.set_process(process)
        handle.set_cancel_hook(lambda: (_ for _ in ()).throw(RuntimeError("hook")))
        entered.set()
        release.wait(2)
        handle.raise_if_cancelled()
        return "no"

    try:
        operation = runtime.start_operation(OperationKind.KEY_DEPLOYMENT, _body)
        assert entered.wait(2)
        runtime.cancel_operation(operation.operation_id)
        release.set()
        final = _wait_for_state(runtime, operation.operation_id, OperationState.CANCELLED)
        assert final.state is OperationState.CANCELLED
        assert getattr(process, "terminated", False)
    finally:
        release.set()
        runtime.shutdown()


def test_progress_and_completion_events_are_monotonic_and_terminal_last():
    runtime = OperationRuntime()
    events = []
    entered = threading.Event()
    release = threading.Event()
    subscription = runtime.subscribe_events(events.append)

    def _body(handle):
        entered.set()
        release.wait(2)
        handle.report("progress", 0.5)
        return "done"

    try:
        operation = runtime.start_operation(OperationKind.KEY_DEPLOYMENT, _body)
        assert entered.wait(2)
        _wait_for_state(runtime, operation.operation_id, OperationState.RUNNING)
        progress_thread = threading.Thread(
            target=lambda: runtime.report_progress(operation.operation_id, "other", 0.75)
        )
        progress_thread.start()
        release.set()
        progress_thread.join(timeout=2)
        final = _wait_for_state(runtime, operation.operation_id, OperationState.SUCCEEDED)
        operation_events = [
            event.payload.state
            for event in events
            if event.type is EventType.OPERATION_STATE_CHANGED
            and event.payload.operation_id == operation.operation_id
        ]
        assert operation_events[-1] is OperationState.SUCCEEDED
        assert operation_events.count(OperationState.SUCCEEDED) == 1
        assert all(
            state is not OperationState.RUNNING
            for state in operation_events[operation_events.index(OperationState.SUCCEEDED) + 1 :]
        )
        assert final.state is OperationState.SUCCEEDED
    finally:
        release.set()
        subscription.close()
        runtime.shutdown()


def test_completion_cancellation_race_has_one_terminal_winner():
    runtime = OperationRuntime()
    entered = threading.Event()
    continue_body = threading.Event()

    def _body(_handle):
        entered.set()
        continue_body.wait(2)
        return "completed"

    try:
        operation = runtime.start_operation(OperationKind.KEY_DEPLOYMENT, _body)
        assert entered.wait(2)
        _wait_for_state(runtime, operation.operation_id, OperationState.RUNNING)
        runtime.cancel_operation(operation.operation_id)
        continue_body.set()
        final = _wait_for_state(runtime, operation.operation_id, OperationState.CANCELLED)
        assert final.state is OperationState.CANCELLED
        assert final.finished_at is not None
    finally:
        continue_body.set()
        runtime.shutdown()


def test_retention_keeps_active_and_expires_old_terminal_records():
    runtime = OperationRuntime(terminal_retention=1)
    release = threading.Event()
    try:
        active = runtime.start_operation(
            OperationKind.KEY_DEPLOYMENT,
            lambda _handle: (release.wait(2), "active")[1],
        )
        _wait_for_state(runtime, active.operation_id, OperationState.RUNNING)
        first = runtime.start_operation(OperationKind.KEY_DEPLOYMENT, lambda _handle: "one")
        _wait_for_state(runtime, first.operation_id, OperationState.SUCCEEDED)
        second = runtime.start_operation(OperationKind.KEY_DEPLOYMENT, lambda _handle: "two")
        _wait_for_state(runtime, second.operation_id, OperationState.SUCCEEDED)
        assert runtime.get_operation(active.operation_id).state is OperationState.RUNNING
        with pytest.raises(SshPilotError) as error:
            runtime.get_operation(first.operation_id)
        assert error.value.code is ErrorCode.OPERATION_NOT_FOUND
    finally:
        release.set()
        runtime.shutdown()


def test_shutdown_requests_cancellation_rejects_new_work_and_closes_events():
    runtime = OperationRuntime(shutdown_timeout=1.0)
    entered = threading.Event()
    release = threading.Event()

    def _body(handle):
        entered.set()
        release.wait(2)
        handle.raise_if_cancelled()
        return "late"

    operation = runtime.start_operation(OperationKind.KEY_DEPLOYMENT, _body)
    assert entered.wait(2)
    _wait_for_state(runtime, operation.operation_id, OperationState.RUNNING)
    runtime.shutdown()
    release.set()
    final = runtime.get_operation(operation.operation_id)
    assert final.state is OperationState.CANCELLED
    with pytest.raises(SshPilotError) as error:
        runtime.start_operation(OperationKind.KEY_DEPLOYMENT, lambda _handle: "no")
    assert error.value.code is ErrorCode.DAEMON_SHUTTING_DOWN
    runtime.shutdown()


def test_callback_can_reenter_runtime_without_runtime_lock_deadlock():
    runtime = OperationRuntime()
    observed = []

    def _callback(event):
        if event.type is EventType.OPERATION_STATE_CHANGED:
            observed.append(runtime.get_operation(event.payload.operation_id).state)

    subscription = runtime.subscribe_events(_callback)
    try:
        operation = runtime.start_operation(OperationKind.KEY_DEPLOYMENT, lambda _handle: "done")
        final = _wait_for_state(runtime, operation.operation_id, OperationState.SUCCEEDED)
        assert final.state is OperationState.SUCCEEDED
        assert OperationState.RUNNING in observed
    finally:
        subscription.close()
        runtime.shutdown()


def test_operation_cancelled_exception_is_terminal_cancelled():
    runtime = OperationRuntime()
    try:
        operation = runtime.start_operation(
            OperationKind.KEY_DEPLOYMENT,
            lambda _handle: (_ for _ in ()).throw(OperationCancelled()),
        )
        final = _wait_for_state(runtime, operation.operation_id, OperationState.CANCELLED)
        assert final.failure is None
    finally:
        runtime.shutdown()
